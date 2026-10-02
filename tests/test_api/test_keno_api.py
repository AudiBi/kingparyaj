# tests/test_api/test_keno_api.py
"""Keno instantané par les routes : panel agent (/agent/api/keno/...), API
(/api/v1/keno/...) et administration (/admin/api/keno/...).
Les paris se prennent uniquement chez un agent ; rien n'est cru du client."""

import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.v1.keno import router as keno_router
from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_user
from app.core.timezone import now_utc
from app.models.enums import KenoBetStatus, KenoDrawStatus
from app.models.keno import KenoBet, KenoDraw
from app.models.ticket import Ticket
from app.routes.admin import router as admin_panel_router
from app.routes.agent import router as agent_panel_router
from app.services import keno_engine
from app.services.keno_service import KenoService
from app.services.wallet_service import WalletService


@pytest_asyncio.fixture(autouse=True)
async def _instant_mode(fake_redis):
    """Ces tests couvrent l'option « instantané » (le mode par défaut est partagé)."""
    import json as _json

    from app.services import keno_engine as _engine

    await fake_redis.set("settings:keno", _json.dumps(_engine.validate_config({"mode": "instant"})))


@pytest_asyncio.fixture
async def make_client(db_session, fake_redis):
    clients = []

    async def _make(current_user=None):
        app = FastAPI()
        app.include_router(keno_router, prefix="/api/v1")
        app.include_router(agent_panel_router)
        app.include_router(admin_panel_router)
        app.add_middleware(AdminCsrfMiddleware)

        async def _db():
            yield db_session

        async def _redis():
            return fake_redis

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_redis] = _redis
        if current_user is not None:
            app.dependency_overrides[get_current_user] = lambda: current_user
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


async def _agent_session(client, agent):
    page = await client.get("/agent/login")
    response = await client.post(
        "/agent/login",
        data={"code": agent.phone, "password": "AgentPass123!", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/agent/login")
    return {"X-CSRFToken": _csrf(page.text)}


async def _prepared(client):
    state = await client.get("/agent/api/keno/state")
    assert state.status_code == 200, state.text
    return state.json()["prepared"]


def _ticket_number(ticket):
    return ticket["ticket_number"]


@pytest.mark.asyncio
async def test_agent_page_shows_prepared_draw_hash(make_client, db_session, make_agent):
    agent = await make_agent()
    client = await make_client()
    await _agent_session(client, agent)
    page = await client.get("/agent/keno")
    assert page.status_code == 200
    prepared = await _prepared(client)
    assert prepared["server_seed_hash"] in page.text
    assert "&#34;" not in page.text  # données JSON intactes dans le script


@pytest.mark.asyncio
async def test_agent_plays_instant_ticket_end_to_end(make_client, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    client = await make_client()
    headers = await _agent_session(client, agent)
    prepared = await _prepared(client)
    draw = await db_session.get(KenoDraw, prepared["draw_id"])
    expected = keno_engine.draw_numbers(draw.server_seed, draw.draw_number)

    response = await client.post("/agent/api/keno/bet", headers=headers, json={
        "player_type": "ticket", "identifier": _ticket_number(ticket), "draw_id": prepared["draw_id"],
        "picks": [expected[0]], "stake": 40,
        # champs envoyés par un client malveillant : ignorés
        "payout": 50000, "multiplier": 999, "winning_numbers": [1, 2, 3],
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["winning_numbers"] == expected
    assert data["payout"] == 100.0 and data["multiplier"] == 2.5  # 1 joué / 1 trouvé
    assert data["server_seed"] and data["next"]["draw_id"] != prepared["draw_id"]
    row = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == _ticket_number(ticket)))).scalar_one()
    assert row.balance == Decimal("160") == Decimal(str(data["ticket_balance"]))

    again = await client.post("/agent/api/keno/bet", headers=headers, json={
        "player_type": "ticket", "identifier": _ticket_number(ticket), "draw_id": prepared["draw_id"], "picks": [1], "stake": 10,
    })
    assert again.status_code == 400 and "déjà été joué" in again.text

    receipt = await client.get(f"/agent/api/keno/bets/{data['bet_id']}")
    assert receipt.status_code == 200 and receipt.json()["payout"] == 100.0
    history = await client.get("/agent/api/keno/history")
    assert history.json()["items"][0]["bet_id"] == data["bet_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("picks,stake", [
    ([0, 5], 40), ([81], 40), ([5, 5, 6], 40), (list(range(1, 12)), 40),
    ([1, 2], 5), ([1, 2], 999999), ([1, 2], "abc"), ("1,2", 40),
])
async def test_agent_ticket_is_validated_by_the_server(make_client, db_session, make_agent, make_ticket, picks, stake):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    client = await make_client()
    headers = await _agent_session(client, agent)
    prepared = await _prepared(client)

    response = await client.post("/agent/api/keno/bet", headers=headers, json={
        "player_type": "ticket", "identifier": _ticket_number(ticket), "draw_id": prepared["draw_id"], "picks": picks, "stake": stake,
    })
    assert response.status_code in (400, 422), response.text
    row = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == _ticket_number(ticket)))).scalar_one()
    assert row.balance == Decimal("100")
    assert (await db_session.execute(select(KenoBet))).scalars().first() is None


