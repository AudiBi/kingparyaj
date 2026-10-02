# tests/test_services/test_keno_shared.py
"""Keno PARTAGÉ (mode par défaut) : tirages communs calés sur l'horloge,
réglages figés par tirage, paris jusqu'à la fermeture, tirage + règlement
par le cycle automatique, état en direct pour les écrans."""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.exceptions import GameException, ValidationException
from app.core.timezone import now_utc
from app.models.enums import KenoBetStatus, KenoDrawStatus
from app.models.keno import KenoDraw
from app.services import keno_engine as engine
from app.services.keno_service import CONFIG_KEY, KenoService
from app.services.wallet_service import WalletService

AGENT = "agent-guichet"


@pytest.fixture
async def service(db_session, fake_redis):
    # toute la journée, pour que les tests ne dépendent pas de l'heure
    await fake_redis.set(CONFIG_KEY, json.dumps(engine.validate_config({"open_hour": 0, "close_hour": 24})))
    return KenoService(db_session, fake_redis)


async def _open_draw(service, minutes=5):
    config = await service.get_config()
    return await service.generate_draw(draw_time=now_utc() + timedelta(minutes=minutes), config=config)


def test_shared_is_the_default_mode():
    config = engine.validate_config({})
    assert config["mode"] == "scheduled" and config["interval_minutes"] == 5
    assert engine.draw_duration_ms(config) == 3000 + 20 * 1500 + 8000


@pytest.mark.parametrize("change,error", [
    ({"interval_minutes": 1}, "Intervalle"),
    ({"ball_interval_ms": 100}, "boules"),
    ({"open_hour": 20, "close_hour": 10}, "ouverture"),
    ({"interval_minutes": 2, "ball_interval_ms": 9000}, "plus longtemps"),
])
def test_timing_validation(change, error):
    with pytest.raises(engine.KenoConfigError, match=error):
        engine.validate_config(change)


def test_first_slot_is_aligned_and_leaves_time_to_bet():
    config = engine.validate_config({})
    assert KenoService.first_slot(config, datetime(2026, 10, 2, 10, 1, 0)) == datetime(2026, 10, 2, 10, 5)
    assert KenoService.first_slot(config, datetime(2026, 10, 2, 10, 4, 0)) == datetime(2026, 10, 2, 10, 10)


@pytest.mark.asyncio
async def test_schedule_next_keeps_two_upcoming_draws_with_frozen_config(service, db_session):
    assert await service.schedule_next() == 2
    assert await service.schedule_next() == 0
    draws = (await db_session.execute(select(KenoDraw).order_by(KenoDraw.draw_time))).scalars().all()
    assert len(draws) == 2 and draws[1].draw_time - draws[0].draw_time == timedelta(minutes=5)
    assert all(d.draw_time.second == 0 and d.draw_time.minute % 5 == 0 for d in draws)
    assert all(d.mode == "scheduled" and d.server_seed_hash and d.config["paytable"] for d in draws)


@pytest.mark.asyncio
async def test_instant_mode_creates_no_shared_draw(service, fake_redis):
    await fake_redis.set(CONFIG_KEY, json.dumps(engine.validate_config({"mode": "instant"})))
    assert await service.schedule_next() == 0


