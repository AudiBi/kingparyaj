# app/api/v1/wallet.py
"""API du portefeuille"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_user
from app.schemas.wallet import (
    WalletResponse, BalanceResponse, DepositRequest, WithdrawRequest,
    TransactionResponse, SetLimitRequest
)
from app.schemas.common import SuccessResponse
from app.services.wallet_service import WalletService
from app.models.user import User
from app.models.enums import PaymentMethod
import redis.asyncio as redis

router = APIRouter()


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Récupère le solde du portefeuille"""
    wallet_service = WalletService(db, redis_client)
    balance = await wallet_service.get_balance(current_user.id)
    return balance


@router.get("/", response_model=WalletResponse)
async def get_wallet(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Récupère les informations du portefeuille"""
    wallet_service = WalletService(db, redis_client)
    wallet = await wallet_service.get_wallet(current_user.id)
    
    return {
        "id": wallet.id,
        "balance": float(wallet.balance),
        "bonus_balance": float(wallet.bonus_balance),
        "total_balance": float(wallet.balance + wallet.bonus_balance),
        "withdrawable_balance": float(wallet.balance),
        "total_deposited": float(wallet.total_deposited),
        "total_withdrawn": float(wallet.total_withdrawn),
        "total_won": float(wallet.total_won),
        "status": wallet.status
    }


@router.post("/deposit", response_model=TransactionResponse)
async def deposit(
    request: Request,
    deposit_data: DepositRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Dépôt d'argent"""
    wallet_service = WalletService(db, redis_client)
    
    payment_method = PaymentMethod(deposit_data.payment_method)
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    
    transaction = await wallet_service.deposit(
        user_id=current_user.id,
        amount=deposit_data.amount,
        payment_method=payment_method,
        external_reference=None,  # Sera mis à jour après paiement
        ip_address=ip_address,
        user_agent=user_agent
    )
    
    return transaction


@router.post("/withdraw", response_model=TransactionResponse)
async def withdraw(
    request: Request,
    withdraw_data: WithdrawRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Retrait d'argent"""
    wallet_service = WalletService(db, redis_client)
    
    payment_method = PaymentMethod(withdraw_data.payment_method)
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    
    transaction = await wallet_service.withdraw(
        user_id=current_user.id,
        amount=withdraw_data.amount,
        payment_method=payment_method,
        destination=withdraw_data.phone,
        ip_address=ip_address,
        user_agent=user_agent
    )
    
    return transaction


@router.get("/transactions", response_model=list[TransactionResponse])
async def get_transactions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
    limit: int = 50,
    offset: int = 0
):
    """Récupère l'historique des transactions"""
    from sqlalchemy import select
    from app.models.transaction import Transaction
    
    result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == current_user.id)
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    transactions = result.scalars().all()
    
    return transactions


@router.post("/limits", response_model=SuccessResponse)
async def set_limit(
    limit_data: SetLimitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Définit une limite de jeu"""
    wallet_service = WalletService(db, redis_client)
    
    from decimal import Decimal
    limit_amount = Decimal(str(limit_data.limit_amount)) if limit_data.limit_amount else None
    
    await wallet_service.set_limit(
        user_id=current_user.id,
        limit_type=limit_data.limit_type,
        limit_amount=limit_amount
    )
    
    return SuccessResponse(message="Limite mise à jour")