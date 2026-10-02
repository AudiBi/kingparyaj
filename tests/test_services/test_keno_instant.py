# tests/test_services/test_keno_instant.py
"""Keno instantané (un ticket = un tirage) : moteur vérifiable, configuration
modifiable à tout moment, jeu au guichet, plafond de gain."""

import json
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.exceptions import AppException, GameException, InsufficientBalanceException, ValidationException
from app.models.enums import KenoBetStatus, KenoDrawStatus
from app.models.keno import KenoBet, KenoDraw
from app.models.ticket import Ticket
from app.models.transaction import Transaction
from app.services import keno_engine as engine
from app.services.fairness import new_server_seed, seed_hash
from app.services.keno_service import CONFIG_KEY, KenoService
from app.services.wallet_service import WalletService


@pytest_asyncio.fixture(autouse=True)
async def _instant_mode(fake_redis):
    """Ces tests couvrent l'option « instantané » (le mode par défaut est partagé)."""
    import json as _json

    from app.services import keno_engine as _engine

    await fake_redis.set("settings:keno", _json.dumps(_engine.validate_config({"mode": "instant"})))


@pytest.fixture
def service(db_session, fake_redis):
    return KenoService(db_session, fake_redis)


async def _balance(db_session, fake_redis, user_id):
    return await WalletService(db_session, fake_redis).get_balance(user_id)


# ============================================================
# Moteur
# ============================================================

def test_draw_is_deterministic_valid_and_verifiable():
    seed = new_server_seed()
    numbers = engine.draw_numbers(seed, 42)
    assert numbers == engine.draw_numbers(seed, 42)
    assert engine.is_valid_draw(numbers)
    assert sorted(engine.draw_order(seed, 42)) == numbers
    assert numbers != engine.draw_numbers(seed, 43)  # chaque tirage diffère
    check = engine.verify_draw(seed, seed_hash(seed), 42, numbers)
    assert check["seed_hash_valid"] and check["result_valid"]
    assert not engine.verify_draw("autre", seed_hash(seed), 42, numbers)["seed_hash_valid"]


def test_draw_known_vector():
    """Vecteur de référence : le même calcul est fait par la page de vérification (navigateur)."""
    assert engine.draw_order("a" * 64, 1) == [21, 45, 54, 58, 60, 46, 48, 39, 23, 14, 65, 50, 42, 79, 20, 15, 6, 26, 3, 43]
    assert engine.draw_numbers("a" * 64, 1) == [3, 6, 14, 15, 20, 21, 23, 26, 39, 42, 43, 45, 46, 48, 50, 54, 58, 60, 65, 79]


def test_rtp_is_exact_and_scaling_never_exceeds_target():
    assert engine.rtp_table(engine.DEFAULT_PAYTABLE)[1] == 62.5  # 2.5 x P(1/1)=0.25
    for target in (70, 85, 95):
        table = engine.scale_paytable(engine.DEFAULT_PAYTABLE, target)
        for spots, value in engine.rtp_table(table).items():
            assert target - 6 <= value <= target + 1e-6, (spots, value)


@pytest.mark.parametrize("change,message", [
    ({"max_picks": 11}, "Numéros"),
    ({"min_picks": 5, "max_picks": 3}, "Numéros"),
    ({"min_bet": 50, "max_bet": 10}, "Mises"),
    ({"max_bet": 200000}, "Mises"),
    ({"stake_options": [5]}, "proposées"),
    ({"max_payout": 0}, "Gain maximum"),
    ({"mode": "autre"}, "Mode"),
    ({"max_picks": 1, "paytable": {"1": {"1": "5"}}}, "taux de redistribution"),  # 125 % > 100 %
    ({"paytable": {"1": {"2": "5"}}}, "impossible"),
    ({"paytable": {"1": {"1": "-1"}}}, "Multiplicateur"),
    ({"max_picks": 3, "paytable": {"1": {"1": "2"}, "2": {"2": "5"}}}, "manquante"),
])
def test_invalid_config_is_rejected(change, message):
    with pytest.raises(engine.KenoConfigError, match=message):
        engine.validate_config(change)


# ============================================================
# Configuration (modifiable à tout moment)
# ============================================================

@pytest.mark.asyncio
async def test_config_is_saved_without_expiry_and_audited(service, db_session, fake_redis):
    from app.models.audit import AuditLog

    table = engine.scale_paytable(engine.DEFAULT_PAYTABLE, 85)
    saved = await service.save_config({
        "min_bet": 25, "stake_options": [25, 50],
        "paytable": {str(s): {str(h): str(m) for h, m in row.items()} for s, row in table.items()},
    }, admin_id="admin-1")
    assert (await service.get_config()) == saved
    log = (await db_session.execute(select(AuditLog).where(AuditLog.resource_type == "keno_config"))).scalar_one()
    assert log.new_values["min_bet"] == 25.0

    with pytest.raises(ValidationException):
        await service.save_config({"max_picks": 12})
    assert (await service.get_config())["min_bet"] == 25.0  # inchangée


# ============================================================
# Jeu instantané
# ============================================================

@pytest.mark.asyncio
async def test_prepared_draw_shows_hash_before_bet_and_is_reused(service, db_session):
    first = await service.prepare_instant_draw("agent-1")
    again = await service.prepare_instant_draw("agent-1")
    other = await service.prepare_instant_draw("agent-2")
    assert first.id == again.id != other.id
    assert first.mode == "instant" and first.status == KenoDrawStatus.PENDING
    assert first.server_seed_hash == seed_hash(first.server_seed)
    assert first.numbers is None  # rien n'est tiré avant le pari


