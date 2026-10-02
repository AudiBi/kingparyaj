# tests/test_api/test_keno_shared_api.py
"""Keno partagé par les routes : panel agent, écrans de salle et du guichet.
Le navigateur n'envoie que le tirage, les numéros, la mise et le joueur."""

import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.timezone import now_utc
from app.routes.agent import router as agent_panel_router
from app.routes.keno import public_router
from app.services import keno_engine
from app.services.keno_service import CONFIG_KEY, KenoService
from app.services.wallet_service import WalletService


@pytest_asyncio.fixture
async def make_client(db_session, fake_redis):
    await fake_redis.set(CONFIG_KEY, json.dumps(keno_engine.validate_config({"open_hour": 0, "close_hour": 24})))
    clients = []

    async def _make():
        app = FastAPI()
        app.include_router(agent_panel_router)
        app.include_router(public_router)
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
    for client in clients:
        await client.aclose()


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]*)"', html)
    assert match
    return match.group(1)


async def _login(client, agent):
    page = await client.get("/agent/login")
    response = await client.post("/agent/login", data={"code": agent.phone, "password": "AgentPass123!", "csrf_token": _csrf(page.text)},
                                 follow_redirects=False)
    assert response.status_code == 303


async def _token(client):
    return {"X-CSRFToken": (await client.get("/agent/api/keno/csrf-token")).json()["csrf_token"]}


async def _draw(db_session, fake_redis, minutes=5):
    service = KenoService(db_session, fake_redis)
    draw = await service.generate_draw(draw_time=now_utc() + timedelta(minutes=minutes), config=await service.get_config())
    await db_session.flush()
    return draw


@pytest.mark.asyncio
async def test_agent_shared_flow_with_ticket(make_client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    draw = await _draw(db_session, fake_redis)
    client = await make_client()
    await _login(client, agent)

    page = await client.get("/agent/keno")
    assert page.status_code == 200 and "keno_live.js" in page.text and 'id="picker"' in page.text

    live = (await client.get("/agent/api/keno/live")).json()
    assert live["betting_draw"]["draw_id"] == draw.id and live["config"]["mode"] == "scheduled"

    preview = await client.post("/agent/api/keno/shared/screen", headers=await _token(client),
                                json={"draw_id": draw.id, "picks": [4, 9], "stake": 10})
    assert preview.status_code == 200

    response = await client.post("/agent/api/keno/shared/bet", headers=await _token(client), json={
        "draw_id": draw.id, "picks": [9, 4, 33], "stake": 30, "player_type": "ticket", "identifier": ticket["ticket_number"],
    })
    assert response.status_code == 200, response.text
    bet = response.json()["bet"]
    assert bet["picks"] == [4, 9, 33] and bet["status"] == "pending" and bet["ticket_balance"] == 70
    assert bet["max_win"] == 360.0 and bet["server_seed_hash"] == draw.server_seed_hash

    mine = (await client.get("/agent/api/keno/shared/my-bets")).json()["bets"]
    assert [b["bet_id"] for b in mine] == [bet["bet_id"]]
    assert (await client.get(f"/agent/api/keno/shared/bets/{bet['bet_id']}")).status_code == 200

    other = await make_agent()
    other_client = await make_client()
    await _login(other_client, other)
    assert (await other_client.get(f"/agent/api/keno/shared/bets/{bet['bet_id']}")).status_code == 404

    code = KenoService.shared_screen_code(agent.id)
    screen = (await client.get(f"/keno/api/salle/{code}")).json()["state"]
    assert screen["tickets"][-1]["picks"] == [4, 9, 33] and ticket["ticket_number"] not in json.dumps(screen)


@pytest.mark.asyncio
async def test_client_cannot_send_game_values(make_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user(balance=Decimal("100"))
    draw = await _draw(db_session, fake_redis)
    client = await make_client()
    await _login(client, agent)
    response = await client.post("/agent/api/keno/shared/bet", headers=await _token(client), json={
        "draw_id": draw.id, "picks": [1], "stake": 10, "player_type": "account", "identifier": player.phone, "multiplier": 999,
    })
    assert response.status_code == 400
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("100")


@pytest.mark.asyncio
async def test_shared_bet_requires_csrf(make_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user(balance=Decimal("100"))
    draw = await _draw(db_session, fake_redis)
    client = await make_client()
    await _login(client, agent)
    response = await client.post("/agent/api/keno/shared/bet", json={
        "draw_id": draw.id, "picks": [1], "stake": 10, "player_type": "account", "identifier": player.phone,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("100")


@pytest.mark.asyncio
async def test_closed_draw_refuses_tickets(make_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user(balance=Decimal("100"))
    draw = await _draw(db_session, fake_redis)
    draw.draw_time = now_utc() + timedelta(seconds=3)
    await db_session.flush()
    client = await make_client()
    await _login(client, agent)
    response = await client.post("/agent/api/keno/shared/bet", headers=await _token(client), json={
        "draw_id": draw.id, "picks": [1], "stake": 10, "player_type": "account", "identifier": player.phone,
    })
    assert response.status_code == 400 and "fermés" in response.json()["detail"]


@pytest.mark.asyncio
async def test_public_screens(make_client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    draw = await _draw(db_session, fake_redis)
    client = await make_client()
    hall = await client.get("/keno/salle")
    assert hall.status_code == 200 and "keno_live.js" in hall.text
    code = KenoService.shared_screen_code(agent.id)
    assert (await client.get("/keno/salle", params={"c": code})).status_code == 200
    assert (await client.get("/keno/salle", params={"c": "<x>"})).status_code == 404
    live = (await client.get("/keno/api/live")).json()
    assert live["draw"]["draw_id"] == draw.id and live["draw"]["draw_order"] is None and "history" in live


@pytest.mark.asyncio
async def test_instant_page_still_available_as_option(make_client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    client = await make_client()
    await fake_redis.set(CONFIG_KEY, json.dumps(keno_engine.validate_config({"mode": "instant"})))
    await _login(client, agent)
    page = await client.get("/agent/keno")
    assert page.status_code == 200 and "keno_live.js" not in page.text
