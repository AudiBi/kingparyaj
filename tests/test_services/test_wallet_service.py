# tests/test_services/test_wallet_service.py
"""Tests critiques du portefeuille : logique pure du modèle Wallet, puis
crédit/débit, dépôt, retrait et limites via WalletService (DB SQLite)."""

from decimal import Decimal

import pytest

from app.core.exceptions import AppException, InsufficientBalanceException
from app.models.audit import AuditLog
from app.models.enums import WalletStatus
from app.models.wallet import Wallet
from app.schemas.wallet import DepositRequest, PaymentMethod, WithdrawRequest
from app.services.wallet_service import WalletService


# ========== Wallet : logique pure (aucune DB) ==========

def test_can_bet_true_within_balance():
    wallet = Wallet(balance=Decimal("500"), status=WalletStatus.ACTIVE, today_losses=Decimal("0"))
    assert wallet.can_bet(Decimal("100")) is True


def test_can_bet_false_when_balance_insufficient():
    wallet = Wallet(balance=Decimal("50"), status=WalletStatus.ACTIVE, today_losses=Decimal("0"))
    assert wallet.can_bet(Decimal("100")) is False


def test_can_bet_false_when_wallet_frozen():
    wallet = Wallet(balance=Decimal("500"), status=WalletStatus.FROZEN, today_losses=Decimal("0"))
    assert wallet.can_bet(Decimal("100")) is False


def test_can_bet_respects_single_bet_limit():
    wallet = Wallet(
        balance=Decimal("500"),
        status=WalletStatus.ACTIVE,
        today_losses=Decimal("0"),
        single_bet_limit=Decimal("50"),
    )
    assert wallet.can_bet(Decimal("100")) is False
    assert wallet.can_bet(Decimal("50")) is True


def test_can_bet_respects_daily_loss_limit():
    wallet = Wallet(
        balance=Decimal("500"),
        status=WalletStatus.ACTIVE,
        today_losses=Decimal("450"),
        daily_loss_limit=Decimal("500"),
    )
    assert wallet.can_bet(Decimal("100")) is False  # 450 + 100 > 500
    assert wallet.can_bet(Decimal("50")) is True  # 450 + 50 == 500, pas au-dessus


def test_deduct_success_decreases_balance():
    wallet = Wallet(balance=Decimal("100"), bonus_balance=Decimal("0"))
    assert wallet.deduct(Decimal("40")) is True
    assert wallet.balance == Decimal("60")


def test_deduct_insufficient_funds_returns_false_and_leaves_balance_untouched():
    wallet = Wallet(balance=Decimal("30"), bonus_balance=Decimal("0"))
    assert wallet.deduct(Decimal("40")) is False
    assert wallet.balance == Decimal("30")


def test_deduct_from_bonus_balance():
    wallet = Wallet(balance=Decimal("0"), bonus_balance=Decimal("20"))
    assert wallet.deduct(Decimal("20"), is_bonus=True) is True
    assert wallet.bonus_balance == Decimal("0")


def test_add_credits_real_balance():
    wallet = Wallet(balance=Decimal("10"), bonus_balance=Decimal("0"), total_bonus_received=Decimal("0"))
    wallet.add(Decimal("15"))
    assert wallet.balance == Decimal("25")


def test_add_bonus_updates_bonus_balance_and_totals():
    wallet = Wallet(balance=Decimal("0"), bonus_balance=Decimal("5"), total_bonus_received=Decimal("5"))
    wallet.add(Decimal("10"), is_bonus=True)
    assert wallet.bonus_balance == Decimal("15")
    assert wallet.total_bonus_received == Decimal("15")


# ========== WalletService.credit / debit ==========

@pytest.mark.asyncio
async def test_get_or_create_creates_wallet_with_zero_balance(db_session, fake_redis, make_user):
    user = await make_user()
    service = WalletService(db_session, fake_redis)
    wallet = await service.get_or_create(user.id)
    assert wallet.balance == Decimal("0")


@pytest.mark.asyncio
async def test_credit_increases_balance_and_records_transaction(db_session, fake_redis, make_user):
    user = await make_user()
    service = WalletService(db_session, fake_redis)

    tx = await service.credit(user.id, Decimal("250"), "DEPOSIT", description="test")

    assert tx.amount == Decimal("250")
    assert tx.balance_before == Decimal("0")
    assert tx.balance_after == Decimal("250")
    assert tx.reference, "la transaction doit avoir une référence non nulle"
    assert await service.get_balance(user.id) == Decimal("250")


