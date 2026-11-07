# tests/test_api/test_admin_agents_list.py
"""Liste des agents du panel admin : filtres, pages, création, modification, activation, réinitialisation du
mot de passe, suppression, actions groupées, export CSV et commission par défaut."""

import csv
import re
from decimal import Decimal
from io import StringIO

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import verify_password
from app.core.session_expired import session_expired_handler
from app.core.timezone import now_utc
from app.models.bureau import CashierSession
from app.models.enums import UserRole
from app.models.user import User
from app.routes.admin import router as admin_router
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token"\s+value="([^"]*)"', html).group(1)


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
        await c.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": _csrf(page.text)})
        yield c


async def _headers(client):
    return {"X-CSRFToken": _csrf((await client.get("/admin/login")).text)}


async def _commit(db_session):
    await db_session.commit()  # les routes de l'API tournent dans la même session de test


async def _open_session(db_session, agent):
    db_session.add(CashierSession(bureau_id=agent.bureau_id, agent_id=agent.id, starting_balance=Decimal("100"),
                                  current_balance=Decimal("100"), expected_balance=Decimal("100"), status="OPEN", opened_at=now_utc()))
    await db_session.commit()


# ======================================================================== liste, filtres, pages

@pytest.mark.asyncio
async def test_list_pages_and_filters(client, db_session, make_agent, make_user, make_bureau):
    bureau_a, bureau_b = await make_bureau(), await make_bureau()
    marie = await make_agent(bureau=bureau_a)
    marie.first_name = "MarieAgent"
    paul = await make_agent(bureau=bureau_b)
    paul.first_name, paul.is_active = "PaulAgent", False
    player = await make_user()
    player.first_name = "JoueurSimple"
    await _commit(db_session)

    def names(html):
        return {n for n in ("MarieAgent", "PaulAgent", "JoueurSimple") if n in html}

    assert names((await client.get("/admin/agents")).text) == {"MarieAgent", "PaulAgent"}  # jamais les joueurs
    assert names((await client.get("/admin/agents?search=MarieAgent")).text) == {"MarieAgent"}
    assert names((await client.get(f"/admin/agents?bureau_id={bureau_b.id}")).text) == {"PaulAgent"}
    assert names((await client.get("/admin/agents?status=inactive")).text) == {"PaulAgent"}
    assert names((await client.get("/admin/agents?status=active")).text) == {"MarieAgent"}
    for query in ("page=999", "per_page=100", "bureau_id=inconnu", "status=zzz"):
        assert (await client.get(f"/admin/agents?{query}")).status_code == 200, query
    for path in (f"/admin/agents/{marie.id}", f"/admin/agents/{marie.id}/edit", f"/admin/agents/{marie.id}/sessions", "/admin/agents/create"):
        assert (await client.get(path)).status_code == 200, path
    assert (await client.get("/admin/agents/inconnu")).status_code == 404


# ======================================================================== création et modification

@pytest.mark.asyncio
async def test_create_agent_and_duplicates(client, db_session, make_bureau):
    bureau = await make_bureau()
    body = {"phone": "51234567", "password": "Secret123!", "first_name": "Nouvel", "last_name": "Agent",
            "email": "nouvel@example.com", "bureau_id": bureau.id}
    r = await client.post("/admin/api/agents", headers=await _headers(client), json=body)
    assert r.status_code == 200 and r.json()["success"] is True, r.text
    created = await db_session.get(User, r.json()["agent_id"])
    assert created.role == UserRole.AGENT and created.bureau_id == bureau.id and created.is_active

    same_phone = await client.post("/admin/api/agents", headers=await _headers(client), json={**body, "email": "autre@example.com"})
    assert same_phone.status_code == 400 and "téléphone" in same_phone.json()["error"]
    same_email = await client.post("/admin/api/agents", headers=await _headers(client), json={**body, "phone": "41234567"})
    assert same_email.status_code == 400 and "email" in same_email.json()["error"]  # avant : erreur 500 de la base
    no_bureau = await client.post("/admin/api/agents", headers=await _headers(client),
                                  json={**body, "phone": "31234567", "email": "x@example.com", "bureau_id": "inconnu"})
    assert no_bureau.status_code == 404 and "Bureau" in no_bureau.json()["error"]


@pytest.mark.asyncio
async def test_update_agent(client, db_session, make_agent, make_user, make_bureau):
    agent = await make_agent()
    other = await make_agent()
    other.email = "pris@example.com"
    bureau = await make_bureau()
    await _commit(db_session)
    agent_id = agent.id

    r = await client.put(f"/admin/api/agents/{agent_id}", headers=await _headers(client),
                         json={"first_name": "Jean", "bureau_id": bureau.id, "commission_rate": "12.5"})
    assert r.status_code == 200, r.text
    await db_session.refresh(agent)
    assert agent.first_name == "Jean" and agent.bureau_id == bureau.id and agent.commission_rate == Decimal("12.5")

    taken = await client.put(f"/admin/api/agents/{agent_id}", headers=await _headers(client), json={"email": "pris@example.com"})
    assert taken.status_code == 400 and "email" in taken.json()["error"]  # avant : erreur 500
    bad_bureau = await client.put(f"/admin/api/agents/{agent_id}", headers=await _headers(client), json={"bureau_id": "inconnu"})
    assert bad_bureau.status_code == 404


@pytest.mark.asyncio
async def test_agent_routes_never_act_on_players_or_admins(client, db_session, admin_user, make_user):
    player = await make_user(balance=Decimal("500"))
    await _commit(db_session)
    player_id, admin_id = player.id, admin_user.id
    for target in (player_id, admin_id):
        h = await _headers(client)
        assert (await client.delete(f"/admin/api/agents/{target}", headers=h)).status_code == 404
        assert (await client.post(f"/admin/api/agents/{target}/toggle-status", headers=await _headers(client))).status_code == 404
        assert (await client.post(f"/admin/api/agents/{target}/reset-password", headers=await _headers(client))).status_code == 404
        assert (await client.put(f"/admin/api/agents/{target}", headers=await _headers(client), json={"first_name": "X"})).status_code == 404
    for u in (player, admin_user):
        await db_session.refresh(u)
    assert player.is_deleted is False and admin_user.is_deleted is False and player.first_name != "X"


# ======================================================================== activer, mot de passe, supprimer

@pytest.mark.asyncio
async def test_toggle_status(client, db_session, make_agent):
    agent = await make_agent()
    await _commit(db_session)
    r = await client.post(f"/admin/api/agents/{agent.id}/toggle-status", headers=await _headers(client))
    assert r.status_code == 200 and "désactivé" in r.json()["message"]
    await db_session.refresh(agent)
    assert agent.is_active is False
    r = await client.post(f"/admin/api/agents/{agent.id}/toggle-status", headers=await _headers(client))
    assert "activé" in r.json()["message"]
    await db_session.refresh(agent)
    assert agent.is_active is True


@pytest.mark.asyncio
async def test_reset_password_gives_the_admin_a_working_temporary_password(client, db_session, make_agent, caplog):
    agent = await make_agent()
    await _commit(db_session)
    with caplog.at_level("INFO"):
        r = await client.post(f"/admin/api/agents/{agent.id}/reset-password", headers=await _headers(client))
    assert r.status_code == 200, r.text
    temporary = r.json()["temporary_password"]
    assert len(temporary) >= 8 and r.json()["phone"] == agent.phone
    await db_session.refresh(agent)
    assert verify_password(temporary, agent.password_hash)          # le mot de passe rendu fonctionne
    assert not verify_password("AgentPass123!", agent.password_hash)  # l'ancien ne fonctionne plus
    assert temporary not in caplog.text                              # jamais écrit dans les journaux


@pytest.mark.asyncio
async def test_delete_agent_is_refused_while_the_cash_session_is_open(client, db_session, make_agent):
    agent = await make_agent()
    await _open_session(db_session, agent)
    r = await client.delete(f"/admin/api/agents/{agent.id}", headers=await _headers(client))
    assert r.status_code == 400 and "session de caisse ouverte" in r.json()["error"]
    await db_session.refresh(agent)
    assert agent.is_deleted is False

    free = await make_agent()
    await _commit(db_session)
    ok = await client.delete(f"/admin/api/agents/{free.id}", headers=await _headers(client))
    assert ok.status_code == 200
    assert "agents/" + free.id not in (await client.get("/admin/agents")).text


# ======================================================================== actions groupées

async def _bulk(client, action, ids):
    return await client.post(f"/admin/api/agents/bulk/{action}", headers=await _headers(client), json={"agent_ids": ids})


@pytest.mark.asyncio
async def test_bulk_activate_deactivate_delete(client, db_session, make_agent):
    agents = [await make_agent() for _ in range(3)]
    ids = [a.id for a in agents]
    await _commit(db_session)

    r = await _bulk(client, "deactivate", ids)
    assert r.status_code == 200 and r.json()["done"] == 3 and "3 agents désactivés" in r.json()["message"]
    for a in agents:
        await db_session.refresh(a)
    assert not any(a.is_active for a in agents)
    assert (await _bulk(client, "activate", ids[:2])).json()["done"] == 2
    for a in agents:
        await db_session.refresh(a)
    assert [a.is_active for a in agents] == [True, True, False]

    r = await _bulk(client, "delete", ids[1:])
    assert r.status_code == 200 and r.json()["done"] == 2
    for a in agents:
        await db_session.refresh(a)
    assert [a.is_deleted for a in agents] == [False, True, True]


@pytest.mark.asyncio
async def test_bulk_skips_players_missing_ids_and_open_cash_sessions(client, db_session, make_agent, make_user):
    working, idle = await make_agent(), await make_agent()
    player = await make_user()
    await _open_session(db_session, working)
    await _commit(db_session)

    r = await _bulk(client, "delete", [working.id, idle.id, player.id, "inconnu"])
    body = r.json()
    assert r.status_code == 200 and body["done"] == 1
    reasons = " ".join(s["reason"] for s in body["skipped"])
    assert "session de caisse ouverte" in reasons and "introuvable" in reasons
    for u in (working, idle, player):
        await db_session.refresh(u)
    assert (working.is_deleted, idle.is_deleted, player.is_deleted) == (False, True, False)

    nothing = await _bulk(client, "delete", [working.id])
    assert nothing.status_code == 400 and "Aucun agent supprimé" in nothing.json()["error"]


@pytest.mark.asyncio
async def test_bulk_validation(client):
    assert (await _bulk(client, "explode", ["x"])).status_code == 404
    empty = await _bulk(client, "activate", [])
    assert empty.status_code == 400 and "Aucun agent sélectionné" in empty.json()["error"]
    no_csrf = await client.post("/admin/api/agents/bulk/delete", json={"agent_ids": ["x"]}, follow_redirects=False)
    assert no_csrf.status_code in (303, 403)


# ======================================================================== export CSV et commission

def _rows(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "agents_" in response.headers["content-disposition"]
    return list(csv.reader(StringIO(response.content.decode("utf-8-sig")), delimiter=";"))


@pytest.mark.asyncio
async def test_export_follows_filters_and_selection(client, db_session, make_agent, make_user, make_bureau):
    bureau = await make_bureau()
    marie = await make_agent(bureau=bureau)
    marie.first_name, marie.commission_rate = "Marie", Decimal("7.5")
    paul = await make_agent()
    paul.first_name, paul.is_active = "Paul", False
    player = await make_user()
    await _commit(db_session)

    rows = _rows(await client.get("/admin/api/agents/export"))
    assert rows[0] == ["Téléphone", "Prénom", "Nom", "E-mail", "Rôle", "Bureau", "Statut", "Commission (%)", "Créé le"]
    assert {r[0] for r in rows[1:]} == {marie.phone, paul.phone}  # pas le joueur
    assert player.phone not in {r[0] for r in rows[1:]}
    only_bureau = _rows(await client.get(f"/admin/api/agents/export?bureau_id={bureau.id}"))
    assert [r[0] for r in only_bureau[1:]] == [marie.phone] and only_bureau[1][5] == bureau.name and only_bureau[1][7] == "7.50"
    inactive = _rows(await client.get("/admin/api/agents/export?status=inactive"))
    assert [r[0] for r in inactive[1:]] == [paul.phone] and inactive[1][6] == "Inactif"
    picked = _rows(await client.get(f"/admin/api/agents/export?ids={paul.id},{player.id}"))
    assert [r[0] for r in picked[1:]] == [paul.phone]  # un joueur coché n'est jamais exporté ici


@pytest.mark.asyncio
async def test_default_commission_rate(client):
    ok = await client.post("/admin/api/agents/commission-default", headers=await _headers(client), json={"rate": "8"})
    assert ok.status_code == 200 and ok.json()["rate"] == 8.0
    bad = await client.post("/admin/api/agents/commission-default", headers=await _headers(client), json={"rate": "abc"})
    assert bad.status_code in (400, 422) and bad.json().get("error")
