# tests/test_api/test_admin_transactions.py
"""Tests de la confirmation/annulation manuelle d'une transaction MonCash/
NatCash en attente depuis le panel admin (app/templates/admin/transactions/
index.html -> /admin/api/transactions/{id}/confirm|cancel).

Ces deux routes dupliquaient à la main la logique de WalletService avec un
modèle de réservation différent (`wallet.pending_withdrawals`, jamais
alimenté ailleurs dans le code) : confirmer un retrait aurait rendu ce
compteur négatif, et confirmer un dépôt ne créditait jamais le joueur.
Elles délèguent maintenant à WalletService.confirm_*/fail_*, comme le
webhook des passerelles.
"""

import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.transaction import Transaction
from app.schemas.wallet import DepositRequest, PaymentMethod
from app.services.wallet_service import WalletService
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]*)"', html)
    assert match, "champ csrf_token introuvable"
    return match.group(1)


async def _login(admin_client) -> None:
    login_page = await admin_client.get("/admin/login")
    response = await admin_client.post(
        "/admin/login",
        data={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "csrf_token": _extract_csrf_token(login_page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


async def _fresh_csrf_headers(admin_client) -> dict:
    """Le cookie CSRF est régénéré à chaque réponse (cf. AdminCsrfMiddleware) :
    le jeton utilisé pour se connecter est déjà périmé pour l'appel suivant,
    il en faut un frais — comme le ferait un vrai fetch() dans le navigateur,
    qui relit toujours {{ csrf_token() }} au moment du rendu de la page."""
    page = await admin_client.get("/admin/login")
    return {"X-CSRFToken": _extract_csrf_token(page.text)}


@pytest.mark.asyncio
async def test_admin_confirm_pending_deposit_credits_the_wallet(
    admin_client, admin_user, make_user, db_session, fake_redis
):
    await _login(admin_client)
    player = await make_user()
    await WalletService(db_session, fake_redis).deposit(
        player.id, DepositRequest(amount=500, payment_method=PaymentMethod.MONCASH)
    )
    result = await db_session.execute(select(Transaction).where(Transaction.user_id == player.id))
    transaction = result.scalar_one()
    assert transaction.status.value == "pending"

    response = await admin_client.post(
        f"/admin/api/transactions/{transaction.id}/confirm",
        headers=await _fresh_csrf_headers(admin_client),
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("500")


@pytest.mark.asyncio
async def test_admin_cancel_pending_withdrawal_refunds_the_wallet(
    admin_client, admin_user, make_user, db_session, fake_redis
):
    await _login(admin_client)
    player = await make_user(balance=Decimal("5000"), kyc_verified=True)

    from app.schemas.wallet import WithdrawRequest

    await WalletService(db_session, fake_redis).withdraw(
        player.id, WithdrawRequest(amount=1000, payment_method=PaymentMethod.MONCASH)
    )
    result = await db_session.execute(select(Transaction).where(Transaction.user_id == player.id))
    transaction = result.scalar_one()
    assert transaction.status.value == "pending"
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("4000")

    response = await admin_client.post(
        f"/admin/api/transactions/{transaction.id}/cancel",
        json={"reason": "Virement bloqué côté fournisseur"},
        headers=await _fresh_csrf_headers(admin_client),
    )

    assert response.status_code == 200
    assert await WalletService(db_session, fake_redis).get_balance(player.id) == Decimal("5000")


@pytest.mark.asyncio
async def test_admin_confirm_already_completed_transaction_rejected(
    admin_client, admin_user, make_user, db_session, fake_redis
):
    await _login(admin_client)
    player = await make_user()
    await WalletService(db_session, fake_redis).credit(player.id, Decimal("100"), "DEPOSIT")
    result = await db_session.execute(select(Transaction).where(Transaction.user_id == player.id))
    transaction = result.scalar_one()
    assert transaction.status.value == "completed"

    response = await admin_client.post(
        f"/admin/api/transactions/{transaction.id}/confirm",
        headers=await _fresh_csrf_headers(admin_client),
    )

    assert response.status_code == 400
