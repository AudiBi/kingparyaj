# tests/test_api/test_cash_tickets.py
"""Vente au comptant : le numéro de ticket est créé automatiquement pour
chaque jeu (Lucky6, Horse Races, Keno partagé, Keno instantané), dans la même
transaction que le pari, et enregistré dans la caisse de l'agent."""

import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.timezone import now_utc
from app.models.bureau import CashierSession
from app.models.ticket import Ticket
from app.routes import horse_races, lucky6
from app.routes.agent import router as agent_panel_router
from app.services import keno_engine
from app.services.horse_race_service import HorseRaceService
from app.services.keno_service import CONFIG_KEY, KenoService
from app.services.lucky6_service import Lucky6Service


@pytest_asyncio.fixture
async def client(db_session, fake_redis):
    app = FastAPI()
    app.include_router(agent_panel_router)
    app.include_router(lucky6.agent_router)
    app.include_router(horse_races.agent_router)
    app.add_middleware(AdminCsrfMiddleware)

    async def _db():
        yield db_session

    async def _redis():
        return fake_redis

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(html):
    return re.search(r'name="csrf_token"\s+value="([^"]*)"', html).group(1)


async def _login(client, agent):
    page = await client.get("/agent/login")
    r = await client.post("/agent/login", data={"code": agent.phone, "password": "AgentPass123!", "csrf_token": _csrf(page.text)},
                          follow_redirects=False)
    assert r.status_code == 303


async def _headers(client):
    return {"X-CSRFToken": (await client.get("/agent/api/keno/csrf-token")).json()["csrf_token"]}


async def _open_session(db_session, agent):
    session = CashierSession(bureau_id=agent.bureau_id, agent_id=agent.id, starting_balance=Decimal("0"),
                             current_balance=Decimal("0"), expected_balance=Decimal("0"), status="OPEN", opened_at=now_utc())
    db_session.add(session)
    await db_session.flush()
    return session


async def _tickets(db_session):
    return (await db_session.execute(select(func.count(Ticket.id)))).scalar()


@pytest.mark.asyncio
async def test_lucky6_cash_bet_creates_ticket_automatically(client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    session = await _open_session(db_session, agent)
    race = await Lucky6Service(db_session, fake_redis).create_race()
    await _login(client, agent)
    r = await client.post("/agent/api/lucky6/bet", headers=await _headers(client), json={
        "race_id": race.id, "bet_type": "SIX", "selection": [1, 2, 3, 4, 5, 6], "stake": "50", "player_type": "cash",
    })
    assert r.status_code == 200, r.text
    number = r.json()["bet"]["ticket_number"]
    assert re.fullmatch(r"KNO-[A-Z0-9]{4}-[A-Z0-9]{4}", number)
    ticket = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == number))).scalar_one()
    assert ticket.initial_amount == Decimal("50") and ticket.balance == Decimal("0")
    assert session.cash_in_count == 1 and session.current_balance == Decimal("50")


