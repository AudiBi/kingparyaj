# tests/test_api/test_admin_users_list.py
"""Liste des utilisateurs du panel admin : filtres, recherche, pagination, voir / modifier,
suppression, actions groupées (activer, désactiver, supprimer) et export CSV."""

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
from app.core.session_expired import session_expired_handler
from app.models.enums import KYCStatus, UserRole
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


# ======================================================================== liste, filtres, pagination

@pytest.mark.asyncio
async def test_list_renders_with_every_filter_value(client, db_session, make_user):
    player = await make_user(balance=Decimal("250"))
    player.first_name, player.last_name = "Marie", "Jean"
    await _commit(db_session)

    assert (await client.get("/admin/users")).status_code == 200
    for query in (
        "role=player", "role=agent", "role=admin", "role=manager", "role=super_admin",
        "kyc_status=pending", "kyc_status=verified", "kyc_status=rejected", "kyc_status=expired",
        "status=active", "status=inactive", "status=locked",
        "role=player&kyc_status=pending&status=active&search=Marie",
    ):
        response = await client.get(f"/admin/users?{query}")
        assert response.status_code == 200, (query, response.status_code)


@pytest.mark.asyncio
async def test_unknown_filter_values_do_not_crash_the_page(client):
    for query in ("role=zzz", "kyc_status=nope", "status=whatever", "page=999", "per_page=100"):
        response = await client.get(f"/admin/users?{query}")
        assert response.status_code == 200, (query, response.status_code, response.text[:200])


@pytest.mark.asyncio
async def test_filters_select_the_right_users(client, db_session, make_user, make_agent):
    marie = await make_user()
    marie.first_name = "Marie"
    paul = await make_user()
    paul.first_name, paul.is_active = "Paul", False
    locked = await make_user()
    locked.first_name, locked.is_locked = "Verrou", True
    agent = await make_agent()
    agent.first_name = "Agent007"
    verified = await make_user(kyc_verified=True)
    verified.first_name = "Verifie"
    await _commit(db_session)

    def names(html):
        return {n for n in ("Marie", "Paul", "Verrou", "Agent007", "Verifie") if n in html}

    assert names((await client.get("/admin/users?search=Marie")).text) == {"Marie"}
    assert names((await client.get(f"/admin/users?search={marie.phone}")).text) == {"Marie"}
    assert names((await client.get("/admin/users?status=inactive")).text) == {"Paul"}
    assert names((await client.get("/admin/users?status=locked")).text) == {"Verrou"}
    assert names((await client.get("/admin/users?role=agent")).text) == {"Agent007"}
    assert names((await client.get("/admin/users?kyc_status=verified")).text) == {"Verifie"}
    everyone = names((await client.get("/admin/users")).text)
    assert {"Marie", "Paul", "Verrou", "Agent007", "Verifie"} <= everyone


@pytest.mark.asyncio
async def test_pagination(client, db_session, make_user):
    for _ in range(5):
        await make_user()
    await _commit(db_session)
    first = await client.get("/admin/users?per_page=2&page=1")
    third = await client.get("/admin/users?per_page=2&page=3")
    assert first.status_code == third.status_code == 200
    assert first.text != third.text
    assert "page=2" in first.text  # lien vers la page suivante


@pytest.mark.asyncio
async def test_view_edit_and_create_pages(client, db_session, make_user):
    player = await make_user()
    await _commit(db_session)
    for path in (f"/admin/users/{player.id}", f"/admin/users/{player.id}/edit", "/admin/users/create"):
        assert (await client.get(path)).status_code == 200, path
    assert (await client.get("/admin/users/inconnu")).status_code in (404, 400)
    assert (await client.get("/admin/users/inconnu/edit")).status_code in (404, 400)


# ======================================================================== suppression individuelle

@pytest.mark.asyncio
async def test_single_delete_removes_the_user_from_the_list(client, db_session, make_user):
    player = await make_user()
    player.first_name = "ASupprimer"
    await _commit(db_session)
    assert "ASupprimer" in (await client.get("/admin/users")).text
    r = await client.delete(f"/admin/api/users/{player.id}", headers=await _headers(client))
    assert r.status_code == 200 and r.json()["success"] is True
    assert "ASupprimer" not in (await client.get("/admin/users")).text
    again = await client.delete(f"/admin/api/users/{player.id}", headers=await _headers(client))
    assert again.status_code == 404 and "non trouvé" in again.json()["error"]


# ======================================================================== actions groupées

async def _bulk(client, action, ids):
    return await client.post(f"/admin/api/users/bulk/{action}", headers=await _headers(client), json={"user_ids": ids})


@pytest.mark.asyncio
async def test_bulk_deactivate_then_activate(client, db_session, make_user):
    users = [await make_user() for _ in range(3)]
    ids = [u.id for u in users]
    await _commit(db_session)

    r = await _bulk(client, "deactivate", ids)
    assert r.status_code == 200 and r.json()["done"] == 3 and "3 utilisateurs désactivés" in r.json()["message"]
    for u in users:
        await db_session.refresh(u)
        assert u.is_active is False
    r = await _bulk(client, "activate", ids[:2])
    assert r.json()["done"] == 2
    for u in users:
        await db_session.refresh(u)
    assert [u.is_active for u in users] == [True, True, False]


@pytest.mark.asyncio
async def test_bulk_activate_skips_locked_accounts(client, db_session, make_user):
    free, locked = await make_user(), await make_user()
    free.is_active = False
    locked.is_active, locked.is_locked = False, True
    await _commit(db_session)
    r = await _bulk(client, "activate", [free.id, locked.id])
    body = r.json()
    assert r.status_code == 200 and body["done"] == 1 and "bloqué" in body["message"]
    await db_session.refresh(locked)
    assert locked.is_active is False and locked.is_locked is True  # un compte bloqué se débloque explicitement


