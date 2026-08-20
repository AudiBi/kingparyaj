# tests/test_api/test_payments.py
"""Test de bout en bout du parcours argent : dépôt/retrait via l'API REST
(/api/v1/deposit, /api/v1/withdraw), puis confirmation ou échec via le
webhook des passerelles de paiement (/api/v1/payments/{provider}/webhook)
ou son raccourci dev (/simulate) — cf. app/payments/base.py : MonCash/
NatCash sont simulés (aucune clé API configurée), mais tout le reste du
cycle (statut pending, signature de webhook, solde crédité/remboursé) est
le même que celui qu'aurait une vraie intégration.
"""

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.v1 import payments as payments_module
from app.api.v1 import wallet as wallet_module
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import create_access_token
from app.models.transaction import Transaction
from app.payments.base import get_gateway


@pytest.fixture
def payments_app(db_session, fake_redis) -> FastAPI:
    app = FastAPI()
    app.include_router(wallet_module.router, prefix="/api/v1")
    app.include_router(payments_module.router, prefix="/api/v1")

    async def _get_db():
        yield db_session

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    return app


@pytest.fixture
async def payments_client(payments_app):
    transport = ASGITransport(app=payments_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _auth_headers(user_id: str) -> dict:
    token = create_access_token({"sub": user_id, "role": "player"})
    return {"Authorization": f"Bearer {token}"}


async def _get_transaction(db_session, reference: str) -> Transaction:
    result = await db_session.execute(select(Transaction).where(Transaction.reference == reference))
    return result.scalar_one()


@pytest.mark.asyncio
async def test_cash_deposit_is_credited_immediately(payments_client, make_user):
    user = await make_user()

    response = await payments_client.post(
        "/api/v1/deposit",
        json={"amount": 500, "payment_method": "cash"},
        headers=_auth_headers(user.id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["new_balance"] == 500

    balance = await payments_client.get("/api/v1/balance", headers=_auth_headers(user.id))
    assert balance.json()["balance"] == 500


@pytest.mark.asyncio
async def test_moncash_deposit_full_cycle_via_webhook(payments_client, make_user, db_session):
    user = await make_user()
    headers = _auth_headers(user.id)

    deposit_response = await payments_client.post(
        "/api/v1/deposit", json={"amount": 500, "payment_method": "moncash"}, headers=headers
    )
    assert deposit_response.status_code == 200
    data = deposit_response.json()
    assert data["status"] == "pending"
    assert data["payment_url"]

    # Le solde ne bouge pas tant que MonCash n'a pas confirmé.
    balance = await payments_client.get("/api/v1/balance", headers=headers)
    assert balance.json()["balance"] == 0

    transaction = await _get_transaction(db_session, data["reference"])
    gateway = get_gateway("moncash")
    body = json.dumps(
        {"event": "payment.completed", "external_reference": transaction.external_reference}
    ).encode()
    signature = gateway.sign(body)

    webhook_response = await payments_client.post(
        "/api/v1/payments/moncash/webhook",
        content=body,
        headers={"X-Signature": signature, "Content-Type": "application/json"},
    )
    assert webhook_response.status_code == 200
    assert webhook_response.json()["status"] == "completed"

    balance = await payments_client.get("/api/v1/balance", headers=headers)
    assert balance.json()["balance"] == 500


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature(payments_client, make_user, db_session):
    user = await make_user()
    headers = _auth_headers(user.id)

    deposit_response = await payments_client.post(
        "/api/v1/deposit", json={"amount": 500, "payment_method": "moncash"}, headers=headers
    )
    transaction = await _get_transaction(db_session, deposit_response.json()["reference"])

    body = json.dumps(
        {"event": "payment.completed", "external_reference": transaction.external_reference}
    ).encode()

    response = await payments_client.post(
        "/api/v1/payments/moncash/webhook",
        content=body,
        headers={"X-Signature": "signature-invalide", "Content-Type": "application/json"},
    )

    assert response.status_code == 401
    balance = await payments_client.get("/api/v1/balance", headers=headers)
    assert balance.json()["balance"] == 0  # toujours pas crédité


@pytest.mark.asyncio
async def test_moncash_deposit_failure_via_simulate_leaves_balance_untouched(
    payments_client, make_user, db_session
):
    user = await make_user()
    headers = _auth_headers(user.id)

    deposit_response = await payments_client.post(
        "/api/v1/deposit", json={"amount": 500, "payment_method": "moncash"}, headers=headers
    )
    transaction = await _get_transaction(db_session, deposit_response.json()["reference"])

    simulate_response = await payments_client.post(
        "/api/v1/payments/moncash/simulate",
        json={
            "external_reference": transaction.external_reference,
            "kind": "deposit",
            "outcome": "failure",
            "reason": "Solde mobile money insuffisant",
        },
    )

    assert simulate_response.status_code == 200
    assert simulate_response.json()["status"] == "failed"

    balance = await payments_client.get("/api/v1/balance", headers=headers)
    assert balance.json()["balance"] == 0


@pytest.mark.asyncio
async def test_moncash_withdraw_full_cycle_success(payments_client, make_user, db_session):
    user = await make_user()
    headers = _auth_headers(user.id)

    # Solde initial via un dépôt cash (immédiat), pour pouvoir retirer ensuite.
    await payments_client.post(
        "/api/v1/deposit", json={"amount": 5000, "payment_method": "cash"}, headers=headers
    )

    withdraw_response = await payments_client.post(
        "/api/v1/withdraw", json={"amount": 1000, "payment_method": "moncash"}, headers=headers
    )
    assert withdraw_response.status_code == 200
    data = withdraw_response.json()
    assert data["status"] == "pending"
    assert data["new_balance"] == 4000  # réservé tout de suite

    transaction = await _get_transaction(db_session, data["reference"])

    simulate_response = await payments_client.post(
        "/api/v1/payments/moncash/simulate",
        json={
            "external_reference": transaction.external_reference,
            "kind": "withdrawal",
            "outcome": "success",
        },
    )
    assert simulate_response.status_code == 200
    assert simulate_response.json()["status"] == "completed"

    balance = await payments_client.get("/api/v1/balance", headers=headers)
    assert balance.json()["balance"] == 4000


@pytest.mark.asyncio
async def test_moncash_withdraw_failure_refunds_via_simulate(payments_client, make_user, db_session):
    user = await make_user()
    headers = _auth_headers(user.id)

    await payments_client.post(
        "/api/v1/deposit", json={"amount": 5000, "payment_method": "cash"}, headers=headers
    )
    withdraw_response = await payments_client.post(
        "/api/v1/withdraw", json={"amount": 1000, "payment_method": "moncash"}, headers=headers
    )
    data = withdraw_response.json()
    assert data["new_balance"] == 4000

    transaction = await _get_transaction(db_session, data["reference"])

    simulate_response = await payments_client.post(
        "/api/v1/payments/moncash/simulate",
        json={
            "external_reference": transaction.external_reference,
            "kind": "withdrawal",
            "outcome": "failure",
            "reason": "Numéro mobile invalide",
        },
    )
    assert simulate_response.status_code == 200
    assert simulate_response.json()["status"] == "failed"

    balance = await payments_client.get("/api/v1/balance", headers=headers)
    assert balance.json()["balance"] == 5000  # remboursé


# ========== Limites (/api/v1/limits) ==========

@pytest.mark.asyncio
async def test_set_limit_via_api_is_reflected_on_wallet(payments_client, make_user):
    user = await make_user()
    headers = _auth_headers(user.id)

    response = await payments_client.post(
        "/api/v1/limits",
        json={"limit_type": "daily_deposit", "limit_amount": 1000},
        headers=headers,
    )
    assert response.status_code == 200

    wallet = await payments_client.get("/api/v1/", headers=headers)
    assert wallet.json()["daily_deposit_limit"] == 1000


@pytest.mark.asyncio
async def test_set_limit_null_clears_it_without_touching_others(payments_client, make_user):
    user = await make_user()
    headers = _auth_headers(user.id)

    await payments_client.post(
        "/api/v1/limits",
        json={"limit_type": "daily_deposit", "limit_amount": 1000},
        headers=headers,
    )
    await payments_client.post(
        "/api/v1/limits",
        json={"limit_type": "single_bet", "limit_amount": 50},
        headers=headers,
    )

    clear_response = await payments_client.post(
        "/api/v1/limits",
        json={"limit_type": "daily_deposit", "limit_amount": None},
        headers=headers,
    )
    assert clear_response.status_code == 200

    wallet = await payments_client.get("/api/v1/", headers=headers)
    data = wallet.json()
    assert data["daily_deposit_limit"] is None
    assert data["single_bet_limit"] == 50  # non affectée


@pytest.mark.asyncio
async def test_set_limit_unknown_type_rejected(payments_client, make_user):
    user = await make_user()
    headers = _auth_headers(user.id)

    response = await payments_client.post(
        "/api/v1/limits",
        json={"limit_type": "not_a_real_limit", "limit_amount": 100},
        headers=headers,
    )

    assert response.status_code == 400
