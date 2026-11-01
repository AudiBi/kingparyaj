# tests/test_services/test_transaction_service.py
"""Transactions : recherche, statistiques joueur et plateforme, rapport quotidien, export,
rapprochement (confirmer / faire échouer une transaction en attente)."""

import csv
import json
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from sqlalchemy import select

from app.core.exceptions import AppException, NotFoundException
from app.models.enums import PaymentMethod, TransactionStatus, TransactionType
from app.models.transaction import Transaction
from app.models.wallet import Wallet
from app.services.transaction_service import TransactionService
from app.services.wallet_service import WalletService

@pytest.fixture
def service(db_session, fake_redis):
    return TransactionService(db_session, fake_redis)


async def _tx(db_session, user, kind, amount, ref, status=TransactionStatus.COMPLETED, method=None, days_ago=0):
    wallet = (await db_session.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    tx = Transaction(
        user_id=user.id, wallet_id=wallet.id, reference=ref, transaction_type=kind, amount=Decimal(amount),
        balance_before=Decimal(0), balance_after=Decimal(amount), status=status, payment_method=method,
        created_at=datetime.utcnow() - timedelta(days=days_ago, minutes=1),
    )
    db_session.add(tx)
    await db_session.flush()
    return tx


@pytest.fixture
async def history(db_session, make_user):
    """Joueur A : dépôt 1000 (MonCash), pari 400, gain 100, retrait 200, dépôt en attente 50 ; joueur B : pari 300."""
    a, b = await make_user(), await make_user()
    await _tx(db_session, a, TransactionType.DEPOSIT, "1000", "A-DEP", method=PaymentMethod.MONCASH)
    await _tx(db_session, a, TransactionType.BET, "400", "A-BET")
    await _tx(db_session, a, TransactionType.WIN, "100", "A-WIN")
    await _tx(db_session, a, TransactionType.WITHDRAWAL, "200", "A-WD", method=PaymentMethod.NATCASH)
    await _tx(db_session, a, TransactionType.DEPOSIT, "50", "A-PEND", status=TransactionStatus.PENDING)
    await _tx(db_session, a, TransactionType.BET, "999", "A-OLD", days_ago=60)
    await _tx(db_session, b, TransactionType.BET, "300", "B-BET")
    return a, b


# ----------------------------------------------------------------------------- recherche

@pytest.mark.asyncio
async def test_lookup_and_filters(service, history):
    a, b = history
    assert (await service.get_by_reference("A-WIN")).amount == 100
    assert await service.get_by_reference("inconnue") is None

    assert len(await service.get_user_transactions(a.id)) == 6
    assert len(await service.get_user_transactions(a.id, limit=2)) == 2
    assert len(await service.get_user_transactions(a.id, skip=5)) == 1
    assert {t.reference for t in await service.get_user_transactions(a.id, transaction_type=TransactionType.BET)} == {"A-BET", "A-OLD"}
    assert [t.reference for t in await service.get_user_transactions(a.id, status=TransactionStatus.PENDING)] == ["A-PEND"]
    recent = await service.get_user_transactions(a.id, start_date=datetime.utcnow() - timedelta(days=1), end_date=datetime.utcnow() + timedelta(minutes=1))
    assert "A-OLD" not in {t.reference for t in recent}

    period = await service.get_transactions_by_period(datetime.utcnow() - timedelta(days=1), datetime.utcnow() + timedelta(minutes=1))
    assert {t.reference for t in period} == {"A-DEP", "A-BET", "A-WIN", "A-WD", "B-BET"}  # terminées, période courante
    only_bets = await service.get_transactions_by_period(datetime.utcnow() - timedelta(days=1), datetime.utcnow() + timedelta(minutes=1), TransactionType.BET)
    assert {t.reference for t in only_bets} == {"A-BET", "B-BET"}


# ----------------------------------------------------------------------------- statistiques

@pytest.mark.asyncio
async def test_user_statistics(service, history):
    a, _ = history
    stats = await service.get_user_statistics(a.id, days=30)
    assert (stats["total_deposits"], stats["total_withdrawals"], stats["total_bets"], stats["total_wins"]) == (1000.0, 200.0, 400.0, 100.0)
    assert stats["net_result"] == -300.0 and stats["period_days"] == 30
    assert stats["daily_stats"] and stats["daily_stats"][0]["amount"] == 1700.0  # 1000 + 400 + 100 + 200
    assert (await service.get_user_statistics(a.id, days=90))["total_bets"] == 1399.0  # inclut le pari de 60 jours


@pytest.mark.asyncio
async def test_platform_statistics(service, history):
    stats = await service.get_platform_statistics(datetime.utcnow() - timedelta(days=1), datetime.utcnow() + timedelta(minutes=1))
    assert stats["total_volume"] == 2000.0 and stats["total_transactions"] == 5
    assert stats["by_type"]["bet"] == {"total": 700.0, "count": 2}
    assert stats["by_type"]["deposit"]["total"] == 1000.0
    assert stats["by_payment_method"]["moncash"] == {"total": 1000.0, "count": 1}
    assert stats["by_payment_method"]["natcash"]["count"] == 1
    assert stats["period"]["start_date"] < stats["period"]["end_date"]


@pytest.mark.asyncio
async def test_daily_report(service, history, db_session):
    report = await service.generate_daily_report()
    assert report["deposits"] == {"total": 1000.0, "count": 1} and report["withdrawals"] == {"total": 200.0, "count": 1}
    assert report["bets"] == {"total": 700.0, "count": 2} and report["wins"] == {"total": 100.0, "count": 1}
    assert report["net_cashflow"] == 800.0 and report["house_win"] == 600.0
    assert report["payout_rate"] == round(100 / 700 * 100, 2)
    quiet = await service.generate_daily_report(datetime.utcnow() - timedelta(days=200))
    assert quiet["bets"]["count"] == 0 and quiet["payout_rate"] == 0 and quiet["house_win"] == 0.0


# ----------------------------------------------------------------------------- export

@pytest.mark.asyncio
async def test_export_only_contains_the_users_own_transactions(service, history):
    a, b = history
    start, end = datetime.utcnow() - timedelta(days=1), datetime.utcnow() + timedelta(minutes=1)
    rows = list(csv.reader(StringIO(await service.export_transactions(a.id, start, end, "csv"))))
    assert rows[0][:3] == ["Date", "Référence", "Type"]
    assert {r[1] for r in rows[1:]} == {"A-DEP", "A-BET", "A-WIN", "A-WD"}  # jamais B-BET
    assert next(r for r in rows if r[1] == "A-DEP")[-1] == "moncash"

    exported = json.loads(await service.export_transactions(b.id, start, end, "json"))
    assert [t["reference"] for t in exported] == ["B-BET"] and exported[0]["payment_method"] is None
    assert await service.export_transactions(a.id, start, end, "xml") == ""
    assert await service.export_transactions(a.id, datetime.utcnow() - timedelta(days=400), datetime.utcnow() - timedelta(days=300)) == ""


# ----------------------------------------------------------------------------- rapprochement

@pytest.mark.asyncio
async def test_pending_transactions_can_be_completed_once(service, history):
    pending = await service.get_pending_transactions()
    assert [t.reference for t in pending] == ["A-PEND"]
    done = await service.mark_as_completed(pending[0].id)
    assert done.status == TransactionStatus.COMPLETED and done.completed_at is not None
    assert await service.get_pending_transactions() == []
    with pytest.raises(AppException, match="déjà"):
        await service.mark_as_completed(done.id)
    with pytest.raises(NotFoundException):
        await service.mark_as_completed("inconnue")


@pytest.mark.asyncio
async def test_failed_debit_is_refunded(service, make_user, db_session, fake_redis):
    user = await make_user(balance=Decimal("0"))
    bet = await _tx(db_session, user, TransactionType.BET, "75", "F-BET", status=TransactionStatus.PENDING)
    failed = await service.mark_as_failed(bet.id, "Erreur passerelle")
    assert failed.status == TransactionStatus.FAILED and failed.failure_reason == "Erreur passerelle"
    assert await WalletService(db_session, fake_redis).get_balance(user.id) == Decimal("75")  # remboursé
    with pytest.raises(AppException, match="déjà"):
        await service.mark_as_failed(bet.id, "encore")

    deposit = await _tx(db_session, user, TransactionType.DEPOSIT, "20", "F-DEP", status=TransactionStatus.PENDING)
    await service.mark_as_failed(deposit.id, "Refusé")
    assert await WalletService(db_session, fake_redis).get_balance(user.id) == Decimal("75")  # un dépôt raté ne crédite rien
