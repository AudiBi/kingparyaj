# tests/test_api/test_horse_races_api.py
"""API Horse Races : joueur (/api/v1/horse-races), agent (/agent/api/...),
admin (/admin/api/...) — authentification, CSRF, refus des champs envoyés
par le client (cote, résultat), cloisonnement des paris."""

import re
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1.horse_races import router as player_router
from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_user
from app.models.enums import UserRole
from app.routes.agent import router as agent_panel_router
from app.routes.horse_races import admin_router, agent_router
from app.services.horse_race_service import HorseRaceService
from app.services.wallet_service import WalletService


@pytest_asyncio.fixture
async def make_client(db_session, fake_redis):
    clients = []

    async def _make(current_user=None):
        app = FastAPI()
        app.include_router(player_router, prefix="/api/v1")
        app.include_router(agent_panel_router)
        app.include_router(agent_router)
        app.include_router(admin_router)
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


async def _open_race(db_session, fake_redis):
    service = HorseRaceService(db_session, fake_redis)
    race = await service.create_race()
    await db_session.flush()
    return race


# ========== Joueur ==========

@pytest.mark.asyncio
async def test_current_race_is_public_and_hides_result(make_client, db_session, fake_redis):
    race = await _open_race(db_session, fake_redis)
    client = await make_client()
    response = await client.get("/api/v1/horse-races/races/current")
    assert response.status_code == 200
    data = response.json()["race"]
    assert data["race_id"] == race.id and len(data["participants"]) == 6
    assert response.json()["betting_race"]["race_id"] == race.id
    assert data["results"] is None and data["server_seed"] is None

    response = await client.get(f"/api/v1/horse-races/races/{race.id}/result")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_players_cannot_bet_online(make_client, db_session, fake_redis, make_user):
    """Règle métier : pas de pari en ligne, uniquement au guichet de l'agent."""
    user = await make_user(balance=Decimal("500"))
    race = await _open_race(db_session, fake_redis)
    client = await make_client(current_user=user)
    response = await client.post("/api/v1/horse-races/bets", json={
        "race_id": race.id, "bet_type": "WIN", "selection": [10], "stake": "10",
    })
    assert response.status_code in (404, 405)
    assert await WalletService(db_session, fake_redis).get_balance(user.id) == Decimal("500")


@pytest.mark.asyncio
async def test_player_follows_bet_placed_by_agent(make_client, db_session, fake_redis, make_user, make_agent):
    agent = await make_agent()
    owner = await make_user(balance=Decimal("500"))
    other = await make_user(balance=Decimal("500"))
    race = await _open_race(db_session, fake_redis)
    bet = await HorseRaceService(db_session, fake_redis).place_bet(race.id, "EXACTA", [10, 7], "50", user_id=owner.id, agent_id=agent.id)

    owner_client = await make_client(current_user=owner)
    history = await owner_client.get("/api/v1/horse-races/bets/history")
    assert [b["bet_id"] for b in history.json()["bets"]] == [bet.id]
    assert (await owner_client.get(f"/api/v1/horse-races/bets/{bet.id}")).status_code == 200

    other_client = await make_client(current_user=other)
    assert (await other_client.get(f"/api/v1/horse-races/bets/{bet.id}")).status_code == 404

    public = await make_client()
    found = await public.get("/api/v1/horse-races/lookup", params={"code": bet.id})
    assert found.status_code == 200
    item = found.json()["bets"][0]
    assert item["selection_names"] == ["Messi", "Ronaldo"] and item["race_status"] == "BETTING_OPEN"
    assert "user_id" not in item  # aucune donnée personnelle


@pytest.mark.asyncio
async def test_quote_returns_server_odds(make_client, db_session, fake_redis):
    race = await _open_race(db_session, fake_redis)
    client = await make_client()
    response = await client.post("/api/v1/horse-races/quote", json={
        "race_id": race.id, "bet_type": "TRIFECTA", "selection": [10, 7, 11],
    })
    assert response.status_code == 200
    assert response.json()["odds"] > 10


