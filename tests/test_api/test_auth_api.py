# tests/test_api/test_auth_api.py
"""Test de bout en bout de l'API d'authentification (app/api/v1/auth.py).

Chaque route appelait AuthService/UserService avec des signatures qui ne
correspondaient à rien de réel (ex. `auth_service.register(phone=..., ...)`
alors que la vraie méthode prend un objet `UserCreate` ; `user_service
.get_user_by_phone(...)`, méthode qui n'a jamais existé — `get_by_phone`).
Résultat : /register, /login, /refresh, /me, /me (PUT) et /change-password
plantaient tous. Sans inscription/connexion fonctionnelles, aucune route
argent n'est atteignable par un nouveau joueur via l'API réelle.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1 import auth as auth_module
from app.core.database import get_db
from app.core.redis_client import get_redis


@pytest.fixture
def auth_app(db_session, fake_redis) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/api/v1")

    async def _get_db():
        yield db_session

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    return app


@pytest.fixture
async def auth_client(auth_app):
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.asyncio
async def test_register_creates_account_and_returns_tokens(auth_client):
    response = await auth_client.post(
        "/api/v1/register", json={"phone": "40001234", "password": "Secret123!"}
    )

    assert response.status_code == 201
    data = response.json()
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_register_duplicate_phone_rejected(auth_client):
    payload = {"phone": "40001235", "password": "Secret123!"}
    await auth_client.post("/api/v1/register", json=payload)

    response = await auth_client.post("/api/v1/register", json=payload)

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_login_success_returns_tokens(auth_client):
    await auth_client.post("/api/v1/register", json={"phone": "40001236", "password": "Secret123!"})

    response = await auth_client.post(
        "/api/v1/login", json={"phone": "40001236", "password": "Secret123!"}
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


@pytest.mark.asyncio
async def test_login_wrong_password_rejected(auth_client):
    await auth_client.post("/api/v1/register", json={"phone": "40001237", "password": "Secret123!"})

    response = await auth_client.post(
        "/api/v1/login", json={"phone": "40001237", "password": "WrongPassword"}
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_returns_new_access_token(auth_client):
    register_response = await auth_client.post(
        "/api/v1/register", json={"phone": "40001238", "password": "Secret123!"}
    )
    refresh_token = register_response.json()["refresh_token"]

    response = await auth_client.post(f"/api/v1/refresh?refresh_token={refresh_token}")

    assert response.status_code == 200
    assert response.json()["access_token"]


@pytest.mark.asyncio
async def test_me_returns_profile_with_wallet_balance(auth_client, db_session, fake_redis):
    register_response = await auth_client.post(
        "/api/v1/register", json={"phone": "40001239", "password": "Secret123!"}
    )
    access_token = register_response.json()["access_token"]

    from decimal import Decimal

    from app.services.user_service import UserService
    from app.services.wallet_service import WalletService

    user = await UserService(db_session, fake_redis).get_by_phone("40001239")
    await WalletService(db_session, fake_redis).credit(user.id, Decimal("250"), "DEPOSIT")

    response = await auth_client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {access_token}"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["phone"] == "40001239"
    assert data["wallet_balance"] == 250.0


@pytest.mark.asyncio
async def test_update_me_rejects_phone_change(auth_client):
    register_response = await auth_client.post(
        "/api/v1/register", json={"phone": "40001240", "password": "Secret123!"}
    )
    access_token = register_response.json()["access_token"]

    response = await auth_client.put(
        "/api/v1/me",
        json={"phone": "40009999"},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_update_me_updates_first_name(auth_client):
    register_response = await auth_client.post(
        "/api/v1/register", json={"phone": "40001241", "password": "Secret123!"}
    )
    access_token = register_response.json()["access_token"]

    response = await auth_client.put(
        "/api/v1/me",
        json={"first_name": "Jean"},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert response.json()["first_name"] == "Jean"


@pytest.mark.asyncio
async def test_change_password_then_login_with_new_password(auth_client):
    register_response = await auth_client.post(
        "/api/v1/register", json={"phone": "40001242", "password": "Secret123!"}
    )
    access_token = register_response.json()["access_token"]

    response = await auth_client.post(
        "/api/v1/change-password",
        json={
            "current_password": "Secret123!",
            "new_password": "NewSecret456!",
            "confirm_password": "NewSecret456!",
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200

    login_response = await auth_client.post(
        "/api/v1/login", json={"phone": "40001242", "password": "NewSecret456!"}
    )
    assert login_response.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_current_password_rejected(auth_client):
    register_response = await auth_client.post(
        "/api/v1/register", json={"phone": "40001243", "password": "Secret123!"}
    )
    access_token = register_response.json()["access_token"]

    response = await auth_client.post(
        "/api/v1/change-password",
        json={
            "current_password": "WrongPassword",
            "new_password": "NewSecret456!",
            "confirm_password": "NewSecret456!",
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_forgot_and_reset_password_full_cycle(auth_client, fake_redis):
    await auth_client.post("/api/v1/register", json={"phone": "40001244", "password": "Secret123!"})

    forgot_response = await auth_client.post(
        "/api/v1/forgot-password", json={"phone": "40001244"}
    )
    assert forgot_response.status_code == 200

    reset_code = await fake_redis.get("reset:40001244")
    assert reset_code

    reset_response = await auth_client.post(
        "/api/v1/reset-password",
        json={"phone": "40001244", "code": reset_code, "new_password": "ResetSecret789!"},
    )
    assert reset_response.status_code == 200
    assert reset_response.json()["access_token"]

    login_response = await auth_client.post(
        "/api/v1/login", json={"phone": "40001244", "password": "ResetSecret789!"}
    )
    assert login_response.status_code == 200