@pytest.mark.asyncio
async def test_bulk_delete_is_a_soft_delete_and_removes_from_the_list(client, db_session, make_user):
    a, b = await make_user(), await make_user()
    a.first_name, b.first_name = "SuppA", "SuppB"
    await _commit(db_session)
    r = await _bulk(client, "delete", [a.id, b.id])
    assert r.status_code == 200 and r.json()["done"] == 2
    page = (await client.get("/admin/users")).text
    assert "SuppA" not in page and "SuppB" not in page
    await db_session.refresh(a)
    assert a.is_deleted is True and a.is_active is False


@pytest.mark.asyncio
async def test_bulk_never_touches_yourself_or_a_super_admin(client, db_session, admin_user, make_user):
    boss = await make_user()
    boss.role = UserRole.SUPER_ADMIN
    other = await make_user()
    await _commit(db_session)

    r = await _bulk(client, "delete", [admin_user.id, boss.id, other.id])
    body = r.json()
    assert r.status_code == 200 and body["done"] == 1
    reasons = " ".join(s["reason"] for s in body["skipped"])
    assert "vous-même" in reasons and "super administrateur" in reasons
    for u in (admin_user, boss, other):
        await db_session.refresh(u)
    assert (admin_user.is_deleted, boss.is_deleted, other.is_deleted) == (False, False, True)

    only_forbidden = await _bulk(client, "deactivate", [admin_user.id, boss.id])
    assert only_forbidden.status_code == 400 and "Aucun utilisateur désactivé" in only_forbidden.json()["error"]
    await db_session.refresh(boss)
    assert boss.is_active is True


@pytest.mark.asyncio
async def test_bulk_validation(client, db_session, make_user):
    assert (await _bulk(client, "explode", ["x"])).status_code == 404
    empty = await _bulk(client, "activate", [])
    assert empty.status_code == 400 and "Aucun utilisateur sélectionné" in empty.json()["error"]
    missing = await _bulk(client, "delete", ["inconnu"])
    assert missing.status_code == 400 and "introuvable" in missing.json()["error"]
    no_csrf = await client.post("/admin/api/users/bulk/delete", json={"user_ids": ["x"]}, follow_redirects=False)
    assert no_csrf.status_code in (303, 403)


@pytest.mark.asyncio
async def test_single_delete_refuses_yourself_and_a_super_admin(client, db_session, admin_user, make_user):
    boss = await make_user()
    boss.role = UserRole.SUPER_ADMIN
    await _commit(db_session)
    me = await client.delete(f"/admin/api/users/{admin_user.id}", headers=await _headers(client))
    assert me.status_code == 400 and "propre compte" in me.json()["error"]
    other = await client.delete(f"/admin/api/users/{boss.id}", headers=await _headers(client))
    assert other.status_code == 403 and "super administrateur" in other.json()["error"]
    await db_session.refresh(admin_user)
    assert admin_user.is_deleted is False


# ======================================================================== export CSV

def _rows(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "utilisateurs_" in response.headers["content-disposition"] and ".csv" in response.headers["content-disposition"]
    text = response.content.decode("utf-8-sig")
    return list(csv.reader(StringIO(text), delimiter=";"))


@pytest.mark.asyncio
async def test_export_follows_the_list_filters(client, db_session, make_user, make_agent):
    marie = await make_user(balance=Decimal("250.5"))
    marie.first_name, marie.last_name = "Marie", "Jean"
    paul = await make_user()
    paul.first_name, paul.is_active = "Paul", False
    agent = await make_agent()
    await _commit(db_session)

    rows = _rows(await client.get("/admin/api/users/export"))
    assert rows[0] == ["Téléphone", "Prénom", "Nom", "E-mail", "Rôle", "KYC", "Statut", "Solde (HTG)", "Créé le"]
    phones = {r[0] for r in rows[1:]}
    assert {marie.phone, paul.phone, agent.phone} <= phones

    filtered = _rows(await client.get("/admin/api/users/export?role=agent"))
    assert [r[0] for r in filtered[1:]] == [agent.phone]
    inactive = _rows(await client.get("/admin/api/users/export?status=inactive&search=Paul"))
    assert [r[0] for r in inactive[1:]] == [paul.phone] and inactive[1][6] == "Inactif"
    mine = _rows(await client.get(f"/admin/api/users/export?ids={marie.id}"))
    assert len(mine) == 2 and mine[1][7] == "250.50" and mine[1][1] == "Marie"
    assert _rows(await client.get("/admin/api/users/export?role=zzz&kyc_status=nope"))  # filtres inconnus : pas d'erreur


@pytest.mark.asyncio
async def test_export_neutralizes_spreadsheet_formulas(client, db_session, make_user):
    attacker = await make_user()
    attacker.first_name = "=HYPERLINK(\"http://evil\")"
    await _commit(db_session)
    rows = _rows(await client.get(f"/admin/api/users/export?ids={attacker.id}"))
    assert rows[1][1].startswith("'=")  # apostrophe devant : Excel n'exécute pas la formule


@pytest.mark.asyncio
async def test_export_requires_an_admin_session(client, db_session):
    anonymous = AsyncClient(transport=client._transport, base_url="http://testserver")
    r = await anonymous.get("/admin/api/users/export", follow_redirects=False)
    assert r.status_code in (303, 401, 403)
    await anonymous.aclose()