@pytest.mark.asyncio
async def test_horse_cash_bet_is_the_default(client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    await _open_session(db_session, agent)
    race = await HorseRaceService(db_session, fake_redis).create_race()
    await _login(client, agent)
    r = await client.post("/agent/api/horse-races/bet", headers=await _headers(client), json={
        "race_id": race.id, "bet_type": "WIN", "selection": [10], "stake": "25",
    })
    assert r.status_code == 200, r.text
    assert r.json()["bet"]["ticket_number"].startswith("KNO-")


@pytest.mark.asyncio
async def test_keno_shared_and_instant_cash_bets(client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    await _open_session(db_session, agent)
    service = KenoService(db_session, fake_redis)
    draw = await service.generate_draw(draw_time=now_utc() + timedelta(minutes=5), config=await service.get_config())
    await _login(client, agent)
    r = await client.post("/agent/api/keno/shared/bet", headers=await _headers(client), json={
        "draw_id": draw.id, "picks": [5, 6], "stake": 20, "player_type": "cash",
    })
    assert r.status_code == 200, r.text
    assert r.json()["bet"]["ticket_number"].startswith("KNO-") and r.json()["bet"]["ticket_balance"] == 0

    await fake_redis.set(CONFIG_KEY, json.dumps(keno_engine.validate_config({"mode": "instant"})))
    prepared = await service.prepare_instant_draw(agent.id)
    await db_session.commit()
    r = await client.post("/agent/api/keno/bet", headers=await _headers(client), json={
        "draw_id": prepared.id, "picks": [1, 2, 3], "stake": 10, "player_type": "cash",
    })
    assert r.status_code == 200, r.text
    assert r.json()["ticket_number"].startswith("KNO-")


@pytest.mark.asyncio
async def test_cash_requires_open_cashier_session(client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    race = await Lucky6Service(db_session, fake_redis).create_race()
    await _login(client, agent)
    r = await client.post("/agent/api/lucky6/bet", headers=await _headers(client), json={
        "race_id": race.id, "bet_type": "SIX", "selection": [1, 2, 3, 4, 5, 6], "stake": "50", "player_type": "cash",
    })
    assert r.status_code == 400 and "session de caisse" in r.json()["detail"]
    assert await _tickets(db_session) == 0


@pytest.mark.asyncio
async def test_refused_bet_creates_no_ticket(client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    session = await _open_session(db_session, agent)
    race = await Lucky6Service(db_session, fake_redis).create_race()
    await db_session.commit()
    await _login(client, agent)
    r = await client.post("/agent/api/lucky6/bet", headers=await _headers(client), json={
        "race_id": race.id, "bet_type": "SIX", "selection": [1, 2, 3, 4, 5, 5], "stake": "50", "player_type": "cash",
    })
    assert r.status_code == 400
    await db_session.rollback()
    assert await _tickets(db_session) == 0
    await db_session.refresh(session)
    assert session.cash_in_count == 0


@pytest.mark.asyncio
async def test_histories_show_the_ticket_number(client, db_session, fake_redis, make_agent):
    """Plusieurs gagnants : chaque ligne de l'historique porte le numéro du ticket."""
    agent = await make_agent()
    await _open_session(db_session, agent)
    l6 = await Lucky6Service(db_session, fake_redis).create_race()
    hr = await HorseRaceService(db_session, fake_redis).create_race()
    kservice = KenoService(db_session, fake_redis)
    draw = await kservice.generate_draw(draw_time=now_utc() + timedelta(minutes=5), config=await kservice.get_config())
    await _login(client, agent)
    h = await _headers(client)
    a = (await client.post("/agent/api/lucky6/bet", headers=h, json={"race_id": l6.id, "bet_type": "FIRST_ODD", "stake": "10"})).json()
    b = (await client.post("/agent/api/lucky6/bet", headers=await _headers(client), json={"race_id": l6.id, "bet_type": "FIRST_EVEN", "stake": "10"})).json()
    c = (await client.post("/agent/api/horse-races/bet", headers=await _headers(client), json={"race_id": hr.id, "bet_type": "WIN", "selection": [7], "stake": "10"})).json()
    d = (await client.post("/agent/api/keno/shared/bet", headers=await _headers(client), json={"draw_id": draw.id, "picks": [1], "stake": 10})).json()

    l6_hist = (await client.get("/agent/api/lucky6/my-bets")).json()["bets"]
    assert {x["ticket_number"] for x in l6_hist} == {a["bet"]["ticket_number"], b["bet"]["ticket_number"]}
    assert len({x["ticket_number"] for x in l6_hist}) == 2  # deux tickets distincts
    hr_hist = (await client.get(f"/agent/api/horse-races/races/{hr.id}/my-bets")).json()["bets"]
    assert hr_hist[0]["ticket_number"] == c["bet"]["ticket_number"]
    kn_hist = (await client.get("/agent/api/keno/shared/my-bets")).json()["bets"]
    assert kn_hist[0]["ticket_number"] == d["bet"]["ticket_number"]
    items = (await client.get("/agent/api/keno/history")).json()["items"]
    assert items[0]["ticket_number"] == d["bet"]["ticket_number"]
