# tests/test_api/test_ticket_cancel.py
"""Annulation d'un ticket depuis le panel admin : cas permis, refus avec motif lisible par la page."""

import re
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.session_expired import session_expired_handler
from app.models.bureau import Bureau
from app.models.enums import TicketStatus
from app.routes.admin import router as admin_router
from app.services.horse_race_service import HorseRaceService
from app.models.ticket import Ticket
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


@pytest_asyncio.fixture
async def client(db_session, fake_redis, admin_user):
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(StarletteHTTPException, session_expired_handler)  # comme dans app.main
    app.add_middleware(AdminCsrfMiddleware)

    async def _db():
        yield db_session

    async def _redis():
        return fake_redis

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        page = await c.get("/admin/login")
        token = re.search(r'name="csrf_token"\s+value="([^"]*)"', page.text).group(1)
        await c.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": token})
        yield c


async def _cancel(client, ticket_id, reason="Erreur de saisie"):
    page = await client.get("/admin/login")
    headers = {"X-CSRFToken": re.search(r'name="csrf_token"\s+value="([^"]*)"', page.text).group(1)}
    return await client.post(f"/admin/api/tickets/{ticket_id}/cancel", headers=headers, json={"reason": reason})


async def _ticket(db_session, make_ticket, agent, balance="100"):
    info = await make_ticket(agent, balance=Decimal(balance))
    return (await db_session.execute(select(Ticket).where(Ticket.ticket_number == info["ticket_number"]))).scalar_one()


@pytest.mark.asyncio
async def test_active_ticket_is_cancelled_and_refunded(client, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent)
    bureau = await db_session.get(Bureau, agent.bureau_id)
    before = Decimal(str(bureau.cash_balance or 0))
    r = await _cancel(client, ticket.id)
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True and "100" in r.json()["message"]
    await db_session.refresh(ticket)
    await db_session.refresh(bureau)
    assert ticket.status == TicketStatus.CANCELLED and ticket.balance == 0
    assert bureau.cash_balance == before - Decimal("100")  # le remboursement sort de la caisse du bureau


@pytest.mark.asyncio
async def test_refusal_carries_a_message_the_page_can_show(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, balance="100")
    from app.core.timezone import now_utc
    from app.models.game import GameBet

    race = await HorseRaceService(db_session, fake_redis).create_race()
    db_session.add(GameBet(round_id=race.id, game_type="horse_races", ticket_id=ticket.id, agent_id=agent.id, bet_type="WIN",
                           selection=[race.participants[0]["player_number"]], stake=Decimal("40"), odds=Decimal("5"),
                           potential_payout=Decimal("200"), status="PENDING", placed_at=now_utc()))
    await db_session.flush()

    r = await _cancel(client, ticket.id)
    assert r.status_code == 400
    body = r.json()
    assert "attend son tirage" in body["detail"]
    assert body["error"] == body["detail"] and body["success"] is False  # `error` : ce que lit la page admin
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.ACTIVE  # rien n'a bougé


@pytest.mark.asyncio
async def test_already_cancelled_and_unknown_tickets_are_refused_with_a_message(client, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent)
    assert (await _cancel(client, ticket.id)).status_code == 200
    again = await _cancel(client, ticket.id)
    assert again.status_code == 400 and "actifs" in again.json()["error"]
    missing = await _cancel(client, "inconnu")
    assert missing.status_code == 404 and missing.json()["error"] == "Ticket non trouvé"
