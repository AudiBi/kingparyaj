# app/api/v1/wallet.py
"""API du portefeuille"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_user
from app.schemas.wallet import (
    WalletResponse, BalanceResponse, DepositRequest, DepositResponse,
    WithdrawRequest, WithdrawResponse, TransactionResponse, SetLimitRequest
)
from app.schemas.common import SuccessResponse
from app.services.wallet_service import WalletService
from app.models.user import User
import redis.asyncio as redis

router = APIRouter()

_LIMIT_TYPE_TO_KWARG = {
    "daily_deposit": "daily_deposit_limit",
    "daily_loss": "daily_loss_limit",
    "weekly_deposit": "weekly_deposit_limit",
    "monthly_deposit": "monthly_deposit_limit",
    "single_bet": "single_bet_limit",
}


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Récupère le solde du portefeuille"""
    wallet_service = WalletService(db, redis_client)
    wallet = await wallet_service.get_or_create(current_user.id)
    return BalanceResponse(
        balance=float(wallet.balance),
        bonus_balance=float(wallet.bonus_balance),
        total_balance=float(wallet.balance + wallet.bonus_balance),
    )


@router.get("/", response_model=WalletResponse)
async def get_wallet(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Récupère les informations du portefeuille"""
    wallet_service = WalletService(db, redis_client)
    wallet = await wallet_service.get_or_create(current_user.id)

    return {
        "id": wallet.id,
        "user_id": wallet.user_id,
        "balance": float(wallet.balance),
        "bonus_balance": float(wallet.bonus_balance),
        "total_balance": float(wallet.balance + wallet.bonus_balance),
        "withdrawable_balance": float(wallet.balance),
        "total_deposited": float(wallet.total_deposited),
        "total_withdrawn": float(wallet.total_withdrawn),
        "total_won": float(wallet.total_won),
        "status": wallet.status,
        "daily_deposit_limit": float(wallet.daily_deposit_limit) if wallet.daily_deposit_limit else None,
        "daily_loss_limit": float(wallet.daily_loss_limit) if wallet.daily_loss_limit else None,
        "single_bet_limit": float(wallet.single_bet_limit) if wallet.single_bet_limit else None,
    }


@router.post("/deposit", response_model=DepositResponse)
async def deposit(
    request: Request,
    deposit_data: DepositRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Dépôt d'argent. Espèces/virement : crédité immédiatement. MonCash/
    NatCash : reste en attente jusqu'à confirmation du fournisseur (webhook
    /api/v1/payments/{provider}/webhook, ou /simulate hors production)."""
    wallet_service = WalletService(db, redis_client)
    ip_address = request.client.host if request.client else None

    result = await wallet_service.deposit(
        user_id=current_user.id,
        request=deposit_data,
        ip_address=ip_address,
    )
    await db.commit()

    return result


@router.post("/withdraw", response_model=WithdrawResponse)
async def withdraw(
    request: Request,
    withdraw_data: WithdrawRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Retrait d'argent. Espèces/virement : réglé immédiatement. MonCash/
    NatCash : fonds réservés tout de suite, retrait confirmé (ou remboursé
    en cas d'échec) via webhook/simulate, comme pour le dépôt."""
    wallet_service = WalletService(db, redis_client)
    ip_address = request.client.host if request.client else None

    result = await wallet_service.withdraw(
        user_id=current_user.id,
        request=withdraw_data,
        ip_address=ip_address,
    )
    await db.commit()

    return result


@router.get("/transactions", response_model=list[TransactionResponse])
async def get_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
    limit: int = 50,
    offset: int = 0
):
    """Récupère l'historique des transactions"""
    wallet_service = WalletService(db, redis_client)
    return await wallet_service.get_transactions(current_user.id, skip=offset, limit=limit)


@router.post("/limits", response_model=SuccessResponse)
async def set_limit(
    limit_data: SetLimitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Définit une limite de jeu"""
    kwarg = _LIMIT_TYPE_TO_KWARG.get(limit_data.limit_type)
    if not kwarg:
        raise HTTPException(
            status_code=400,
            detail=f"Type de limite inconnu: {limit_data.limit_type}. "
                   f"Attendu: {', '.join(_LIMIT_TYPE_TO_KWARG)}",
        )

    from decimal import Decimal
    limit_amount = Decimal(str(limit_data.limit_amount)) if limit_data.limit_amount is not None else None

    wallet_service = WalletService(db, redis_client)
    await wallet_service.update_limits(user_id=current_user.id, **{kwarg: limit_amount})
    await db.commit()

    return SuccessResponse(message="Limite mise à jour")