@pytest.mark.asyncio
async def test_public_pages_render(make_client, db_session, fake_redis):
    from app.routes.horse_races import public_router

    client = await make_client()
    client._transport.app.include_router(public_router)
    screen = await client.get("/horse-races/ecran")
    assert screen.status_code == 200 and "raceCanvas" in screen.text and "/static/js/horse_races.js" in screen.text
    lookup = await client.get("/horse-races/mon-pari", params={"code": "<script>"})
    assert lookup.status_code == 200 and "<script>\"" not in lookup.text and "&lt;script&gt;" in lookup.text


# ========== Agent (cookie + CSRF) ==========

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


async def _csrf_headers(client):
    page = await client.get("/agent/login")
    return {"X-CSRFToken": _csrf(page.text)}


@pytest.mark.asyncio
async def test_agent_places_ticket_bet(make_client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    race = await _open_race(db_session, fake_redis)
    client = await make_client()
    await _agent_login(client, agent)

    response = await client.post(
        "/agent/api/horse-races/bet",
        json={"race_id": race.id, "bet_type": "PLACE", "selection": [10, 9], "stake": "40",
              "player_type": "ticket", "identifier": ticket["ticket_number"]},
        headers=await _csrf_headers(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["bet"]["selection"] == [9, 10]


@pytest.mark.asyncio
async def test_agent_page_quote_and_my_bets(make_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user(balance=Decimal("300"))
    race = await _open_race(db_session, fake_redis)
    client = await make_client()
    await _agent_login(client, agent)

    page = await client.get("/agent/horse-races")
    assert page.status_code == 200 and "raceCanvas" in page.text and "Horse Races" in page.text

    quote = await client.get("/agent/api/horse-races/quote", params={"race_id": race.id, "bet_type": "EXACTA", "selection": "10,7"})
    assert quote.status_code == 200 and quote.json()["selection"] == [10, 7]

    token = (await client.get("/agent/api/horse-races/csrf-token")).json()["csrf_token"]
    response = await client.post(
        "/agent/api/horse-races/bet",
        json={"race_id": race.id, "bet_type": "WIN", "selection": [10], "stake": "100",
              "player_type": "account", "identifier": player.phone},
        headers={"X-CSRFToken": token},
    )
    assert response.status_code == 200, response.text
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("200")

    mine = await client.get(f"/agent/api/horse-races/races/{race.id}/my-bets")
    assert len(mine.json()["bets"]) == 1

    state = (await client.get("/agent/api/horse-races/current")).json()
    assert state["betting_race"]["race_id"] == race.id


@pytest.mark.asyncio
async def test_agent_bet_without_csrf_token_is_refused(make_client, db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user(balance=Decimal("100"))
    race = await _open_race(db_session, fake_redis)
    client = await make_client()
    await _agent_login(client, agent)

    response = await client.post(
        "/agent/api/horse-races/bet",
        json={"race_id": race.id, "bet_type": "WIN", "selection": [10], "stake": "10",
              "player_type": "account", "identifier": player.phone},
        follow_redirects=False,
    )
    assert response.status_code == 303  # rejet CSRF (redirection), aucun débit
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("100")


@pytest.mark.asyncio
async def test_agent_routes_require_agent_login(make_client, db_session, fake_redis):
    client = await make_client()
    response = await client.get("/agent/api/horse-races/current", follow_redirects=False)
    assert response.status_code in (303, 401, 403)


# ========== Admin ==========

@pytest.mark.asyncio
async def test_admin_routes_refuse_agents(make_client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    client = await make_client()
    await _agent_login(client, agent)  # cookie agent valide, mais pas admin
    response = await client.get("/admin/api/horse-races/races", follow_redirects=False)
    assert response.status_code in (303, 401, 403)


@pytest.mark.asyncio
async def test_admin_creates_then_cancels_race(make_client, db_session, fake_redis, admin_user, make_user):
    from app.core.security import get_current_admin
    from app.routes.admin import router as admin_panel_router

    player = await make_user(balance=Decimal("100"))
    client = await make_client()
    app = client._transport.app
    app.include_router(admin_panel_router)
    app.dependency_overrides[get_current_admin] = lambda: admin_user

    async def admin_csrf():
        page = await client.get("/admin/login")
        return {"X-CSRFToken": _csrf(page.text)}

    response = await client.post("/admin/api/horse-races/races", json={"open_betting": True}, headers=await admin_csrf())
    assert response.status_code == 200, response.text
    race_id = response.json()["race"]["race_id"]

    await HorseRaceService(db_session, fake_redis).place_bet(race_id, "WIN", [10], "60", user_id=player.id, agent_id="agent-guichet")
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("40")

    response = await client.post(
        f"/admin/api/horse-races/races/{race_id}/cancel", json={"reason": "Test annulation"}, headers=await admin_csrf()
    )
    assert response.status_code == 200, response.text
    assert response.json()["refunded_bets"] == 1
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("100")

    page = await client.get("/admin/games/horse-races")
    assert page.status_code == 200 and "configForm" in page.text

    detail = await client.get(f"/admin/api/horse-races/races/{race_id}")
    assert detail.json()["status"] == "CANCELLED"
    assert detail.json()["server_seed"]  # seed révélé après annulation


@pytest.mark.asyncio
async def test_counter_screen_shows_ticket_entry_then_result(make_client, db_session, fake_redis, make_agent, make_ticket, monkeypatch):
    """Écran du joueur au guichet : ticket en saisie (type, chevaux, mise, cote,
    gain potentiel calculés par le serveur), ticket enregistré, puis statut
    (gagné / perdu) après l'arrivée — sans donnée d'identité."""
    import json

    import app.api.websockets.manager as manager
    from app.routes.horse_races import public_router
    from app.services.horse_race_service import HorseRaceService

    sent = []

    async def fake_publish(message, draw_id="all"):
        sent.append((draw_id, message))

    monkeypatch.setattr(manager, "publish", fake_publish)
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    race = await _open_race(db_session, fake_redis)
    client = await make_client()
    await _agent_login(client, agent)
    code = HorseRaceService.screen_code(agent.id)
    assert f"/horse-races/guichet?c={code}" in (await client.get("/agent/horse-races")).text

    preview = await client.post("/agent/api/horse-races/screen", headers=await _csrf_headers(client),
                                json={"race_id": race.id, "bet_type": "exacta", "selection": [10, 7, 7, 99], "stake": "50"})
    assert preview.status_code == 200, preview.text
    channel, message = [m for m in sent if m[1]["type"] == "hr_screen"][-1]
    assert channel == f"hr-screen-{code}"
    p = message["data"]["preview"]
    assert p["bet_type"] == "EXACTA" and [h["number"] for h in p["selection"]] == [10, 7]
    assert p["selection"][0]["name"] == "Messi" and p["odds"] > 1
    assert p["potential_payout"] == float((Decimal("50") * Decimal(str(p["odds"]))).quantize(Decimal("0.01")))

    bet = await client.post("/agent/api/horse-races/bet", headers=await _csrf_headers(client), json={
        "race_id": race.id, "bet_type": "WIN", "selection": [10], "stake": "40",
        "player_type": "ticket", "identifier": ticket["ticket_number"]})
    assert bet.status_code == 200, bet.text
    state = [m for m in sent if m[1]["type"] == "hr_screen"][-1][1]["data"]
    assert state["event"] == "ticket" and state["preview"] is None
    assert state["last_ticket"]["bet_id"] == bet.json()["bet"]["bet_id"] and state["last_ticket"]["status"] == "PENDING"
    assert ticket["ticket_number"] not in json.dumps(state)

    service = HorseRaceService(db_session, fake_redis)
    await service.close_betting(race.id)
    await service.start_race(race.id)
    await service.finish_race(race.id)
    await service.settle_race(race.id)
    await db_session.commit()

    app = FastAPI()
    app.include_router(public_router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_redis] = lambda: fake_redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as pub:
        assert (await pub.get(f"/horse-races/guichet?c={code}")).status_code == 200
        st = (await pub.get(f"/horse-races/api/guichet/{code}")).json()["state"]
        assert st["tickets"][0]["status"] in ("WON", "LOST")
        assert (await pub.get("/horse-races/guichet?c=xyz")).status_code == 404