@pytest.mark.asyncio
async def test_credit_win_updates_total_won_and_writes_audit_log(db_session, fake_redis, make_user):
    """Régression : WalletService.credit(..., 'WIN', ...) est le chemin emprunté
    pour payer les gagnants d'un tirage Keno (cf. KenoService.execute_draw).
    Il ne doit ni planter, ni oublier de créditer le portefeuille."""
    user = await make_user()
    service = WalletService(db_session, fake_redis)

    await service.credit(user.id, Decimal("600"), "WIN")

    wallet = await service.get_by_user_id(user.id)
    assert wallet.balance == Decimal("600")
    assert wallet.total_won == Decimal("600")

    from sqlalchemy import select

    result = await db_session.execute(select(AuditLog).where(AuditLog.user_id == user.id))
    assert result.scalars().first() is not None


@pytest.mark.asyncio
async def test_debit_decreases_balance(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("300"))
    service = WalletService(db_session, fake_redis)

    tx = await service.debit(user.id, Decimal("120"), "BET")

    assert tx.balance_after == Decimal("180")
    assert await service.get_balance(user.id) == Decimal("180")


@pytest.mark.asyncio
async def test_debit_insufficient_balance_raises_and_leaves_balance_untouched(
    db_session, fake_redis, make_user
):
    user = await make_user(balance=Decimal("50"))
    service = WalletService(db_session, fake_redis)

    with pytest.raises(InsufficientBalanceException):
        await service.debit(user.id, Decimal("100"), "BET")

    assert await service.get_balance(user.id) == Decimal("50")


