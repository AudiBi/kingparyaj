# tests/test_api/test_lucky6_api.py
"""Lucky6 : API publique en lecture seule, panel agent (cookie + CSRF), admin,
écrans (salle et guichet). Aucune valeur de jeu acceptée du navigateur."""

import re
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.lucky6 import router as api_router
from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.routes.agent import router as agent_panel_router
from app.routes.lucky6 import admin_router, agent_router, public_router
from app.services import lucky6_engine as engine
from app.services.lucky6_service import Lucky6Service
from app.services.wallet_service import WalletService

PICKS = [3, 11, 17, 24, 30, 45]


@pytest_asyncio.fixture
async def make_client(db_session, fake_redis):
    clients = []

    async def _make():
        app = FastAPI()
        app.include_router(api_router, prefix="/api/v1")
        app.include_router(agent_panel_router)
        app.include_router(agent_router)
        app.include_router(admin_router)
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


async def _agent_login(client, agent):
    page = await client.get("/agent/login")
    response = await client.post(
        "/agent/login",
        data={"code": agent.phone, "password": "AgentPass123!", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def _token(client):
    return {"X-CSRFToken": (await client.get("/agent/api/lucky6/csrf-token")).json()["csrf_token"]}


async def _round(db_session, fake_redis):
    race = await Lucky6Service(db_session, fake_redis).create_race()
    await db_session.flush()
    return race


# ========== Public ==========

@pytest.mark.asyncio
async def test_public_api_is_read_only_and_hides_draw(make_client, db_session, fake_redis):
    race = await _round(db_session, fake_redis)
    client = await make_client()
    config = (await client.get("/api/v1/lucky6/config")).json()
    assert config["picks"] == 6 and config["drawn_count"] == 35 and len(config["paytable"]) == 30
    current = (await client.get("/api/v1/lucky6/rounds/current")).json()
    assert current["betting_race"]["race_id"] == race.id and current["race"]["balls"] is None
    assert (await client.get(f"/api/v1/lucky6/rounds/{race.id}/verify")).status_code == 409
    assert (await client.post("/api/v1/lucky6/bets", json={})).status_code in (404, 405)


@pytest.mark.asyncio
async def test_public_screens_render(make_client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    client = await make_client()
    hall = await client.get("/lucky6/ecran")
    assert hall.status_code == 200 and "/static/js/lucky6.js" in hall.text and "LUCKY" in hall.text
    code = Lucky6Service.screen_code(agent.id)
    counter = await client.get("/lucky6/ecran", params={"c": code})
    assert counter.status_code == 200 and code in counter.text
    assert (await client.get("/lucky6/ecran", params={"c": "<script>"})).status_code == 404
    live = (await client.get("/lucky6/api/live")).json()
    assert "history" in live and "server_time" in live
    verify = await client.get("/lucky6/verifier", params={"manche": "<b>x</b>"})
    assert verify.status_code == 200 and "&lt;b&gt;" in verify.text


# ========== Agent ==========

@pytest.mark.asyncio
async def test_agent_full_flow_with_ticket(make_client, db_session, fake_redis, make_agent, make_ticket, monkeypatch):
    import app.api.websockets.manager as manager

    sent = []

    async def fake_publish(message, draw_id="all"):
        sent.append((draw_id, message))

    monkeypatch.setattr(manager, "publish", fake_publish)
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    race = await _round(db_session, fake_redis)
    client = await make_client()
    await _agent_login(client, agent)

    page = await client.get("/agent/lucky6")
    assert page.status_code == 200 and 'id="picker"' in page.text and "Lucky6" in page.text

    state = (await client.get("/agent/api/lucky6/state")).json()
    assert state["betting_race"]["race_id"] == race.id and state["config"]["parity_odds"] == 1.9

    pick = (await client.get("/agent/api/lucky6/quick-pick")).json()["numbers"]
    assert len(set(pick)) == 6 and pick == sorted(pick) and all(1 <= n <= 48 for n in pick)

    preview = await client.post("/agent/api/lucky6/screen", headers=await _token(client),
                                json={"race_id": race.id, "bet_type": "SIX", "selection": [3, 11, 99, 3], "stake": 20})
    assert preview.status_code == 200, preview.text
    channel, message = [m for m in sent if m[1]["type"] == "l6_screen"][-1]
    assert channel == f"l6-screen-{Lucky6Service.screen_code(agent.id)}"
    assert message["data"]["preview"]["selection"] == [3, 11] and message["data"]["preview"]["potential_payout"] == 200000

    response = await client.post("/agent/api/lucky6/bet", headers=await _token(client), json={
        "race_id": race.id, "bet_type": "SIX", "selection": PICKS, "stake": "20",
        "player_type": "ticket", "identifier": ticket["ticket_number"],
    })
    assert response.status_code == 200, response.text
    bet = response.json()["bet"]
    assert bet["selection"] == PICKS and bet["odds"] == 10000 and bet["server_seed_hash"] == race.server_seed_hash

    mine = (await client.get("/agent/api/lucky6/my-bets")).json()["bets"]
    assert [b["bet_id"] for b in mine] == [bet["bet_id"]]
    receipt = await client.get(f"/agent/api/lucky6/bets/{bet['bet_id']}")
    assert receipt.status_code == 200

    other = await make_agent()
    other_client = await make_client()
    await _agent_login(other_client, other)
    assert (await other_client.get(f"/agent/api/lucky6/bets/{bet['bet_id']}")).status_code == 404

    screen = (await client.get(f"/lucky6/api/guichet/{Lucky6Service.screen_code(agent.id)}")).json()["state"]
    assert screen["tickets"][-1]["selection"] == PICKS
    assert "ticket_number" not in str(screen) and ticket["ticket_number"] not in str(screen)


@pytest.mark.asyncio
async def test_client_values_are_refused(make_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user(balance=Decimal("100"))
    race = await _round(db_session, fake_redis)
    client = await make_client()
    await _agent_login(client, agent)
    response = await client.post("/agent/api/lucky6/bet", headers=await _token(client), json={
        "race_id": race.id, "bet_type": "SIX", "selection": PICKS, "stake": "10", "odds": 99999,
        "player_type": "account", "identifier": player.phone,
    })
    assert response.status_code == 422
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("100")


@pytest.mark.asyncio
async def test_agent_bet_without_csrf_is_refused(make_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user(balance=Decimal("100"))
    race = await _round(db_session, fake_redis)
    client = await make_client()
    await _agent_login(client, agent)
    response = await client.post("/agent/api/lucky6/bet", json={
        "race_id": race.id, "bet_type": "FIRST_ODD", "selection": [], "stake": "10",
        "player_type": "account", "identifier": player.phone,
    }, follow_redirects=False)
    assert response.status_code == 303
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("100")


@pytest.mark.asyncio
async def test_agent_routes_require_login(make_client):
    client = await make_client()
    assert (await client.get("/agent/api/lucky6/state", follow_redirects=False)).status_code in (303, 401, 403)
    assert (await client.get("/admin/api/lucky6/config", follow_redirects=False)).status_code in (303, 401, 403)


# ========== Admin ==========

@pytest.mark.asyncio
async def test_admin_config_paytable_and_rounds(make_client, db_session, fake_redis, admin_user, make_user):
    from app.core.security import get_current_admin
    from app.routes.admin import router as admin_panel_router

    player = await make_user(balance=Decimal("100"))
    client = await make_client()
    app = client._transport.app
    app.include_router(admin_panel_router)
    app.dependency_overrides[get_current_admin] = lambda: admin_user

    async def csrf():
        page = await client.get("/admin/login")
        return {"X-CSRFToken": _csrf(page.text)}

    page = await client.get("/admin/games/lucky6")
    assert page.status_code == 200 and "paytable" in page.text

    meta = (await client.get("/admin/api/lucky6/config")).json()
    assert meta["rtp"] == pytest.approx(0.8506, abs=1e-4) and len(meta["rtp_table"]) == 30

    scaled = (await client.post("/admin/api/lucky6/paytable/preview", headers=await csrf(),
                                json={"paytable": engine.CLASSIC_SHAPE, "target_rtp": 0.9})).json()
    assert scaled["rtp"] <= 0.9

    config = dict(meta["config"], paytable=scaled["paytable"], parity_odds=1.85)
    saved = await client.put("/admin/api/lucky6/config", headers=await csrf(), json=config)
    assert saved.status_code == 200, saved.text
    too_high = await client.put("/admin/api/lucky6/config", headers=await csrf(), json=dict(config, paytable=engine.CLASSIC_SHAPE))
    assert too_high.status_code == 400 and "maximum autorisé" in too_high.json()["detail"]

    created = await client.post("/admin/api/lucky6/rounds", headers=await csrf(), json={"open_betting": True})
    assert created.status_code == 200, created.text
    rid = created.json()["round"]["race_id"]
    assert created.json()["round"]["parity_odds"] == 1.85

    await Lucky6Service(db_session, fake_redis).place_bet(rid, "SIX", PICKS, "40", user_id=player.id, agent_id="agent-guichet")
    rounds = (await client.get("/admin/api/lucky6/rounds")).json()["rounds"]
    assert rounds[0]["total_bets"] == 1 and rounds[0]["total_stake"] == 40

    cancelled = await client.post(f"/admin/api/lucky6/rounds/{rid}/cancel", headers=await csrf(), json={"reason": "Test"})
    assert cancelled.status_code == 200 and cancelled.json()["refunded_bets"] == 1
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("100")
    detail = (await client.get(f"/admin/api/lucky6/rounds/{rid}")).json()
    assert detail["status"] == "CANCELLED" and detail["bets"][0]["status"] == "REFUNDED"


@pytest.mark.asyncio
async def test_admin_draws_and_verifies(make_client, db_session, fake_redis, admin_user):
    from app.core.security import get_current_admin
    from app.routes.admin import router as admin_panel_router

    client = await make_client()
    app = client._transport.app
    app.include_router(admin_panel_router)
    app.dependency_overrides[get_current_admin] = lambda: admin_user

    async def csrf():
        page = await client.get("/admin/login")
        return {"X-CSRFToken": _csrf(page.text)}

    rid = (await client.post("/admin/api/lucky6/rounds", headers=await csrf(), json={})).json()["round"]["race_id"]
    started = await client.post(f"/admin/api/lucky6/rounds/{rid}/start", headers=await csrf())
    assert started.status_code == 200 and len(started.json()["round"]["balls"]) == 35
    service = Lucky6Service(db_session, fake_redis)
    await service.finish_race(rid)
    settled = await client.post(f"/admin/api/lucky6/rounds/{rid}/settle", headers=await csrf())
    assert settled.status_code == 200
    verify = (await client.get(f"/api/v1/lucky6/rounds/{rid}/verify")).json()
    assert verify["verification"]["seed_hash_valid"] and verify["verification"]["result_valid"]