@pytest.mark.asyncio
async def test_instant_ticket_debits_draws_settles_and_reveals_seed(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    draw = await service.prepare_instant_draw("agent-1")
    expected = engine.draw_numbers(draw.server_seed, draw.draw_number)
    picks = expected[:3] + [n for n in range(1, 81) if n not in expected][:2]  # 3 trouvés sur 5

    result = await service.play_instant(draw.id, picks, 100, agent_id="agent-1", user_id=user.id)

    assert result["winning_numbers"] == expected
    assert sorted(result["draw_order"]) == expected
    assert result["match_count"] == 3 and result["matches"] == sorted(expected[:3])
    assert result["multiplier"] == 2.0 and result["payout"] == 200.0  # 5 joués / 3 trouvés : x2
    assert result["server_seed"] == draw.server_seed
    assert await _balance(db_session, fake_redis, user.id) == Decimal("1100")  # 1000 - 100 + 200
    refs = sorted((await db_session.execute(select(Transaction.reference))).scalars().all())
    bet_id = result["bet_id"]
    assert refs == sorted([f"BET-KENO-{bet_id}", f"WIN-KENO-{bet_id}"])
    assert draw.status == KenoDrawStatus.COMPLETED and draw.total_bets == 1

    verify = await service.verify(draw.id)
    assert verify["verifiable"] and verify["seed_hash_valid"] and verify["result_valid"]


@pytest.mark.asyncio
async def test_instant_draw_cannot_be_played_twice(service, db_session, make_user):
    user = await make_user(balance=Decimal("1000"))
    draw = await service.prepare_instant_draw("agent-1")
    await service.play_instant(draw.id, [1, 2], 10, agent_id="agent-1", user_id=user.id)
    with pytest.raises(GameException, match="déjà été joué"):
        await service.play_instant(draw.id, [1, 2], 10, agent_id="agent-1", user_id=user.id)
    nxt = await service.prepare_instant_draw("agent-1")
    assert nxt.id != draw.id


@pytest.mark.asyncio
async def test_failed_ticket_consumes_nothing(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("5"))
    draw = await service.prepare_instant_draw("agent-1")
    await db_session.commit()
    draw_id, user_id = draw.id, user.id
    with pytest.raises((InsufficientBalanceException, AppException)):
        await service.play_instant(draw_id, [1, 2], 10, agent_id="agent-1", user_id=user_id)
    await db_session.rollback()
    row = await db_session.get(KenoDraw, draw_id)
    assert row.status == KenoDrawStatus.PENDING and row.numbers is None
    assert (await db_session.execute(select(func.count(KenoBet.id)))).scalar() == 0
    assert await _balance(db_session, fake_redis, user_id) == Decimal("5")


@pytest.mark.asyncio
@pytest.mark.parametrize("picks,stake", [([0, 1], 10), ([1, 1], 10), (list(range(1, 12)), 10), ([1], 9), ([1], 100001)])
async def test_instant_ticket_validation(service, make_user, picks, stake):
    user = await make_user(balance=Decimal("1000000"))
    draw = await service.prepare_instant_draw("agent-1")
    with pytest.raises(ValidationException):
        await service.play_instant(draw.id, picks, stake, agent_id="agent-1", user_id=user.id)


@pytest.mark.asyncio
async def test_new_paytable_applies_to_the_next_ticket(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    cfg = engine.validate_config({"mode": "instant"})
    cfg["paytable"]["1"] = {"1": "3.2"}
    await fake_redis.set(CONFIG_KEY, json.dumps(cfg))

    draw = await service.prepare_instant_draw("agent-1")
    winner = engine.draw_numbers(draw.server_seed, draw.draw_number)[0]
    result = await service.play_instant(draw.id, [winner], 10, agent_id="agent-1", user_id=user.id)
    assert result["multiplier"] == 3.2 and result["payout"] == 32.0


@pytest.mark.asyncio
async def test_payout_is_capped(service, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    await fake_redis.set(CONFIG_KEY, json.dumps(engine.validate_config({"max_payout": 15, "mode": "instant"})))
    draw = await service.prepare_instant_draw("agent-1")
    winner = engine.draw_numbers(draw.server_seed, draw.draw_number)[0]
    result = await service.play_instant(draw.id, [winner], 10, agent_id="agent-1", user_id=user.id)
    assert result["payout"] == 15.0  # 10 x 2.5 = 25, plafonné à 15


@pytest.mark.asyncio
async def test_closed_keno_and_agent_only(service, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    draw = await service.prepare_instant_draw("agent-1")
    with pytest.raises(ValidationException, match="agent"):
        await service.play_instant(draw.id, [1], 10, agent_id=None, user_id=user.id)
    await fake_redis.set(CONFIG_KEY, json.dumps(engine.validate_config({"enabled": False})))
    with pytest.raises(GameException, match="fermé"):
        await service.play_instant(draw.id, [1], 10, agent_id="agent-1", user_id=user.id)


@pytest.mark.asyncio
async def test_instant_ticket_with_bureau_ticket(service, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    draw = await service.prepare_instant_draw(agent.id)
    losing = [n for n in range(1, 81) if n not in engine.draw_numbers(draw.server_seed, draw.draw_number)][:4]

    result = await service.play_instant(draw.id, losing, 40, agent_id=agent.id, ticket_number=ticket["ticket_number"])

    assert result["payout"] == 0 and result["status"] == "lost"
    row = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == ticket["ticket_number"]))).scalar_one()
    assert row.balance == Decimal("60")


@pytest.mark.asyncio
async def test_scheduled_bet_on_instant_draw_is_refused(service, make_user):
    user = await make_user(balance=Decimal("100"))
    draw = await service.prepare_instant_draw("agent-1")
    with pytest.raises(GameException, match="instantané"):
        await service.create_bet(draw_id=draw.id, picks=[1], stake=10, user_id=user.id)