@pytest.mark.asyncio
async def test_transaction_references_are_unique(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    service = WalletService(db_session, fake_redis)

    tx1 = await service.debit(user.id, Decimal("10"), "BET")
    tx2 = await service.debit(user.id, Decimal("10"), "BET")

    assert tx1.reference != tx2.reference


# ========== Dépôt ==========

@pytest.mark.asyncio
async def test_deposit_cash_credits_balance_immediately(db_session, fake_redis, make_user):
    """Espèces au bureau : pas de passerelle à attendre, crédité tout de suite."""
    user = await make_user()
    service = WalletService(db_session, fake_redis)

    result = await service.deposit(
        user.id,
        DepositRequest(amount=500, payment_method=PaymentMethod.CASH),
        ip_address="127.0.0.1",
    )

    assert result["success"] is True
    assert result["status"] == "completed"
    assert result["new_balance"] == 500
    assert await service.get_balance(user.id) == Decimal("500")


@pytest.mark.asyncio
async def test_deposit_moncash_stays_pending_until_confirmed(db_session, fake_redis, make_user):
    """MonCash/NatCash : la transaction reste pending et le solde ne bouge
    pas tant que le fournisseur n'a pas confirmé (cf. confirm_deposit)."""
    user = await make_user()
    service = WalletService(db_session, fake_redis)

    result = await service.deposit(
        user.id,
        DepositRequest(amount=500, payment_method=PaymentMethod.MONCASH),
        ip_address="127.0.0.1",
    )

    assert result["success"] is True
    assert result["status"] == "pending"
    assert result["new_balance"] == 0
    assert result["payment_url"]
    assert await service.get_balance(user.id) == Decimal("0")


@pytest.mark.asyncio
async def test_confirm_deposit_credits_balance_and_is_idempotent(db_session, fake_redis, make_user):
    user = await make_user()
    service = WalletService(db_session, fake_redis)
    result = await service.deposit(
        user.id, DepositRequest(amount=500, payment_method=PaymentMethod.MONCASH)
    )
    transactions = await service.get_transactions(user.id)
    external_reference = transactions[0].external_reference

    confirmed = await service.confirm_deposit(external_reference)

    assert confirmed.status.value == "completed"
    assert await service.get_balance(user.id) == Decimal("500")

    # Un webhook rejoué ne doit pas créditer une seconde fois.
    await service.confirm_deposit(external_reference)
    assert await service.get_balance(user.id) == Decimal("500")


@pytest.mark.asyncio
async def test_fail_deposit_leaves_balance_untouched(db_session, fake_redis, make_user):
    user = await make_user()
    service = WalletService(db_session, fake_redis)
    result = await service.deposit(
        user.id, DepositRequest(amount=500, payment_method=PaymentMethod.MONCASH)
    )
    external_reference = (await service.get_transactions(user.id))[0].external_reference

    failed = await service.fail_deposit(external_reference, reason="Solde mobile money insuffisant")

    assert failed.status.value == "failed"
    assert failed.failure_reason == "Solde mobile money insuffisant"
    assert await service.get_balance(user.id) == Decimal("0")


@pytest.mark.asyncio
async def test_deposit_respects_daily_deposit_limit(db_session, fake_redis, make_user):
    user = await make_user()
    service = WalletService(db_session, fake_redis)
    await service.update_limits(user.id, daily_deposit_limit=Decimal("1000"))

    await service.deposit(user.id, DepositRequest(amount=800, payment_method=PaymentMethod.CASH))

    with pytest.raises(AppException):
        await service.deposit(user.id, DepositRequest(amount=300, payment_method=PaymentMethod.CASH))


@pytest.mark.asyncio
async def test_update_limits_omitted_param_is_left_untouched(db_session, fake_redis, make_user):
    user = await make_user()
    service = WalletService(db_session, fake_redis)
    await service.update_limits(user.id, daily_deposit_limit=Decimal("1000"), single_bet_limit=Decimal("50"))

    # N'ajuste que daily_loss_limit : les deux limites déjà posées doivent survivre.
    wallet = await service.update_limits(user.id, daily_loss_limit=Decimal("2000"))

    assert wallet.daily_deposit_limit == Decimal("1000")
    assert wallet.single_bet_limit == Decimal("50")
    assert wallet.daily_loss_limit == Decimal("2000")


@pytest.mark.asyncio
async def test_update_limits_explicit_none_clears_that_limit_only(db_session, fake_redis, make_user):
    user = await make_user()
    service = WalletService(db_session, fake_redis)
    await service.update_limits(user.id, daily_deposit_limit=Decimal("1000"), single_bet_limit=Decimal("50"))

    wallet = await service.update_limits(user.id, daily_deposit_limit=None)

    assert wallet.daily_deposit_limit is None
    assert wallet.single_bet_limit == Decimal("50")  # non touchée


# ========== Retrait ==========

@pytest.mark.asyncio
async def test_withdraw_cash_completes_immediately(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("5000"), kyc_verified=True)
    service = WalletService(db_session, fake_redis)

    result = await service.withdraw(
        user.id, WithdrawRequest(amount=1000, payment_method=PaymentMethod.CASH)
    )

    assert result["success"] is True
    assert result["status"] == "completed"
    assert await service.get_balance(user.id) == Decimal("4000")


@pytest.mark.asyncio
async def test_withdraw_moncash_reserves_funds_immediately_but_stays_pending(
    db_session, fake_redis, make_user
):
    """Les fonds sont débités (réservés) tout de suite pour empêcher une
    double dépense pendant que le virement externe est en cours."""
    user = await make_user(balance=Decimal("5000"), kyc_verified=True)
    service = WalletService(db_session, fake_redis)

    result = await service.withdraw(
        user.id, WithdrawRequest(amount=1000, payment_method=PaymentMethod.MONCASH)
    )

    assert result["success"] is True
    assert result["status"] == "pending"
    assert await service.get_balance(user.id) == Decimal("4000")


@pytest.mark.asyncio
async def test_confirm_withdrawal_marks_completed_without_moving_balance_again(
    db_session, fake_redis, make_user
):
    user = await make_user(balance=Decimal("5000"), kyc_verified=True)
    service = WalletService(db_session, fake_redis)
    await service.withdraw(user.id, WithdrawRequest(amount=1000, payment_method=PaymentMethod.MONCASH))
    external_reference = (await service.get_transactions(user.id))[0].external_reference

    confirmed = await service.confirm_withdrawal(external_reference)

    assert confirmed.status.value == "completed"
    assert await service.get_balance(user.id) == Decimal("4000")

    wallet = await service.get_by_user_id(user.id)
    assert wallet.total_withdrawn == Decimal("1000")


@pytest.mark.asyncio
async def test_fail_withdrawal_refunds_the_reserved_funds(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("5000"), kyc_verified=True)
    service = WalletService(db_session, fake_redis)
    await service.withdraw(user.id, WithdrawRequest(amount=1000, payment_method=PaymentMethod.MONCASH))
    external_reference = (await service.get_transactions(user.id))[0].external_reference

    failed = await service.fail_withdrawal(external_reference, reason="Numéro mobile invalide")

    assert failed.status.value == "failed"
    assert await service.get_balance(user.id) == Decimal("5000")  # remboursé


@pytest.mark.asyncio
async def test_withdraw_insufficient_balance_raises(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("100"))
    service = WalletService(db_session, fake_redis)

    with pytest.raises(InsufficientBalanceException):
        await service.withdraw(user.id, WithdrawRequest(amount=500, payment_method=PaymentMethod.CASH))


@pytest.mark.asyncio
async def test_withdraw_above_threshold_requires_kyc(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("50000"), kyc_verified=False)
    service = WalletService(db_session, fake_redis)

    with pytest.raises(AppException, match="KYC"):
        await service.withdraw(
            user.id, WithdrawRequest(amount=10000, payment_method=PaymentMethod.MONCASH)
        )

    # Le solde ne doit pas bouger si le retrait est refusé.
    assert await service.get_balance(user.id) == Decimal("50000")


@pytest.mark.asyncio
async def test_withdraw_below_kyc_threshold_allowed_without_verification(
    db_session, fake_redis, make_user
):
    user = await make_user(balance=Decimal("5000"), kyc_verified=False)
    service = WalletService(db_session, fake_redis)

    result = await service.withdraw(
        user.id, WithdrawRequest(amount=500, payment_method=PaymentMethod.CASH)
    )

    assert result["success"] is True
