# tests/test_services/test_lucky6_service.py
"""Lucky6Service : manches partagées, paris au bureau, règlement selon la table
FIGÉE de la manche, plafond, pair/impair, remboursements, cycle automatique."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.exceptions import AppException, GameException, NotFoundException, ValidationException
from app.core.timezone import now_utc
from app.models.game import GameBet
from app.models.transaction import Transaction
from app.services import lucky6_engine as engine
from app.services.horse_race_service import HorseRaceService
from app.services.lucky6_service import CONFIG_KEY, Lucky6Service
from app.services.wallet_service import WalletService

AGENT = "agent-guichet"
PICKS = [3, 11, 17, 24, 30, 45]
# 6e numéro du joueur (45) en 8e position, 1re boule impaire (3)
FIXED = [3, 11, 40, 17, 24, 30, 2, 45] + [n for n in range(1, 49) if n not in (3, 11, 40, 17, 24, 30, 2, 45)][:27]


@pytest.fixture
def service(db_session, fake_redis):
    return Lucky6Service(db_session, fake_redis)


@pytest.fixture
def fixed_draw(monkeypatch):
    monkeypatch.setattr(engine, "draw_order", lambda seed, n: list(FIXED))


async def _balance(db_session, fake_redis, user_id):
    return await WalletService(db_session, fake_redis).get_balance(user_id)


async def _draw_and_settle(service, race):
    await service.start_race(race.id)
    await service.finish_race(race.id)
    return await service.settle_race(race.id)


def test_fixed_draw_is_valid():
    assert len(FIXED) == 35 and len(set(FIXED)) == 35
    assert engine.sixth_match_position(PICKS, FIXED) == 8


# ========== Création ==========

@pytest.mark.asyncio
async def test_create_round_freezes_config_and_hides_secrets(service):
    race = await service.create_race()
    data = service.serialize_race(race)
    assert race.game_type == "lucky6" and race.status == "BETTING_OPEN" and race.participants == []
    assert race.config["paytable"] == engine.DEFAULT_PAYTABLE
    assert data["paytable"][0] == {"position": 6, "multiplier": 10000}
    assert data["balls"] is None and data["server_seed"] is None and data["server_seed_hash"]
    assert data["duration_ms"] == 80000


@pytest.mark.asyncio
async def test_next_slot_is_aligned_on_the_clock(service):
    config = await service.get_config()
    slot = service.next_scheduled_at(config, datetime(2026, 10, 2, 10, 2, 30))
    assert slot == datetime(2026, 10, 2, 10, 5)
    # moins de 75 s avant le créneau : on passe au suivant (temps de parier)
    assert service.next_scheduled_at(config, datetime(2026, 10, 2, 10, 4, 0)) == datetime(2026, 10, 2, 10, 10)


@pytest.mark.asyncio
async def test_lucky6_and_horse_rounds_are_isolated(service, db_session, fake_redis):
    race = await service.create_race()
    with pytest.raises(NotFoundException):
        await HorseRaceService(db_session, fake_redis).get_race(race.id)
    assert await HorseRaceService(db_session, fake_redis).get_current_race() is None


# ========== Paris ==========

@pytest.mark.asyncio
async def test_six_bet_debits_and_freezes_top_multiplier(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    race = await service.create_race()
    bet = await service.place_bet(race.id, "six", [45, 3, 30, 24, 17, 11], "50", user_id=user.id, agent_id=AGENT)
    assert bet.bet_type == "SIX" and bet.selection == PICKS
    assert bet.odds == Decimal("10000")
    assert bet.potential_payout == Decimal("500000.00")
    assert await _balance(db_session, fake_redis, user.id) == Decimal("950")
    tx = (await db_session.execute(select(Transaction).where(Transaction.bet_id == bet.id))).scalar_one()
    assert tx.reference == f"BET-L6-{bet.id}"


@pytest.mark.asyncio
async def test_potential_payout_is_capped(service, make_user, fake_redis):
    user = await make_user(balance=Decimal("10000"))
    config = engine.validate_config({})
    config["max_payout"] = 200000
    await service.save_config(config)
    race = await service.create_race()
    bet = await service.place_bet(race.id, "SIX", PICKS, "1000", user_id=user.id, agent_id=AGENT)
    assert bet.potential_payout == Decimal("200000")


@pytest.mark.asyncio
@pytest.mark.parametrize("bet_type,selection,error", [
    ("SIX", [1, 2, 3, 4, 5], "exactement"),
    ("SIX", [1, 2, 3, 4, 5, 5], "qu'une fois"),
    ("SIX", [1, 2, 3, 4, 5, 49], "1 à 48"),
    ("WIN", [1], "invalide"),
])
async def test_invalid_bets_are_refused_without_debit(service, db_session, fake_redis, make_user, bet_type, selection, error):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    with pytest.raises(GameException, match=error):
        await service.place_bet(race.id, bet_type, selection, "10", user_id=user.id, agent_id=AGENT)
    assert await _balance(db_session, fake_redis, user.id) == Decimal("100")


@pytest.mark.asyncio
async def test_bets_only_at_an_agent(service, make_user):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    with pytest.raises(ValidationException, match="uniquement chez un agent"):
        await service.place_bet(race.id, "SIX", PICKS, "10", user_id=user.id)


@pytest.mark.asyncio
async def test_closed_game_refuses_bets(service, make_user):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    config = engine.validate_config({})
    config["enabled"] = False
    await service.save_config(config)
    with pytest.raises(GameException, match="fermé"):
        await service.place_bet(race.id, "SIX", PICKS, "10", user_id=user.id, agent_id=AGENT)


@pytest.mark.asyncio
async def test_stake_limits_and_closed_betting(service, make_user):
    user = await make_user(balance=Decimal("100000"))
    race = await service.create_race()
    with pytest.raises(GameException, match="Mise invalide"):
        await service.place_bet(race.id, "SIX", PICKS, "5", user_id=user.id, agent_id=AGENT)
    with pytest.raises(GameException, match="Mise invalide"):
        await service.place_bet(race.id, "SIX", PICKS, "10001", user_id=user.id, agent_id=AGENT)
    await service.close_betting(race.id)
    with pytest.raises(GameException, match="fermés"):
        await service.place_bet(race.id, "SIX", PICKS, "10", user_id=user.id, agent_id=AGENT)


@pytest.mark.asyncio
async def test_parity_disabled_on_round(service, make_user):
    user = await make_user(balance=Decimal("100"))
    config = engine.validate_config({})
    config["parity_enabled"] = False
    await service.save_config(config)
    race = await service.create_race()
    with pytest.raises(GameException, match="pair / impair"):
        await service.place_bet(race.id, "FIRST_ODD", [], "10", user_id=user.id, agent_id=AGENT)


# ========== Règlement ==========

@pytest.mark.asyncio
async def test_settlement_pays_by_position_of_sixth_match(service, db_session, fake_redis, make_user, fixed_draw):
    winner = await make_user(balance=Decimal("100"))
    loser = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    win = await service.place_bet(race.id, "SIX", PICKS, "10", user_id=winner.id, agent_id=AGENT)
    lose = await service.place_bet(race.id, "SIX", [1, 4, 5, 46, 47, 48], "10", user_id=loser.id, agent_id=AGENT)

    result = await _draw_and_settle(service, race)

    assert result["winners"] == 1 and result["settled_bets"] == 2
    expected = Decimal("10") * Decimal(str(engine.DEFAULT_PAYTABLE[8 - 6]))  # position 8 -> x5000
    assert win.status == "WON" and win.winnings == expected == Decimal("50000")
    assert lose.status == "LOST" and lose.winnings == 0
    assert await _balance(db_session, fake_redis, winner.id) == Decimal("90") + expected
    tx = (await db_session.execute(select(Transaction).where(Transaction.reference == f"WIN-L6-{win.id}"))).scalar_one()
    assert tx.amount == expected
    details = service.bet_details(win, race)
    assert details["sixth_position"] == 8 and details["multiplier"] == 5000 and len(details["matched"]) == 6


@pytest.mark.asyncio
async def test_parity_bets(service, make_user, fixed_draw):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    odd = await service.place_bet(race.id, "FIRST_ODD", [], "10", user_id=user.id, agent_id=AGENT)
    even = await service.place_bet(race.id, "FIRST_EVEN", [], "10", user_id=user.id, agent_id=AGENT)
    assert odd.selection == [] and odd.odds == Decimal("1.9")
    await _draw_and_settle(service, race)
    assert odd.status == "WON" and odd.winnings == Decimal("19.00")
    assert even.status == "LOST"


@pytest.mark.asyncio
async def test_settlement_uses_frozen_paytable(service, make_user, fixed_draw):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    bet = await service.place_bet(race.id, "SIX", PICKS, "10", user_id=user.id, agent_id=AGENT)
    # l'admin change la table après l'ouverture : la manche garde la sienne
    config = engine.validate_config({})
    config["paytable"] = engine.scale_paytable(engine.CLASSIC_SHAPE, 0.6)
    await service.save_config(config)
    await _draw_and_settle(service, race)
    assert bet.winnings == Decimal("50000")


@pytest.mark.asyncio
async def test_winnings_capped_at_settlement(service, make_user, fixed_draw):
    user = await make_user(balance=Decimal("1000"))
    config = engine.validate_config({})
    config["max_payout"] = 20000
    await service.save_config(config)
    race = await service.create_race()
    bet = await service.place_bet(race.id, "SIX", PICKS, "100", user_id=user.id, agent_id=AGENT)
    await _draw_and_settle(service, race)
    assert bet.winnings == Decimal("20000")


@pytest.mark.asyncio
async def test_settlement_is_idempotent(service, db_session, fake_redis, make_user, fixed_draw):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    await service.place_bet(race.id, "SIX", PICKS, "10", user_id=user.id, agent_id=AGENT)
    await _draw_and_settle(service, race)
    again = await service.settle_race(race.id)
    assert again["already_settled"]
    assert await _balance(db_session, fake_redis, user.id) == Decimal("50090")


@pytest.mark.asyncio
async def test_ticket_bet_win_goes_back_to_ticket(service, db_session, make_agent, make_ticket, fixed_draw):
    from app.models.ticket import Ticket

    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    race = await service.create_race()
    await service.place_bet(race.id, "FIRST_ODD", [], "40", ticket_number=ticket["ticket_number"], agent_id=agent.id)
    row = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == ticket["ticket_number"]))).scalar_one()
    assert row.balance == Decimal("60")
    await _draw_and_settle(service, race)
    assert row.balance == Decimal("136.00")


@pytest.mark.asyncio
async def test_cancel_refunds_once(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    bet = await service.place_bet(race.id, "SIX", PICKS, "30", user_id=user.id, agent_id=AGENT)
    result = await service.cancel_race(race.id, "Panne")
    assert result["refunded_bets"] == 1 and bet.status == "REFUNDED"
    assert (await service.cancel_race(race.id, "Panne"))["already_cancelled"]
    assert await _balance(db_session, fake_redis, user.id) == Decimal("100")
    tx = (await db_session.execute(select(Transaction).where(Transaction.reference == f"REFUND-L6-{bet.id}"))).scalar_one()
    assert tx.amount == Decimal("30")
    assert service.serialize_race(race)["server_seed"]  # seed révélé à l'annulation


@pytest.mark.asyncio
async def test_balls_visible_only_after_start(service):
    race = await service.create_race()
    await service.close_betting(race.id)
    assert service.serialize_race(race)["balls"] is None
    await service.start_race(race.id)
    data = service.serialize_race(race)
    assert len(data["balls"]) == 35 and data["first_ball_parity"] in ("ODD", "EVEN")
    assert data["server_seed"] is None  # seed révélé à la fin du tirage
    await service.finish_race(race.id)
    verified = await service.verify(race.id)
    assert verified["verification"]["seed_hash_valid"] and verified["verification"]["result_valid"]


# ========== Cycle automatique ==========

@pytest.mark.asyncio
async def test_tick_runs_full_cycle_and_creates_next_round(service, make_user, monkeypatch):
    monkeypatch.setattr(Lucky6Service, "_within_opening_hours", staticmethod(lambda config: True))
    user = await make_user(balance=Decimal("100"))
    now = now_utc()
    counts = await service.tick(now)
    assert counts["created"] == 1
    race = await service.get_open_race()
    assert race.scheduled_at.second == 0 and race.scheduled_at.minute % 5 == 0
    await service.place_bet(race.id, "SIX", PICKS, "10", user_id=user.id, agent_id=AGENT)

    at = race.scheduled_at
    counts = await service.tick(at - timedelta(seconds=10))
    assert counts["closed"] == 1 and counts["created"] == 1  # la manche suivante prend déjà les paris
    assert (await service.tick(at))["started"] == 1
    following = await service.get_open_race()
    assert following.scheduled_at - at == timedelta(minutes=5)
    counts = await service.tick(at + timedelta(milliseconds=80000))
    assert counts["finished"] == 1 and counts["settled"] == 1
    bet = (await service.get_race_bets(race.id))[0]
    assert bet.status in ("WON", "LOST")


@pytest.mark.asyncio
async def test_tick_does_not_create_when_closed(service, fake_redis, monkeypatch):
    monkeypatch.setattr(Lucky6Service, "_within_opening_hours", staticmethod(lambda config: True))
    config = engine.validate_config({})
    config["enabled"] = False
    await service.save_config(config)
    assert (await service.tick())["created"] == 0
    assert json.loads(await fake_redis.get(CONFIG_KEY))["enabled"] is False
