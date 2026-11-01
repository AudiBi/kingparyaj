# tests/test_api/test_agent_ticket_cancel.py
"""Un agent annule, avant le lancement du tirage, un ticket qu'il a vendu."""

import re
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.session_expired import session_expired_handler
from app.core.timezone import now_utc
from app.models.bureau import Bureau, CashierSession
from app.models.cash_movement import TicketCashMovement
from app.models.enums import TicketStatus, UserRole
from app.models.ticket import Ticket
from app.routes.agent import router as agent_router
from app.services.horse_race_service import HorseRaceService
from starlette.exceptions import HTTPException as StarletteHTTPException


@pytest_asyncio.fixture
async def make_client(db_session, fake_redis):
    clients = []

    async def _make():
        app = FastAPI()
        app.include_router(agent_router)
        app.add_exception_handler(StarletteHTTPException, session_expired_handler)
        app.add_middleware(AdminCsrfMiddleware)

        async def _db():
            yield db_session

        async def _redis():
            return fake_redis

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_redis] = _redis
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        clients.append(client)
        return client

    yield _make
    for c in clients:
        await c.aclose()


def _csrf(html):
    return re.search(r'name="csrf_token"\s+value="([^"]*)"', html).group(1)


async def _login(client, phone):
    page = await client.get("/agent/login")
    r = await client.post("/agent/login", data={"code": phone, "password": "AgentPass123!", "csrf_token": _csrf(page.text)},
                          follow_redirects=False)
    assert r.status_code == 303


async def _cancel(client, number):
    headers = {"X-CSRFToken": _csrf((await client.get("/agent/login")).text)}
    return await client.post(f"/agent/api/tickets/{number}/cancel", headers=headers)


async def _open_session(db_session, agent, cash="1000"):
    await db_session.refresh(agent)
    session = CashierSession(bureau_id=agent.bureau_id, agent_id=agent.id, starting_balance=Decimal(cash),
                             current_balance=Decimal(cash), expected_balance=Decimal(cash), status="OPEN", opened_at=now_utc())
    db_session.add(session)
    await db_session.commit()
    return session


async def _sold(db_session, fake_redis, make_ticket, agent, balance="100", stake="40", launch=False, bets=True):
    await db_session.refresh(agent)
    info = await make_ticket(agent, balance=Decimal(balance))
    ticket = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == info["ticket_number"]))).scalar_one()
    service = HorseRaceService(db_session, fake_redis)
    race = bet = None
    if bets:
        race = await service.create_race()
        bet = await service.place_bet(race.id, "WIN", [race.participants[0]["player_number"]], stake,
                                      ticket_number=ticket.ticket_number, agent_id=agent.id)
        if launch:
            await service.launch_race(race.id, by=agent.id)
    await db_session.commit()  # les requêtes de l'API tournent dans la même session de test
    return ticket, race, bet, service


