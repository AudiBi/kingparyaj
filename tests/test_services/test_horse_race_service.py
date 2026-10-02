# tests/test_services/test_horse_race_service.py
"""HorseRaceService : cycle de vie, paris, règlement idempotent, annulation,
cycle automatique — sur la base SQLite de test (tables game_rounds/game_bets)."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.exceptions import AppException, GameException, InsufficientBalanceException, ValidationException
from app.core.timezone import now_utc
from app.models.game import GameBet, GameRound
from app.models.transaction import Transaction, TransactionType
from app.services import horse_race_engine as engine
from app.services import horse_race_service as hr
from app.services.horse_race_service import CONFIG_KEY, DEFAULT_CONFIG, HorseRaceService
from app.services.wallet_service import WalletService

FIXED_ORDER = [10, 7, 11, 9, 8, 6]
AGENT = "agent-guichet"  # les paris se prennent toujours chez un agent


@pytest.fixture
def service(db_session, fake_redis):
    return HorseRaceService(db_session, fake_redis)


@pytest.fixture
def fixed_result(monkeypatch):
    """Classement imposé pour tester les gains de façon déterministe."""
    monkeypatch.setattr(engine, "draw_finishing_order", lambda *a, **k: list(FIXED_ORDER))


async def _balance(db_session, fake_redis, user_id):
    return await WalletService(db_session, fake_redis).get_balance(user_id)


# ========== Création ==========

@pytest.mark.asyncio
async def test_create_race_has_six_participants_odds_and_hidden_secrets(service):
    race = await service.create_race()
    data = service.serialize_race(race)

    assert race.status == "BETTING_OPEN"
    assert race.round_number == 1
    assert [p["player_number"] for p in data["participants"]] == [10, 7, 11, 9, 8, 6]
    assert data["participants"][0]["player_name"] == "Messi"
    assert all(p["odds"]["WIN"] >= 1.01 for p in data["participants"])
    assert data["server_seed_hash"] == engine.seed_hash(race.server_seed)
    assert data["server_seed"] is None and data["results"] is None and data["race_script"] is None
    assert "probability" not in data["participants"][0]


@pytest.mark.asyncio
async def test_round_numbers_increase(service):
    first = await service.create_race()
    second = await service.create_race()
    assert (first.round_number, second.round_number) == (1, 2)


@pytest.mark.asyncio
async def test_create_race_too_close_to_start_is_rejected(service):
    with pytest.raises(ValidationException):
        await service.create_race(scheduled_at=now_utc() + timedelta(seconds=5))


@pytest.mark.parametrize("change,error", [
    ({"runners": DEFAULT_CONFIG["runners"][:5]}, "exactement 6"),
    ({"runners": [dict(r, player_number=10) for r in DEFAULT_CONFIG["runners"]]}, "même numéro"),
    ({"runners": [dict(DEFAULT_CONFIG["runners"][0], player_name="")] + DEFAULT_CONFIG["runners"][1:]}, "Nom"),
    ({"place_positions": 4}, "PLACE"),
    ({"margin": 0.9}, "Marge"),
])
def test_config_validation(change, error):
    with pytest.raises(ValidationException, match=error):
        hr.validate_config(change)


@pytest.mark.asyncio
async def test_saved_config_is_used_for_next_race(service, fake_redis):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config["runners"][5]["player_name"] = "Vinícius"
    config["runners"][5]["player_number"] = 20
    await service.save_config(config)
    race = await service.create_race()
    assert race.participants[5]["player_number"] == 20
    assert race.participants[5]["player_name"] == "Vinícius"


# ========== Paris ==========

@pytest.mark.asyncio
async def test_win_bet_debits_wallet_and_freezes_server_odds(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    race = await service.create_race()
    _, expected_odds = service.quote(race, "WIN", [10])

    bet = await service.place_bet(race.id, "WIN", [10], "100", user_id=user.id, agent_id=AGENT)

    assert bet.odds == expected_odds
    assert bet.potential_payout == (Decimal("100") * expected_odds).quantize(Decimal("0.01"))
    assert await _balance(db_session, fake_redis, user.id) == Decimal("900")
    tx = (await db_session.execute(select(Transaction).where(Transaction.bet_id == bet.id))).scalar_one()
    assert tx.reference == f"BET-HR-{bet.id}" and tx.draw_id == race.id and tx.transaction_type == TransactionType.BET
    await service.close_betting(race.id)
    assert race.total_bets == 1 and race.total_stake == Decimal("100")


@pytest.mark.asyncio
@pytest.mark.parametrize("bet_type,selection", [("PLACE", [10, 7]), ("EXACTA", [10, 7]), ("TRIFECTA", [10, 7, 11])])
async def test_other_bet_types_are_accepted(service, make_user, bet_type, selection):
    user = await make_user(balance=Decimal("1000"))
    race = await service.create_race()
    bet = await service.place_bet(race.id, bet_type, selection, "10", user_id=user.id, agent_id=AGENT)
    assert bet.bet_type == bet_type and bet.status == "PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("bet_type,selection,stake,exc", [
    ("WIN", [99], "10", GameException),            # concurrent inconnu
    ("EXACTA", [10, 10], "10", GameException),     # doublon
    ("TRIFECTA", [10, 7], "10", GameException),    # mauvais nombre
    ("WIN", [10], "5", GameException),             # sous la mise minimum
    ("WIN", [10], "20000", GameException),         # au-dessus du maximum
    ("WIN", [10], "abc", ValidationException),     # montant invalide
])
async def test_invalid_bets_are_rejected_without_debit(service, db_session, fake_redis, make_user, bet_type, selection, stake, exc):
    user = await make_user(balance=Decimal("100000"))
    race = await service.create_race()
    with pytest.raises(exc):
        await service.place_bet(race.id, bet_type, selection, stake, user_id=user.id, agent_id=AGENT)
    assert await _balance(db_session, fake_redis, user.id) == Decimal("100000")


@pytest.mark.asyncio
async def test_insufficient_balance(service, make_user):
    user = await make_user(balance=Decimal("5"))
    race = await service.create_race()
    with pytest.raises(InsufficientBalanceException):
        await service.place_bet(race.id, "WIN", [10], "10", user_id=user.id, agent_id=AGENT)


@pytest.mark.asyncio
async def test_bet_after_betting_closed_is_rejected(service, make_user):
    user = await make_user(balance=Decimal("1000"))
    race = await service.create_race()
    await service.close_betting(race.id)
    with pytest.raises(GameException, match="fermés"):
        await service.place_bet(race.id, "WIN", [10], "10", user_id=user.id, agent_id=AGENT)


@pytest.mark.asyncio
async def test_bet_after_closing_time_is_rejected_even_if_tick_late(service, make_user):
    user = await make_user(balance=Decimal("1000"))
    race = await service.create_race()
    race.betting_closes_at = now_utc() - timedelta(seconds=1)  # le tick n'est pas encore passé
    with pytest.raises(GameException, match="fermés"):
        await service.place_bet(race.id, "WIN", [10], "10", user_id=user.id, agent_id=AGENT)


@pytest.mark.asyncio
async def test_self_excluded_or_limited_player_cannot_bet(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    (await WalletService(db_session, fake_redis).get_by_user_id(user.id)).single_bet_limit = Decimal("50")
    race = await service.create_race()
    with pytest.raises(AppException) as exc:
        await service.place_bet(race.id, "WIN", [10], "100", user_id=user.id, agent_id=AGENT)
    assert exc.value.code == "BET_LIMIT"


@pytest.mark.asyncio
async def test_ticket_bet_debits_ticket(service, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("200"))
    race = await service.create_race()

    bet = await service.place_bet(race.id, "WIN", [9], "50", ticket_number=ticket["ticket_number"], agent_id=agent.id)

    from app.models.ticket import Ticket
    stored = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == ticket["ticket_number"]))).scalar_one()
    assert stored.balance == Decimal("150")
    assert bet.ticket_id == stored.id and bet.user_id is None and bet.agent_id == agent.id


@pytest.mark.asyncio
async def test_bet_needs_exactly_one_funding_source(service, make_user):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    with pytest.raises(ValidationException):
        await service.place_bet(race.id, "WIN", [10], "10", user_id=user.id, ticket_number="KNO-XXXX-0000", agent_id=AGENT)
    with pytest.raises(ValidationException):
        await service.place_bet(race.id, "WIN", [10], "10", agent_id=AGENT)


@pytest.mark.asyncio
async def test_bets_are_only_taken_by_an_agent(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    with pytest.raises(ValidationException, match="agent"):
        await service.place_bet(race.id, "WIN", [10], "10", user_id=user.id)
    assert await _balance(db_session, fake_redis, user.id) == Decimal("100")


# ========== Résultat ==========

@pytest.mark.asyncio
async def test_start_race_generates_verifiable_result(service):
    race = await service.create_race()
    await service.start_race(race.id)

    data = service.serialize_race(race)
    assert race.status == "RUNNING"
    assert sorted(race.result) == sorted([10, 7, 11, 9, 8, 6])
    assert [r["position"] for r in data["results"]] == [1, 2, 3, 4, 5, 6]
    assert data["race_script"]["frames"]
    assert data["server_seed"] is None  # révélé seulement à l'arrivée

    await service.finish_race(race.id)
    data = service.serialize_race(race)
    check = engine.verify_race(
        data["server_seed"], data["server_seed_hash"],
        [p["player_number"] for p in race.participants], [p["probability"] for p in race.participants],
        race.round_number, race.nonce, race.result,
    )
    assert check["seed_hash_valid"] and check["result_valid"]


@pytest.mark.asyncio
async def test_result_cannot_be_generated_twice(service):
    race = await service.create_race()
    await service.start_race(race.id)
    with pytest.raises(GameException):
        await service.start_race(race.id)


# ========== Règlement ==========

@pytest.mark.asyncio
async def test_settlement_pays_winners_once(service, db_session, fake_redis, make_user, fixed_result):
    user = await make_user(balance=Decimal("1000"))
    race = await service.create_race()
    win = await service.place_bet(race.id, "WIN", [10], "100", user_id=user.id, agent_id=AGENT)          # gagne
    place = await service.place_bet(race.id, "PLACE", [6, 10], "100", user_id=user.id, agent_id=AGENT)   # gagne (10 est 1er)
    exacta = await service.place_bet(race.id, "EXACTA", [7, 10], "100", user_id=user.id, agent_id=AGENT) # perd (ordre)
    trifecta = await service.place_bet(race.id, "TRIFECTA", [10, 7, 11], "100", user_id=user.id, agent_id=AGENT)  # gagne
    assert await _balance(db_session, fake_redis, user.id) == Decimal("600")

    await service.start_race(race.id)
    await service.finish_race(race.id)
    summary = await service.settle_race(race.id)

    expected = sum((b.stake * b.odds).quantize(Decimal("0.01")) for b in (win, place, trifecta))
    assert summary["winners"] == 3 and summary["settled_bets"] == 4
    assert (win.status, place.status, exacta.status, trifecta.status) == ("WON", "WON", "LOST", "WON")
    assert exacta.winnings == 0
    assert await _balance(db_session, fake_redis, user.id) == Decimal("600") + expected
    assert race.status == "SETTLED"

    # second règlement : rien ne bouge
    again = await service.settle_race(race.id)
    assert again["already_settled"] is True
    assert await _balance(db_session, fake_redis, user.id) == Decimal("600") + expected
    wins = (await db_session.execute(select(Transaction).where(Transaction.transaction_type == "WIN"))).scalars().all()
    assert sorted(t.reference for t in wins) == sorted(f"WIN-HR-{b.id}" for b in (win, place, trifecta))


@pytest.mark.asyncio
async def test_settlement_uses_odds_frozen_at_bet_time(service, db_session, fake_redis, make_user, fixed_result):
    user = await make_user(balance=Decimal("1000"))
    race = await service.create_race()
    bet = await service.place_bet(race.id, "WIN", [10], "100", user_id=user.id, agent_id=AGENT)
    frozen = bet.odds

    race.participants = [dict(p, odds={"WIN": 1.01, "PLACE": 1.01}) for p in race.participants]  # cotes modifiées après coup
    await service.start_race(race.id)
    await service.finish_race(race.id)
    await service.settle_race(race.id)

    assert bet.winnings == (Decimal("100") * frozen).quantize(Decimal("0.01"))


@pytest.mark.asyncio
async def test_ticket_winnings_go_back_to_ticket(service, db_session, make_agent, make_ticket, fixed_result):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    race = await service.create_race()
    bet = await service.place_bet(race.id, "WIN", [10], "100", ticket_number=ticket["ticket_number"], agent_id=agent.id)
    await service.start_race(race.id)
    await service.finish_race(race.id)
    await service.settle_race(race.id)

    from app.models.ticket import Ticket
    stored = (await db_session.execute(select(Ticket).where(Ticket.id == bet.ticket_id))).scalar_one()
    assert stored.balance == (Decimal("100") * bet.odds).quantize(Decimal("0.01"))


@pytest.mark.asyncio
async def test_cannot_settle_before_finish(service):
    race = await service.create_race()
    with pytest.raises(GameException):
        await service.settle_race(race.id)


# ========== Annulation ==========

@pytest.mark.asyncio
async def test_cancel_refunds_every_bet_once(service, db_session, fake_redis, make_user, make_agent, make_ticket):
    user = await make_user(balance=Decimal("500"))
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    race = await service.create_race()
    b1 = await service.place_bet(race.id, "WIN", [10], "200", user_id=user.id, agent_id=AGENT)
    b2 = await service.place_bet(race.id, "EXACTA", [10, 7], "40", ticket_number=ticket["ticket_number"], agent_id=agent.id)

    result = await service.cancel_race(race.id, reason="Incident technique")
    again = await service.cancel_race(race.id, reason="double clic")

    assert result["refunded_bets"] == 2 and again["already_cancelled"] is True
    assert (b1.status, b2.status) == ("REFUNDED", "REFUNDED")
    assert await _balance(db_session, fake_redis, user.id) == Decimal("500")
    from app.models.ticket import Ticket
    stored = (await db_session.execute(select(Ticket).where(Ticket.id == b2.ticket_id))).scalar_one()
    assert stored.balance == Decimal("100")
    assert race.status == "CANCELLED" and race.cancel_reason == "Incident technique"


@pytest.mark.asyncio
async def test_finished_race_cannot_be_cancelled(service):
    race = await service.create_race()
    await service.start_race(race.id)
    await service.finish_race(race.id)
    with pytest.raises(GameException):
        await service.cancel_race(race.id, reason="trop tard")


# ========== Cycle automatique ==========

@pytest.mark.asyncio
async def test_tick_runs_a_full_race_cycle(service, db_session, fake_redis, make_user, monkeypatch):
    monkeypatch.setattr(HorseRaceService, "_within_opening_hours", staticmethod(lambda config: True))
    user = await make_user(balance=Decimal("1000"))

    now = now_utc()
    assert (await service.tick(now))["created"] == 1
    race = await service.get_current_race()
    assert race.status == "BETTING_OPEN"
    await service.place_bet(race.id, "WIN", [10], "100", user_id=user.id, agent_id=AGENT)

    counts = await service.tick(race.betting_closes_at)
    assert counts["closed"] == 1 and counts["created"] == 1  # la course suivante ouvre aussitôt ses paris
    assert race.status == "BETTING_CLOSED"
    next_race = await service.get_current_race()
    assert next_race.id != race.id or race.status == "BETTING_CLOSED"

    counts = await service.tick(race.scheduled_at)
    assert counts["started"] == 1 and counts["created"] == 0
    assert race.status == "RUNNING"

    counts = await service.tick(race.started_at + timedelta(milliseconds=race.config["race_duration_ms"]))
    assert counts["finished"] == 1 and counts["settled"] == 1
    assert race.status == "SETTLED"

    bet = (await db_session.execute(select(GameBet).where(GameBet.round_id == race.id))).scalar_one()
    assert bet.status in ("WON", "LOST")


@pytest.mark.asyncio
async def test_tick_does_not_create_races_when_auto_disabled(service, fake_redis, monkeypatch):
    monkeypatch.setattr(HorseRaceService, "_within_opening_hours", staticmethod(lambda config: True))
    config = dict(DEFAULT_CONFIG, auto_enabled=False)
    await fake_redis.set(CONFIG_KEY, json.dumps(config))
    assert (await service.tick())["created"] == 0


@pytest.mark.asyncio
async def test_manual_scheduled_race_waits_for_admin(service):
    race = await service.create_race(open_betting=False)
    assert race.status == "SCHEDULED"
    await service.tick()
    assert race.status == "SCHEDULED"
    await service.open_betting(race.id)
    assert race.status == "BETTING_OPEN"


# ========== Diffusion ==========

@pytest.mark.asyncio
async def test_events_are_published_only_after_commit(service, monkeypatch):
    from app.api.websockets import manager as ws

    published = []

    async def fake_publish(message, draw_id="all"):
        published.append(message)

    monkeypatch.setattr(ws, "publish", fake_publish)
    race = await service.create_race()
    assert published == []
    await service.commit_and_publish()
    assert [m["event"] for m in published] == ["betting_opened"]
    assert published[0]["data"]["race_id"] == race.id
    assert published[0]["data"]["results"] is None


# ========== Suivi public et état en direct ==========

@pytest.mark.asyncio
async def test_lookup_by_bet_code_and_ticket_number(service, make_agent, make_ticket, make_user):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    ticket_bet = await service.place_bet(race.id, "WIN", [10], "20", ticket_number=ticket["ticket_number"], agent_id=agent.id)
    account_bet = await service.place_bet(race.id, "WIN", [7], "20", user_id=user.id, agent_id=agent.id)

    assert [b.id for b in await service.lookup_bets(account_bet.id)] == [account_bet.id]
    assert [b.id for b in await service.lookup_bets(account_bet.id.upper())] == [account_bet.id]
    assert [b.id for b in await service.lookup_bets(ticket["ticket_number"].lower())] == [ticket_bet.id]
    assert await service.lookup_bets("KNO-ZZZZ-9999") == []
    assert await service.lookup_bets("") == []


@pytest.mark.asyncio
async def test_live_state_shows_running_race_and_next_betting_race(service):
    first = await service.create_race()
    await service.start_race(first.id)
    second = await service.create_race()

    state = await service.live_state()
    assert state["race"]["race_id"] == first.id and state["race"]["status"] == "RUNNING"
    assert state["betting_race"]["race_id"] == second.id
    assert state["race"]["race_script"] is not None


def test_default_roster_uses_real_player_names():
    names = [r["player_name"] for r in DEFAULT_CONFIG["runners"]]
    assert names == ["Messi", "Ronaldo", "Neymar", "Mbappé", "De Bruyne", "Kimmich"]
    assert "Player" not in names


# ========== Autant de parieurs que l'on veut sur une même course ==========

@pytest.mark.asyncio
async def test_one_race_accepts_many_bettors_and_settles_them_all(
    service, db_session, fake_redis, make_user, make_agent, make_ticket, make_bureau, fixed_result
):
    bureau = await make_bureau()
    agents = [await make_agent(bureau=bureau) for _ in range(3)]
    players = [await make_user(balance=Decimal("1000")) for _ in range(30)]
    tickets = [await make_ticket(agents[i % 3], balance=Decimal("500")) for i in range(10)]
    race = await service.create_race()

    plans = [("WIN", [10]), ("WIN", [7]), ("PLACE", [10, 9]), ("EXACTA", [10, 7]), ("TRIFECTA", [10, 7, 11]), ("EXACTA", [7, 10])]
    bets = []
    for i, player in enumerate(players):
        bet_type, selection = plans[i % len(plans)]
        bets.append(await service.place_bet(race.id, bet_type, selection, "20", user_id=player.id, agent_id=agents[i % 3].id))
    # un même joueur peut aussi parier plusieurs fois sur la même course
    bets.append(await service.place_bet(race.id, "WIN", [9], "50", user_id=players[0].id, agent_id=agents[0].id))
    for i, ticket in enumerate(tickets):
        bet_type, selection = plans[i % len(plans)]
        bets.append(await service.place_bet(race.id, bet_type, selection, "30",
                                            ticket_number=ticket["ticket_number"], agent_id=agents[i % 3].id))

    await service.close_betting(race.id)
    assert race.total_bets == 41
    assert race.total_stake == Decimal("20") * 30 + Decimal("50") + Decimal("30") * 10

    await service.start_race(race.id)
    await service.finish_race(race.id)
    summary = await service.settle_race(race.id)

    assert summary["settled_bets"] == 41
    assert all(b.status in ("WON", "LOST") for b in bets)
    expected_winners = [b for b in bets if engine.is_winning(b.bet_type, b.selection, FIXED_ORDER, 1)]
    assert summary["winners"] == len(expected_winners) > 0
    assert summary["total_payout"] == pytest.approx(float(sum(b.winnings for b in expected_winners)))

    # chaque joueur : 1000 - mises + gains, payé une seule fois
    for player in players[1:]:
        mine = [b for b in bets if b.user_id == player.id]
        expected = Decimal("1000") - sum(b.stake for b in mine) + sum(b.winnings for b in mine)
        assert await _balance(db_session, fake_redis, player.id) == expected
    wins = (await db_session.execute(select(Transaction).where(Transaction.transaction_type == TransactionType.WIN))).scalars().all()
    assert len(wins) == len([b for b in expected_winners if b.user_id]) == len({t.reference for t in wins})

    again = await service.settle_race(race.id)
    assert again["already_settled"] is True


def test_config_rejects_margin_incompatible_with_odds_range():
    from app.services.horse_race_service import validate_config
    from app.core.exceptions import ValidationException
    with pytest.raises(ValidationException, match="Marge incompatible"):
        validate_config({"odds_min": 4.14, "odds_max": 7.79, "margin": 0.35})
    with pytest.raises(ValidationException, match="Cotes invalides"):
        validate_config({"odds_min": 7.79, "odds_max": 4.14})
    assert validate_config({})["odds_min"] == 4.14
    assert validate_config({})["odds_max"] == 7.79