@pytest.mark.asyncio
async def test_agent_ticket_for_player_account(make_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user(balance=Decimal("200"))
    client = await make_client()
    headers = await _agent_session(client, agent)
    prepared = await _prepared(client)
    response = await client.post("/agent/api/keno/bet", headers=headers, json={
        "player_type": "account", "identifier": player.phone, "draw_id": prepared["draw_id"], "picks": [1, 2, 3], "stake": 50,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("150") + Decimal(str(data["payout"]))
    bet = (await db_session.execute(select(KenoBet))).scalar_one()
    assert bet.agent_id == agent.id and bet.user_id == player.id


@pytest.mark.asyncio
async def test_agent_cannot_reprint_another_agents_receipt(make_client, db_session, make_agent, make_ticket):
    agent, other = await make_agent(), await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    client = await make_client()
    headers = await _agent_session(client, agent)
    prepared = await _prepared(client)
    data = (await client.post("/agent/api/keno/bet", headers=headers, json={
        "player_type": "ticket", "identifier": _ticket_number(ticket), "draw_id": prepared["draw_id"], "picks": [1], "stake": 10,
    })).json()

    other_client = await make_client()
    await _agent_session(other_client, other)
    assert (await other_client.get(f"/agent/api/keno/bets/{data['bet_id']}")).status_code == 404


@pytest.mark.asyncio
async def test_agent_ticket_requires_csrf(make_client, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    client = await make_client()
    await _agent_session(client, agent)
    prepared = await _prepared(client)
    response = await client.post("/agent/api/keno/bet", json={
        "player_type": "ticket", "identifier": _ticket_number(ticket), "draw_id": prepared["draw_id"], "picks": [1], "stake": 10,
    }, follow_redirects=False)
    assert response.status_code in (303, 400, 403)  # le middleware CSRF refuse (redirection)
    assert (await db_session.execute(select(KenoBet))).scalars().first() is None


@pytest.mark.asyncio
async def test_quick_pick_returns_valid_selection(make_client, make_agent):
    agent = await make_agent()
    client = await make_client()
    await _agent_session(client, agent)
    for count in (1, 5, 10):
        picks = (await client.get(f"/agent/api/keno/quick-pick?count={count}")).json()["picks"]
        assert len(picks) == len(set(picks)) == count and all(1 <= n <= 80 for n in picks)
    assert (await client.get("/agent/api/keno/quick-pick?count=11")).status_code == 422


@pytest.mark.asyncio
async def test_players_cannot_bet_online(make_client, db_session, fake_redis, make_user):
    player = await make_user(balance=Decimal("100"))
    draw = await KenoService(db_session, fake_redis).prepare_instant_draw("agent-x")
    client = await make_client(current_user=player)
    for path, body in (("/api/v1/keno/bets", {"draw_id": draw.id, "picks": [1], "stake": 20}),
                       ("/api/v1/keno/quick-pick", {"draw_id": draw.id, "numbers_count": 3, "stake": 20})):
        response = await client.post(path, json=body)
        assert response.status_code == 403 and "agent" in response.text
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("100")


@pytest.mark.asyncio
async def test_public_config_and_verification(make_client, db_session, fake_redis, make_user):
    client = await make_client()
    config = (await client.get("/api/v1/keno/config")).json()
    assert config["paytable"]["1"] == {"1": 2.5} and config["rtp"]["1"] == 62.5

    service = KenoService(db_session, fake_redis)
    draw = await service.prepare_instant_draw("agent-1")
    pending = (await client.get(f"/api/v1/keno/draws/{draw.id}/verify")).json()
    assert pending["verifiable"] is False and "server_seed" not in pending  # seed secret avant le tirage

    user = await make_user(balance=Decimal("100"))
    await service.play_instant(draw.id, [1], 10, agent_id="agent-1", user_id=user.id)
    done = (await client.get(f"/api/v1/keno/draws/{draw.id}/verify")).json()
    assert done["verifiable"] and done["seed_hash_valid"] and done["result_valid"]


@pytest.mark.asyncio
async def test_admin_config_api_validates_and_applies(make_client, db_session, fake_redis, admin_user):
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    client = await make_client()
    page = await client.get("/admin/login")
    login = await client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": _csrf(page.text)},
                              follow_redirects=False)
    assert login.status_code == 303
    headers = {"X-CSRFToken": _csrf((await client.get("/admin/login")).text)}

    preview = await client.post("/admin/api/keno/paytable/preview", headers=headers,
                                json={"paytable": keno_engine.DEFAULT_CONFIG["paytable"], "target_rtp": 85})
    assert preview.status_code == 200, preview.text
    assert all(v <= 85 for v in preview.json()["rtp"].values())

    bad = await client.put("/admin/api/keno/config", headers=headers, json={"max_picks": 15})
    assert bad.status_code == 400 and "Numéros" in bad.text

    ok = await client.put("/admin/api/keno/config", headers=headers,
                          json={"min_bet": 20, "stake_options": [20, 50], "paytable": preview.json()["paytable"]})
    assert ok.status_code == 200, ok.text
    config = await KenoService(db_session, fake_redis).get_config()
    assert config["min_bet"] == 20.0 and config["paytable"]["1"] == {"1": "3.4"}

    page = await client.get("/admin/games/keno/config")
    assert page.status_code == 200 and "Taux de redistribution" in page.text


@pytest.mark.asyncio
async def test_player_screen_shows_ticket_then_the_draw(make_client, db_session, fake_redis, make_agent, make_ticket, monkeypatch):
    """Écran du joueur : numéros demandés et mise pendant la saisie, puis le
    tirage (ordre de sortie des 20 numéros) — sans aucune donnée d'identité."""
    import app.api.websockets.manager as manager
    from app.routes.keno import public_router

    sent = []

    async def fake_publish(message, draw_id="all"):
        sent.append((draw_id, message))

    monkeypatch.setattr(manager, "publish", fake_publish)
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    client = await make_client()
    headers = await _agent_session(client, agent)
    page = await client.get("/agent/keno")
    code = KenoService.screen_code(agent.id)
    assert f"/keno/ecran?c={code}" in page.text

    preview = await client.post("/agent/api/keno/screen", headers=headers, json={"picks": [9, 3, 3, 99, 0], "stake": 50})
    assert preview.status_code == 200
    channel, message = sent[-1]
    assert channel == f"keno-screen-{code}" and message["type"] == "keno_screen"
    state = message["data"]
    assert state["event"] == "preview" and state["picks"] == [3, 9] and state["stake"] == 50.0
    assert state["max_win"] == 300.0  # 2 numéros : x6

    prepared = await _prepared(client)
    played = (await client.post("/agent/api/keno/bet", headers=headers, json={
        "player_type": "ticket", "identifier": ticket["ticket_number"], "draw_id": prepared["draw_id"], "picks": [3, 9], "stake": 50,
    })).json()
    state = sent[-1][1]["data"]
    assert state["event"] == "play" and state["draw_order"] == played["draw_order"] and state["payout"] == played["payout"]
    assert ticket["ticket_number"] not in json.dumps(state)
    assert state["seq"] > preview.json()["seq"]

    # page et état publics de l'écran
    app = FastAPI()
    app.include_router(public_router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_redis] = lambda: fake_redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as pub:
        assert (await pub.get(f"/keno/ecran?c={code}")).status_code == 200
        assert (await pub.get(f"/keno/api/ecran/{code}")).json()["state"]["event"] == "play"
        assert (await pub.get("/keno/ecran?c=nimportequoi")).status_code == 404


@pytest.mark.asyncio
async def test_screen_preview_requires_agent_and_csrf(make_client, make_agent):
    client = await make_client()
    assert (await client.post("/agent/api/keno/screen", json={"picks": [1]}, follow_redirects=False)).status_code in (303, 401, 403)
