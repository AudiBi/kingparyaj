# tests/test_services/test_bureau_service.py
"""Bureaux, sessions de caisse (ouverture, encaissements, fermeture avec écart) et
opérations génériques du service de base (liste, comptage, mise à jour, suppression)."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.exceptions import AppException, NotFoundException
from app.models.bureau import Bureau
from app.schemas.bureau import BureauCreate, BureauUpdate, CashierSessionClose, CashierSessionOpen
from app.services.bureau_service import BureauService, CashierSessionService


@pytest.fixture
def bureaus(db_session, fake_redis):
    return BureauService(db_session, fake_redis)


@pytest.fixture
def sessions(db_session, fake_redis):
    return CashierSessionService(db_session, fake_redis)


def _bureau(code="BUR-1", **changes):
    return BureauCreate(name=f"Bureau {code}", code=code, city="Port-au-Prince", **changes)


# ----------------------------------------------------------------------------- bureaux

@pytest.mark.asyncio
async def test_create_bureau_enforces_unique_code(bureaus):
    bureau = await bureaus.create_bureau(_bureau(), user_id="admin-1")
    assert bureau.code == "BUR-1" and bureau.created_by == "admin-1"
    with pytest.raises(AppException, match="déjà utilisé"):
        await bureaus.create_bureau(_bureau())


@pytest.mark.asyncio
async def test_bureau_stats_count_agents_and_open_sessions(bureaus, sessions, make_agent):
    bureau = await bureaus.create_bureau(_bureau())
    agent = await make_agent(bureau=bureau)
    await make_agent(bureau=bureau)
    await sessions.open_session(bureau.id, agent.id, CashierSessionOpen(starting_balance=100))
    stats = await bureaus.get_bureau_stats(bureau.id)
    assert stats["agents_count"] == 2 and stats["active_sessions_count"] == 1
    assert stats["code"] == "BUR-1" and stats["is_active"] is True and stats["cash_balance"] == 0.0
    with pytest.raises(NotFoundException):
        await bureaus.get_bureau_stats("inconnu")


# ----------------------------------------------------------------------------- sessions de caisse

@pytest.mark.asyncio
async def test_open_session_rules(bureaus, sessions, make_agent):
    bureau = await bureaus.create_bureau(_bureau())
    agent = await make_agent(bureau=bureau)
    session = await sessions.open_session(bureau.id, agent.id, CashierSessionOpen(starting_balance=500))
    assert session.status == "OPEN" and session.current_balance == 500 and session.expected_balance == 500
    assert (await sessions.get_current_session(agent.id)).id == session.id
    with pytest.raises(AppException, match="déjà ouverte"):
        await sessions.open_session(bureau.id, agent.id, CashierSessionOpen(starting_balance=0))
    other = await make_agent(bureau=bureau)
    with pytest.raises(NotFoundException):
        await sessions.open_session("bureau-inconnu", other.id, CashierSessionOpen(starting_balance=0))
    assert await sessions.get_current_session(other.id) is None


@pytest.mark.asyncio
async def test_cash_movements_and_closing_with_a_gap(bureaus, sessions, make_agent, db_session):
    bureau = await bureaus.create_bureau(_bureau())
    agent = await make_agent(bureau=bureau)
    session = await sessions.open_session(bureau.id, agent.id, CashierSessionOpen(starting_balance=1000))
    await sessions.add_transaction(session.id, Decimal("300"), "CASH_IN")
    await sessions.add_transaction(session.id, Decimal("50"), "CASH_OUT")
    assert (session.cash_in_count, session.cash_out_count, session.current_balance) == (1, 1, Decimal("1250"))
    with pytest.raises(AppException, match="invalide"):
        await sessions.add_transaction(session.id, Decimal("1"), "AUTRE")

    other = await make_agent(bureau=bureau)
    with pytest.raises(AppException, match="pas autorisé"):
        await sessions.close_session(session.id, other.id, CashierSessionClose(actual_balance=1250))

    closed = await sessions.close_session(session.id, agent.id, CashierSessionClose(actual_balance=1245, difference_reason="Erreur de rendu"))
    assert closed.status == "CLOSED" and closed.expected_balance == Decimal("1250") and closed.difference == Decimal("-5")
    assert closed.difference_reason == "Erreur de rendu" and closed.closed_at is not None
    assert (await db_session.get(Bureau, bureau.id)).cash_balance == Decimal("1245")
    assert await sessions.get_current_session(agent.id) is None
    with pytest.raises(AppException, match="déjà CLOSED"):
        await sessions.close_session(session.id, agent.id, CashierSessionClose(actual_balance=1245))
    with pytest.raises(AppException, match="Session fermée"):
        await sessions.add_transaction(session.id, Decimal("1"), "CASH_IN")


@pytest.mark.asyncio
async def test_session_stats_for_a_day(bureaus, sessions, make_agent):
    bureau = await bureaus.create_bureau(_bureau())
    empty = await sessions.get_session_stats(bureau.id)
    assert empty["total_sessions"] == 0 and empty["net_cash"] == 0.0

    agent = await make_agent(bureau=bureau)
    session = await sessions.open_session(bureau.id, agent.id, CashierSessionOpen(starting_balance=100))
    await sessions.add_transaction(session.id, Decimal("400"), "CASH_IN")
    await sessions.add_transaction(session.id, Decimal("150"), "CASH_OUT")
    await sessions.close_session(session.id, agent.id, CashierSessionClose(actual_balance=340))  # écart -10
    stats = await sessions.get_session_stats(bureau.id)
    assert (stats["total_sessions"], stats["total_cash_in"], stats["total_cash_out"], stats["net_cash"]) == (1, 400.0, 150.0, 250.0)
    assert stats["avg_difference"] == -10.0
    yesterday = await sessions.get_session_stats(bureau.id, datetime.utcnow() - timedelta(days=1))
    assert yesterday["total_sessions"] == 0


# ----------------------------------------------------------------------------- service de base

@pytest.mark.asyncio
async def test_base_service_crud(bureaus):
    first = await bureaus.create_bureau(_bureau("A"))
    second = await bureaus.create_bureau(_bureau("B"))
    assert await bureaus.count() == 2 and await bureaus.count({"code": "A"}) == 1 and await bureaus.count({"code": None}) == 2
    assert {b.code for b in await bureaus.get_all(skip=0, limit=10)} == {"A", "B"}
    assert [b.code for b in await bureaus.get_all(order_by="code", descending=False)] == ["A", "B"]
    assert len(await bureaus.get_all(limit=1)) == 1
    assert await bureaus.exists(code="A") and not await bureaus.exists(code="Z")

    updated = await bureaus.update(first.id, BureauUpdate(city="Cap-Haïtien"), user_id="admin-1")
    assert updated.city == "Cap-Haïtien" and updated.updated_by == "admin-1" and updated.name == "Bureau A"

    assert await bureaus.delete(second.id, user_id="admin-1") is True  # suppression logique
    assert await bureaus.get_by_id(second.id) is None and await bureaus.count() == 1
    with pytest.raises(NotFoundException):
        await bureaus.get_or_raise(second.id)
    assert await bureaus.delete(first.id, soft=False) is True  # suppression définitive
    assert await bureaus.count() == 0