@pytest.mark.asyncio
async def test_bet_uses_frozen_limits_and_payout_uses_frozen_paytable(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    draw = await _open_draw(service)
    bet = await service.create_bet(draw_id=draw.id, picks=[1], stake=100, user_id=user.id, agent_id=AGENT)

    # l'admin change la table APRÈS le pari : ce tirage garde la sienne
    config = await service.get_config()
    config["paytable"] = dict(config["paytable"], **{"1": {"1": "1.5"}})
    await service.save_config(config)
    assert draw.config["paytable"]["1"]["1"] == "2.5"

    draw.draw_time = now_utc() - timedelta(seconds=1)
    await db_session.flush()
    monkey = engine.draw_order(draw.server_seed, draw.draw_number)
    await service.execute_draw(draw.id)
    won = 1 in monkey
    assert bet.status == (KenoBetStatus.WON if won else KenoBetStatus.LOST)
    if won:
        assert bet.winnings == Decimal("250.00")  # x2.5 (table figée), pas x1.5


@pytest.mark.asyncio
async def test_admin_change_applies_to_draws_without_bets(service, db_session, make_user):
    user = await make_user(balance=Decimal("100"))
    with_bet = await _open_draw(service, 5)
    empty = await _open_draw(service, 10)
    await service.create_bet(draw_id=with_bet.id, picks=[5], stake=10, user_id=user.id, agent_id=AGENT)
    config = await service.get_config()
    config["max_bet"] = 5000
    config["stake_options"] = [10, 50]
    await service.save_config(config)
    assert with_bet.config["max_bet"] == 100000
    assert empty.config["max_bet"] == 5000


@pytest.mark.asyncio
async def test_betting_closes_before_draw_time(service, db_session, make_user):
    user = await make_user(balance=Decimal("100"))
    draw = await _open_draw(service)
    draw.draw_time = now_utc() + timedelta(seconds=5)  # fermeture 10 s avant
    await db_session.flush()
    with pytest.raises(GameException, match="fermés"):
        await service.create_bet(draw_id=draw.id, picks=[5], stake=10, user_id=user.id, agent_id=AGENT)
    assert await WalletService(db_session, fake_redis_of(service)).get_balance(user.id) == Decimal("100")


def fake_redis_of(service):
    return service.redis


@pytest.mark.asyncio
async def test_invalid_shared_tickets(service, make_user):
    user = await make_user(balance=Decimal("100"))
    draw = await _open_draw(service)
    for picks, stake in (([], 10), (list(range(1, 12)), 10), ([0], 10), ([81], 10), ([1, 1], 10), ([5], 5)):
        with pytest.raises((ValidationException, GameException)):
            await service.create_bet(draw_id=draw.id, picks=picks, stake=stake, user_id=user.id, agent_id=AGENT)


@pytest.mark.asyncio
async def test_tick_draws_on_time_settles_once_and_prepares_next(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("100"))
    draw = await _open_draw(service)
    await service.create_bet(draw_id=draw.id, picks=[1, 2, 3], stake=20, user_id=user.id, agent_id=AGENT)
    await db_session.commit()

    later = draw.draw_time + timedelta(seconds=1)
    first = await service.tick(later)
    second = await service.tick(later)
    assert len(first["settled"]) == 1 and first["created"] >= 1
    assert second["settled"] == [] and second["created"] == 0
    await db_session.refresh(draw)
    assert draw.status == KenoDrawStatus.COMPLETED and sorted(draw.numbers) == sorted(engine.draw_order(draw.server_seed, draw.draw_number))


@pytest.mark.asyncio
async def test_live_state_and_serialization(service, db_session):
    draw = await _open_draw(service)
    nxt = await _open_draw(service, 10)
    state = await service.live_state()
    assert state["draw"]["draw_id"] == draw.id and state["betting_draw"]["draw_id"] == draw.id
    assert state["draw"]["draw_order"] is None and state["draw"]["server_seed"] is None
    assert state["draw"]["paytable"]["10"]["10"] == 5000

    draw.draw_time = now_utc() - timedelta(seconds=1)
    await db_session.flush()
    await service.execute_draw(draw.id)
    state = await service.live_state()
    shown = state["draw"]
    assert shown["draw_id"] == draw.id  # animation en cours
    assert shown["draw_order"] == engine.draw_order(draw.server_seed, draw.draw_number)
    assert shown["numbers"] == sorted(shown["draw_order"]) and shown["server_seed"] == draw.server_seed
    assert state["betting_draw"]["draw_id"] == nxt.id

    later = now_utc() + timedelta(milliseconds=engine.draw_duration_ms(draw.config) + 1000)
    assert (await service.live_state(later))["draw"]["draw_id"] == nxt.id


@pytest.mark.asyncio
async def test_counter_screen_preview_and_ticket(service, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    draw = await _open_draw(service)
    state = await service.shared_screen_preview(agent.id, draw.id, [7, 3, 3, 99, "x"], "20")
    assert state["preview"]["picks"] == [3, 7] and state["preview"]["max_win"] == 120.0  # 2 sur 2 : x6
    bet = await service.create_bet(draw_id=draw.id, picks=[3, 7], stake=20, ticket_number=ticket["ticket_number"], agent_id=agent.id)
    state = await service.shared_screen_ticket(agent.id, bet)
    assert state["tickets"][-1]["picks"] == [3, 7] and state["tickets"][-1]["status"] == "pending"
    assert ticket["ticket_number"] not in json.dumps(state)
