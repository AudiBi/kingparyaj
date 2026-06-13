# app/services/wallet_service.py
"""Service de gestion du portefeuille et transactions"""

from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
import redis.asyncio as redis

from app.core.exceptions import AppException, InsufficientBalanceException
from app.core.logger import get_logger
from app.models.wallet import Wallet, WalletStatus
from app.models.user import User
from app.models.transaction import Transaction, TransactionType, TransactionStatus, PaymentMethod
from app.services.base import BaseService
from app.services.audit_service import AuditService, AuditAction
from app.schemas.wallet import DepositRequest, WithdrawRequest


class WalletService(BaseService[Wallet, None, None]):
    """Service de gestion du portefeuille"""
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, Wallet)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("WalletService")
    
    async def get_or_create(self, user_id: str) -> Wallet:
        """Récupère ou crée un portefeuille pour un utilisateur"""
        result = await self.db.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        wallet = result.scalar_one_or_none()
        
        if not wallet:
            wallet = Wallet(user_id=user_id, balance=Decimal("0"))
            self.db.add(wallet)
            await self.db.flush()
            self.logger.info(f"Wallet created for user: {user_id}")
        
        return wallet
    
    async def get_by_user_id(self, user_id: str) -> Optional[Wallet]:
        """Récupère le portefeuille d'un utilisateur"""
        result = await self.db.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        return result.scalar_one_or_none()
    
    async def get_balance(self, user_id: str) -> Decimal:
        """Récupère le solde d'un utilisateur"""
        wallet = await self.get_by_user_id(user_id)
        return wallet.balance if wallet else Decimal("0")
    
    async def debit(
        self,
        user_id: str,
        amount: Decimal,
        transaction_type: str,
        reference_id: str = None,
        description: str = None
    ) -> Transaction:
        """Débite le portefeuille d'un utilisateur"""
        wallet = await self.get_or_create(user_id)
        
        if wallet.balance < amount:
            raise InsufficientBalanceException(float(amount), float(wallet.balance))
        
        old_balance = wallet.balance
        wallet.balance -= amount
        wallet.updated_at = datetime.utcnow()
        
        # Mettre à jour les compteurs journaliers
        await self._update_daily_counters(wallet, amount, is_debit=True)
        
        transaction = Transaction(
            user_id=user_id,
            wallet_id=wallet.id,
            transaction_type=transaction_type,
            amount=amount,
            balance_before=old_balance,
            balance_after=wallet.balance,
            status=TransactionStatus.COMPLETED,
            completed_at=datetime.utcnow(),
            metadata={"description": description, "reference_id": reference_id}
        )
        
        self.db.add(transaction)
        await self.db.flush()
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.BET_PLACED if transaction_type == "BET" else None,
            resource_type="wallet",
            resource_id=wallet.id,
            new_values={"balance": float(wallet.balance), "debited": float(amount)}
        )
        
        self.logger.info(f"Debited {amount} from user {user_id}. New balance: {wallet.balance}")
        
        return transaction
    
    async def credit(
        self,
        user_id: str,
        amount: Decimal,
        transaction_type: str,
        reference_id: str = None,
        description: str = None
    ) -> Transaction:
        """Crédite le portefeuille d'un utilisateur"""
        wallet = await self.get_or_create(user_id)
        
        old_balance = wallet.balance
        wallet.balance += amount
        
        if transaction_type == "WIN":
            wallet.total_won += amount
        
        wallet.updated_at = datetime.utcnow()
        
        # Mettre à jour les compteurs journaliers
        await self._update_daily_counters(wallet, amount, is_debit=False)
        
        transaction = Transaction(
            user_id=user_id,
            wallet_id=wallet.id,
            transaction_type=transaction_type,
            amount=amount,
            balance_before=old_balance,
            balance_after=wallet.balance,
            status=TransactionStatus.COMPLETED,
            completed_at=datetime.utcnow(),
            metadata={"description": description, "reference_id": reference_id}
        )
        
        self.db.add(transaction)
        await self.db.flush()
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.DEPOSIT if transaction_type == "DEPOSIT" else None,
            resource_type="wallet",
            resource_id=wallet.id,
            new_values={"balance": float(wallet.balance), "credited": float(amount)}
        )
        
        self.logger.info(f"Credited {amount} to user {user_id}. New balance: {wallet.balance}")
        
        return transaction
    
    async def deposit(
        self,
        user_id: str,
        request: DepositRequest,
        ip_address: str = None
    ) -> Dict[str, Any]:
        """Effectue un dépôt"""
        # Vérifier les limites
        wallet = await self.get_or_create(user_id)
        
        if wallet.daily_deposit_limit:
            today_deposits = await self._get_today_deposits(user_id)
            if today_deposits + request.amount > wallet.daily_deposit_limit:
                raise AppException(400, f"Limite de dépôt journalière atteinte ({wallet.daily_deposit_limit} HTG)")
        
        # Créer la transaction
        transaction = await self.credit(
            user_id=user_id,
            amount=Decimal(str(request.amount)),
            transaction_type="DEPOSIT",
            description=f"Dépôt via {request.payment_method}"
        )
        
        return {
            "success": True,
            "transaction_id": transaction.id,
            "reference": transaction.reference,
            "amount": float(request.amount),
            "new_balance": float(wallet.balance),
            "message": f"Dépôt de {request.amount} HTG effectué avec succès"
        }
    
    async def withdraw(
        self,
        user_id: str,
        request: WithdrawRequest,
        ip_address: str = None
    ) -> Dict[str, Any]:
        """Effectue un retrait"""
        wallet = await self.get_or_create(user_id)
        
        if wallet.balance < request.amount:
            raise InsufficientBalanceException(request.amount, float(wallet.balance))
        
        # Vérifier KYC pour retraits importants
        user = await self.db.get(User, user_id)
        if request.amount >= 10000 and user.kyc_status != "verified":
            raise AppException(400, "Veuillez compléter votre vérification KYC avant de retirer")
        
        transaction = await self.debit(
            user_id=user_id,
            amount=Decimal(str(request.amount)),
            transaction_type="WITHDRAWAL",
            description=f"Retrait via {request.payment_method}"
        )
        
        return {
            "success": True,
            "transaction_id": transaction.id,
            "reference": transaction.reference,
            "amount": float(request.amount),
            "new_balance": float(wallet.balance),
            "message": f"Retrait de {request.amount} HTG effectué"
        }
    
    async def get_transactions(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 50,
        transaction_type: str = None
    ) -> List[Transaction]:
        """Récupère l'historique des transactions"""
        query = select(Transaction).where(Transaction.user_id == user_id)
        
        if transaction_type:
            query = query.where(Transaction.transaction_type == transaction_type)
        
        query = query.order_by(Transaction.created_at.desc()).offset(skip).limit(limit)
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    async def update_limits(
        self,
        user_id: str,
        daily_deposit_limit: Optional[Decimal] = None,
        daily_loss_limit: Optional[Decimal] = None,
        single_bet_limit: Optional[Decimal] = None
    ) -> Wallet:
        """Met à jour les limites du joueur"""
        wallet = await self.get_or_create(user_id)
        
        if daily_deposit_limit is not None:
            wallet.daily_deposit_limit = daily_deposit_limit
        if daily_loss_limit is not None:
            wallet.daily_loss_limit = daily_loss_limit
        if single_bet_limit is not None:
            wallet.single_bet_limit = single_bet_limit
        
        wallet.updated_at = datetime.utcnow()
        await self.db.flush()
        
        self.logger.info(f"Limits updated for user {user_id}")
        
        return wallet
    
    async def _update_daily_counters(self, wallet: Wallet, amount: Decimal, is_debit: bool) -> None:
        """Met à jour les compteurs journaliers"""
        today = date.today()
        
        if wallet.last_reset_date and wallet.last_reset_date.date() != today:
            # Réinitialiser les compteurs quotidiens
            wallet.today_deposits = Decimal("0")
            wallet.today_losses = Decimal("0")
            wallet.today_bets = Decimal("0")
            wallet.last_reset_date = datetime.utcnow()
        
        if not wallet.last_reset_date:
            wallet.last_reset_date = datetime.utcnow()
        
        if is_debit:
            wallet.today_bets += amount
            wallet.today_losses += amount
        else:
            wallet.today_deposits += amount
    
    async def _get_today_deposits(self, user_id: str) -> Decimal:
        """Récupère le total des dépôts du jour"""
        today_start = datetime.combine(date.today(), datetime.min.time())
        result = await self.db.execute(
            select(func.sum(Transaction.amount))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.transaction_type == "DEPOSIT",
                    Transaction.created_at >= today_start,
                    Transaction.status == TransactionStatus.COMPLETED
                )
            )
        )
        return result.scalar() or Decimal("0")