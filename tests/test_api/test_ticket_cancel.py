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


async def _horse_bet(db_session, fake_redis, agent, ticket, stake="40", launch=False):
    """Pari de course financé par le ticket ; `launch` : la course est lancée (tirage en cours)."""
    service = HorseRaceService(db_session, fake_redis)
    race = await service.create_race()
    first = race.participants[0]["player_number"]
    bet = await service.place_bet(race.id, "WIN", [first], stake, ticket_number=ticket.ticket_number, agent_id=agent.id)
    if launch:
        await service.launch_race(race.id, by=agent.id)
    await db_session.flush()
    return service, race, bet


@pytest.mark.asyncio
async def test_ticket_with_a_bet_on_an_unlaunched_race_is_cancelled_with_its_bet(
    client, db_session, fake_redis, make_agent, make_ticket
):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, balance="100")
    service, race, bet = await _horse_bet(db_session, fake_redis, agent, ticket, stake="40")  # solde du ticket : 60
    bureau = await db_session.get(Bureau, agent.bureau_id)
    before = Decimal(str(bureau.cash_balance or 0))

    r = await _cancel(client, ticket.id)
    assert r.status_code == 200, r.text
    assert "1 pari(s) annulé(s)" in r.json()["message"] and "100" in r.json()["message"]  # 60 de solde + 40 de mise rendue
    await db_session.refresh(ticket)
    await db_session.refresh(bet)
    await db_session.refresh(bureau)
    assert ticket.status == TicketStatus.CANCELLED and ticket.balance == 0
    assert bet.status == "REFUNDED" and bet.settled_at is not None
    assert bureau.cash_balance == before - Decimal("100")
    assert (await service.bet_totals([race.id])) == {}  # la manche ne compte plus ce pari


@pytest.mark.asyncio
async def test_ticket_whose_race_is_already_launched_cannot_be_cancelled(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, balance="100")
    _, race, bet = await _horse_bet(db_session, fake_redis, agent, ticket, launch=True)
    r = await _cancel(client, ticket.id)
    assert r.status_code == 400
    body = r.json()
    assert "déjà lancé" in body["detail"] and body["error"] == body["detail"] and body["success"] is False
    await db_session.refresh(ticket)
    await db_session.refresh(bet)
    assert ticket.status == TicketStatus.ACTIVE and bet.status == "PENDING"  # rien n'a bougé


@pytest.mark.asyncio
async def test_cancellation_is_all_or_nothing(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, balance="200")
    _, _, open_bet = await _horse_bet(db_session, fake_redis, agent, ticket, stake="40")
    # le 2e pari est sur une course lancée (la 1re course reste ouverte : elle n'est pas « en cours »)
    service = HorseRaceService(db_session, fake_redis)
    running = await service.create_race()
    await service.place_bet(running.id, "WIN", [running.participants[0]["player_number"]], "50",
                            ticket_number=ticket.ticket_number, agent_id=agent.id)
    await service.launch_race(running.id, by=agent.id)
    await db_session.flush()

    r = await _cancel(client, ticket.id)
    assert r.status_code == 400
    await db_session.refresh(open_bet)
    await db_session.refresh(ticket)
    assert open_bet.status == "PENDING" and ticket.status == TicketStatus.ACTIVE and ticket.balance == Decimal("110")


@pytest.mark.asyncio
async def test_keno_bet_on_a_pending_draw_is_cancelled_with_the_ticket(client, db_session, fake_redis, make_agent, make_ticket):
    import json

    from app.models.enums import KenoBetStatus
    from app.models.keno import KenoBet
    from app.services import keno_engine
    from app.services.keno_service import CONFIG_KEY, KenoService

    await fake_redis.set(CONFIG_KEY, json.dumps(keno_engine.validate_config({"open_hour": 0, "close_hour": 24})))
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, balance="100")
    keno = KenoService(db_session, fake_redis)
    draw = await keno.generate_draw(config=await keno.get_config())
    bet = await keno.create_bet(draw_id=draw.id, picks=[1, 2, 3], stake=30, ticket_number=ticket.ticket_number, agent_id=agent.id)
    await db_session.flush()

    r = await _cancel(client, ticket.id)
    assert r.status_code == 200, r.text
    await db_session.refresh(bet)
    await db_session.refresh(ticket)
    assert bet.status == KenoBetStatus.REFUNDED and ticket.status == TicketStatus.CANCELLED and ticket.balance == 0

    # tirage déjà fait : le pari est réglé (plus « en attente »), l'annulation ne touche qu'au solde du ticket
    other = await _ticket(db_session, make_ticket, agent, balance="100")
    done = await keno.generate_draw(config=await keno.get_config())
    settled = await keno.create_bet(draw_id=done.id, picks=[4, 5], stake=20, ticket_number=other.ticket_number, agent_id=agent.id)
    await keno.execute_draw(done.id)
    await db_session.flush()
    again = await _cancel(client, other.id)
    assert again.status_code == 200, again.text
    await db_session.refresh(settled)
    assert settled.status in (KenoBetStatus.WON, KenoBetStatus.LOST)  # le résultat n'est pas annulé


@pytest.mark.asyncio
async def test_bulk_cancel_skips_tickets_whose_draw_is_launched(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    free = await _ticket(db_session, make_ticket, agent, balance="100")
    open_ticket = await _ticket(db_session, make_ticket, agent, balance="100")
    locked = await _ticket(db_session, make_ticket, agent, balance="100")
    await _horse_bet(db_session, fake_redis, agent, open_ticket)               # course ouverte : annulable
    await _horse_bet(db_session, fake_redis, agent, locked, launch=True)       # course lancée : refusée
    page = await client.get("/admin/login")
    headers = {"X-CSRFToken": re.search(r'name="csrf_token"\s+value="([^"]*)"', page.text).group(1)}
    r = await client.post("/admin/api/tickets/bulk/cancel", headers=headers,
                          json={"ticket_ids": [free.id, open_ticket.id, locked.id], "reason": "test"})
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == 1 and "tirage déjà lancé" in r.json()["message"]
    for t, expected in ((free, TicketStatus.CANCELLED), (open_ticket, TicketStatus.CANCELLED), (locked, TicketStatus.ACTIVE)):
        await db_session.refresh(t)
        assert t.status == expected


@pytest.mark.asyncio
async def test_already_cancelled_and_unknown_tickets_are_refused_with_a_message(client, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent)
    assert (await _cancel(client, ticket.id)).status_code == 200
    again = await _cancel(client, ticket.id)
    assert again.status_code == 400 and "actifs" in again.json()["error"]
    missing = await _cancel(client, "inconnu")
    assert missing.status_code == 404 and missing.json()["error"] == "Ticket non trouvé"
