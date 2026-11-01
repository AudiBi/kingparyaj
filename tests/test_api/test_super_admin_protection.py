# tests/test_api/test_super_admin_protection.py
"""Un admin ne peut ni modifier, ni supprimer, ni bloquer, ni créditer… un super admin,
ni donner le rôle super admin. Seul un super admin le peut."""

import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.enums import UserRole
from app.models.user import User
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token"\s+value="([^"]*)"', html).group(1)


async def _login(client) -> None:
    page = await client.get("/admin/login")
    response = await client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def _headers(client) -> dict:
    return {"X-CSRFToken": _csrf((await client.get("/admin/login")).text)}


def _actions(uid: str):
    """Toutes les actions d'un admin sur un compte (méthode, adresse, corps JSON)."""
    return [
        ("PUT", f"/admin/api/users/{uid}", {"first_name": "Piraté"}),
        ("PUT", f"/admin/api/users/{uid}", {"password": "Nouveau123!"}),
        ("PUT", f"/admin/api/users/{uid}", {"is_active": False}),
        ("DELETE", f"/admin/api/users/{uid}", None),
        ("POST", f"/admin/api/users/{uid}/block", None),
        ("POST", f"/admin/api/users/{uid}/unblock", None),
        ("POST", f"/admin/api/users/{uid}/kyc/verify", None),
        ("POST", f"/admin/api/users/{uid}/kyc/reject", {"reason": "test"}),
        ("POST", f"/admin/api/users/{uid}/kyc/reset", None),
        ("POST", f"/admin/api/users/{uid}/credit", {"amount": 10, "reason": "test"}),
        ("POST", f"/admin/api/users/{uid}/debit", {"amount": 10, "reason": "test"}),
        ("PUT", f"/admin/api/agents/{uid}", {"first_name": "Piraté"}),
        ("DELETE", f"/admin/api/agents/{uid}", None),
        ("POST", f"/admin/api/agents/{uid}/toggle-status", None),
        ("POST", f"/admin/api/agents/{uid}/reset-password", None),
    ]


async def _call(client, method, url, body):
    kwargs = {"headers": await _headers(client)}
    if body is not None:
        kwargs["json"] = body
    elif url.endswith("/block"):
        kwargs["data"] = {"reason": "test"}  # cette route lit un formulaire
    return await client.request(method, url, **kwargs)


async def _make_super(db_session, make_user, balance=Decimal("100")) -> User:
    boss = await make_user(balance=balance)
    boss.role = UserRole.SUPER_ADMIN
    await db_session.commit()
    return boss


@pytest.mark.asyncio
async def test_admin_cannot_touch_a_super_admin(admin_client, admin_user, make_user, db_session):
    boss = await _make_super(db_session, make_user)
    await _login(admin_client)
    for method, url, body in _actions(boss.id):
        response = await _call(admin_client, method, url, body)
        assert response.status_code == 403, (method, url, response.status_code, response.text)
        assert "super" in response.json()["detail"].lower()
    await db_session.refresh(boss)
    assert boss.role == UserRole.SUPER_ADMIN and boss.is_active and not boss.is_deleted
    assert boss.first_name != "Piraté" and not boss.is_locked


@pytest.mark.asyncio
async def test_admin_still_manages_ordinary_accounts(admin_client, admin_user, make_user, db_session):
    player = await make_user(balance=Decimal("100"))
    await _login(admin_client)
    response = await _call(admin_client, "PUT", f"/admin/api/users/{player.id}", {"first_name": "Marie"})
    assert response.status_code == 200
    response = await _call(admin_client, "POST", f"/admin/api/users/{player.id}/credit", {"amount": 10, "reason": "test"})
    assert response.status_code == 200
    await db_session.refresh(player)
    assert player.first_name == "Marie"


@pytest.mark.asyncio
async def test_admin_cannot_grant_super_admin_role(admin_client, admin_user, make_user, db_session):
    player = await make_user()
    await _login(admin_client)
    response = await _call(admin_client, "PUT", f"/admin/api/users/{player.id}", {"role": "super_admin"})
    assert response.status_code == 403
    response = await _call(admin_client, "PUT", f"/admin/api/users/{admin_user.id}", {"role": "super_admin"})  # lui-même
    assert response.status_code == 403
    await db_session.refresh(player)
    await db_session.refresh(admin_user)
    assert player.role == UserRole.PLAYER and admin_user.role == UserRole.ADMIN

    created = await _call(admin_client, "POST", "/admin/users/create", {
        "phone": "40998877", "password": "Secret123!", "role": "super_admin",
    })
    assert created.status_code == 403
    assert (await db_session.execute(select(User).where(User.phone == "40998877"))).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_super_admin_can_manage_a_super_admin(admin_client, admin_user, make_user, db_session):
    other = await _make_super(db_session, make_user)
    admin_user.role = UserRole.SUPER_ADMIN  # l'utilisateur connecté devient super admin
    await db_session.commit()
    await _login(admin_client)
    response = await _call(admin_client, "PUT", f"/admin/api/users/{other.id}", {"first_name": "Chef"})
    assert response.status_code == 200
    response = await _call(admin_client, "POST", f"/admin/api/users/{other.id}/credit", {"amount": 10, "reason": "test"})
    assert response.status_code == 200
    response = await _call(admin_client, "PUT", f"/admin/api/users/{other.id}", {"role": "super_admin"})
    assert response.status_code == 200