@pytest.mark.asyncio
async def test_agent_cancels_his_ticket_before_launch_and_cash_goes_back(make_client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    phone = agent.phone
    session = await _open_session(db_session, agent)
    ticket, race, bet, service = await _sold(db_session, fake_redis, make_ticket, agent)  # solde 60 + mise 40 = 100
    bureau = await db_session.get(Bureau, agent.bureau_id)
    before = Decimal(str(bureau.cash_balance or 0))
    client = await make_client()
    await _login(client, phone)

    r = await _cancel(client, ticket.ticket_number)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["refunded"] == 100.0 and body["data"]["bets_cancelled"] == 1 and "rendre 100.00 HTG" in body["message"]
    for obj in (ticket, bet, session, bureau):
        await db_session.refresh(obj)
    assert ticket.status == TicketStatus.CANCELLED and ticket.balance == 0 and bet.status == "REFUNDED"
    assert session.cash_out_count == 1 and session.cash_out_amount == 100 and session.current_balance == Decimal("900")
    assert bureau.cash_balance == before - Decimal("100")
    assert (await service.bet_totals([race.id])) == {}  # la manche ne compte plus ce pari
    moves = (await db_session.execute(select(TicketCashMovement).where(TicketCashMovement.ticket_id == ticket.id))).scalars().all()
    assert any(m.kind == "CANCEL_REFUND" or "cancel" in str(m.kind).lower() for m in moves)
    assert (await _cancel(client, ticket.ticket_number)).status_code == 400  # une seule fois


@pytest.mark.asyncio
async def test_cancellation_needs_an_open_cash_session(make_client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    phone = agent.phone
    ticket, *_ = await _sold(db_session, fake_redis, make_ticket, agent)
    client = await make_client()
    await _login(client, phone)
    r = await _cancel(client, ticket.ticket_number)
    assert r.status_code == 400 and "session de caisse" in r.json()["error"]
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.ACTIVE


@pytest.mark.asyncio
async def test_only_the_seller_or_a_manager_of_the_bureau_can_cancel(make_client, db_session, fake_redis, make_agent, make_ticket):
    seller = await make_agent()
    colleague = await make_agent(bureau=await db_session.get(Bureau, seller.bureau_id))
    stranger = await make_agent()  # autre bureau
    manager = await make_agent(bureau=await db_session.get(Bureau, seller.bureau_id))
    manager.role = UserRole.MANAGER
    await db_session.commit()
    phones = {"colleague": colleague.phone, "stranger": stranger.phone, "manager": manager.phone}
    for a in (colleague, stranger, manager):
        await _open_session(db_session, a)
    ticket, *_ = await _sold(db_session, fake_redis, make_ticket, seller)
    number = ticket.ticket_number

    for who, status, text in (("colleague", 403, "vendu ce ticket"), ("stranger", 400, "autre bureau")):
        client = await make_client()
        await _login(client, phones[who])
        r = await _cancel(client, number)
        assert r.status_code == status and text in r.json()["error"], (who, r.text)
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.ACTIVE

    client = await make_client()
    await _login(client, phones["manager"])
    assert (await _cancel(client, number)).status_code == 200


@pytest.mark.asyncio
async def test_launched_draw_or_played_ticket_cannot_be_cancelled(make_client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    phone = agent.phone
    session = await _open_session(db_session, agent)
    client = await make_client()
    await _login(client, phone)

    launched, _, bet, _ = await _sold(db_session, fake_redis, make_ticket, agent, launch=True)
    r = await _cancel(client, launched.ticket_number)
    assert r.status_code == 400 and "déjà lancé" in r.json()["error"]
    await db_session.refresh(launched)
    await db_session.refresh(bet)
    assert launched.status == TicketStatus.ACTIVE and bet.status == "PENDING"

    played, race, bet2, service = await _sold(db_session, fake_redis, make_ticket, agent)
    race.result = [p["player_number"] for p in race.participants]
    race.status = "FINISHED"
    await db_session.flush()
    await service.settle_race(race.id)  # le pari est réglé (gagné ou perdu)
    await db_session.commit()
    r = await _cancel(client, played.ticket_number)
    assert r.status_code == 400 and "résultat" in r.json()["error"]
    await db_session.refresh(session)
    assert session.cash_out_count == 0  # aucune espèce rendue


@pytest.mark.asyncio
async def test_not_enough_cash_in_the_drawer_changes_nothing(make_client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    phone = agent.phone
    session = await _open_session(db_session, agent, cash="50")  # 100 à rendre, 50 en caisse
    ticket, _, bet, _ = await _sold(db_session, fake_redis, make_ticket, agent)
    client = await make_client()
    await _login(client, phone)
    r = await _cancel(client, ticket.ticket_number)
    assert r.status_code == 400 and "Caisse insuffisante" in r.json()["error"]
    for obj in (ticket, bet, session):
        await db_session.refresh(obj)
    assert ticket.status == TicketStatus.ACTIVE and bet.status == "PENDING" and session.current_balance == Decimal("50")


@pytest.mark.asyncio
async def test_unused_ticket_without_bets_can_be_cancelled(make_client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    phone = agent.phone
    session = await _open_session(db_session, agent)
    ticket, *_ = await _sold(db_session, fake_redis, make_ticket, agent, balance="75", bets=False)
    client = await make_client()
    await _login(client, phone)
    r = await _cancel(client, ticket.ticket_number)
    assert r.status_code == 200 and r.json()["data"]["refunded"] == 75.0 and r.json()["data"]["bets_cancelled"] == 0
    await db_session.refresh(session)
    assert session.current_balance == Decimal("925")


@pytest.mark.asyncio
async def test_cancel_requires_login_and_csrf(make_client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    phone = agent.phone
    await _open_session(db_session, agent)
    ticket, *_ = await _sold(db_session, fake_redis, make_ticket, agent)
    client = await make_client()
    assert (await client.post(f"/agent/api/tickets/{ticket.ticket_number}/cancel", follow_redirects=False)).status_code in (303, 401, 403)
    await _login(client, phone)
    no_csrf = await client.post(f"/agent/api/tickets/{ticket.ticket_number}/cancel", follow_redirects=False)
    assert no_csrf.status_code in (303, 403)
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.ACTIVE
