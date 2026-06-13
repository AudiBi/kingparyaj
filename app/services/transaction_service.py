# app/services/transaction_service.py
"""Service pour la gestion des transactions financières"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
import redis.asyncio as redis

from app.core.exceptions import AppException, NotFoundException
from app.core.logger import get_logger
from app.models.transaction import Transaction, TransactionType, TransactionStatus, PaymentMethod
from app.models.user import User
from app.models.wallet import Wallet
from app.services.base import BaseService
from app.services.audit_service import AuditService, AuditAction
from app.schemas.transaction import TransactionFilter


class TransactionService(BaseService[Transaction, None, None]):
    """
    Service pour la gestion des transactions financières.
    Permet le suivi complet des dépôts, retraits, paris et gains.
    """
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, Transaction)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("TransactionService")
    
    # ========== Méthodes de base ==========
    
    async def get_by_reference(self, reference: str) -> Optional[Transaction]:
        """Récupère une transaction par sa référence"""
        result = await self.db.execute(
            select(Transaction).where(Transaction.reference == reference)
        )
        return result.scalar_one_or_none()
    
    async def get_user_transactions(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 50,
        transaction_type: Optional[TransactionType] = None,
        status: Optional[TransactionStatus] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> List[Transaction]:
        """Récupère les transactions d'un utilisateur avec filtres"""
        query = select(Transaction).where(Transaction.user_id == user_id)
        
        if transaction_type:
            query = query.where(Transaction.transaction_type == transaction_type)
        if status:
            query = query.where(Transaction.status == status)
        if start_date:
            query = query.where(Transaction.created_at >= start_date)
        if end_date:
            query = query.where(Transaction.created_at <= end_date)
        
        query = query.order_by(Transaction.created_at.desc()).offset(skip).limit(limit)
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    async def get_transactions_by_period(
        self,
        start_date: datetime,
        end_date: datetime,
        transaction_type: Optional[TransactionType] = None
    ) -> List[Transaction]:
        """Récupère les transactions sur une période donnée"""
        query = select(Transaction).where(
            and_(
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
                Transaction.status == TransactionStatus.COMPLETED
            )
        )
        
        if transaction_type:
            query = query.where(Transaction.transaction_type == transaction_type)
        
        query = query.order_by(Transaction.created_at)
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    # ========== Statistiques ==========
    
    async def get_user_statistics(
        self,
        user_id: str,
        days: int = 30
    ) -> Dict[str, Any]:
        """Récupère les statistiques financières d'un utilisateur"""
        
        start_date = datetime.utcnow() - timedelta(days=days)
        
        # Total dépôts
        deposits_result = await self.db.execute(
            select(func.sum(Transaction.amount))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.transaction_type == TransactionType.DEPOSIT,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date
                )
            )
        )
        total_deposits = deposits_result.scalar() or Decimal("0")
        
        # Total retraits
        withdrawals_result = await self.db.execute(
            select(func.sum(Transaction.amount))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.transaction_type == TransactionType.WITHDRAWAL,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date
                )
            )
        )
        total_withdrawals = withdrawals_result.scalar() or Decimal("0")
        
        # Total paris
        bets_result = await self.db.execute(
            select(func.sum(Transaction.amount))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.transaction_type == TransactionType.BET,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date
                )
            )
        )
        total_bets = bets_result.scalar() or Decimal("0")
        
        # Total gains
        wins_result = await self.db.execute(
            select(func.sum(Transaction.amount))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.transaction_type == TransactionType.WIN,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date
                )
            )
        )
        total_wins = wins_result.scalar() or Decimal("0")
        
        # Transactions par jour (pour graphiques)
        daily_result = await self.db.execute(
            select(
                func.date(Transaction.created_at).label("day"),
                func.sum(Transaction.amount).label("total")
            )
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date
                )
            )
            .group_by(func.date(Transaction.created_at))
            .order_by(func.date(Transaction.created_at))
        )
        
        daily_stats = [
            {"date": str(row.day), "amount": float(row.total)}
            for row in daily_result
        ]
        
        return {
            "period_days": days,
            "total_deposits": float(total_deposits),
            "total_withdrawals": float(total_withdrawals),
            "total_bets": float(total_bets),
            "total_wins": float(total_wins),
            "net_result": float(total_wins - total_bets),
            "daily_stats": daily_stats
        }
    
    async def get_platform_statistics(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> Dict[str, Any]:
        """Récupère les statistiques financières globales"""
        
        # Total volume
        volume_result = await self.db.execute(
            select(
                func.sum(Transaction.amount).label("total_volume"),
                func.count(Transaction.id).label("total_transactions")
            )
            .where(
                and_(
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date,
                    Transaction.created_at <= end_date
                )
            )
        )
        volume_stats = volume_result.one()
        
        # Détail par type
        type_result = await self.db.execute(
            select(
                Transaction.transaction_type,
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.id).label("count")
            )
            .where(
                and_(
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date,
                    Transaction.created_at <= end_date
                )
            )
            .group_by(Transaction.transaction_type)
        )
        
        by_type = {}
        for row in type_result:
            by_type[row.transaction_type.value] = {
                "total": float(row.total),
                "count": row.count
            }
        
        # Par méthode de paiement
        method_result = await self.db.execute(
            select(
                Transaction.payment_method,
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.id).label("count")
            )
            .where(
                and_(
                    Transaction.payment_method.isnot(None),
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date,
                    Transaction.created_at <= end_date
                )
            )
            .group_by(Transaction.payment_method)
        )
        
        by_method = {}
        for row in method_result:
            if row.payment_method:
                by_method[row.payment_method.value] = {
                    "total": float(row.total),
                    "count": row.count
                }
        
        return {
            "period": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat()
            },
            "total_volume": float(volume_stats.total_volume or 0),
            "total_transactions": volume_stats.total_transactions or 0,
            "by_type": by_type,
            "by_payment_method": by_method
        }
    
    # ========== Rapports ==========
    
    async def generate_daily_report(self, date: datetime = None) -> Dict[str, Any]:
        """Génère un rapport financier quotidien"""
        
        if date is None:
            date = datetime.utcnow()
        
        start_date = datetime.combine(date.date(), datetime.min.time())
        end_date = datetime.combine(date.date(), datetime.max.time())
        
        # Dépôts
        deposits = await self.db.execute(
            select(
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.id).label("count")
            )
            .where(
                and_(
                    Transaction.transaction_type == TransactionType.DEPOSIT,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date,
                    Transaction.created_at <= end_date
                )
            )
        )
        deposit_stats = deposits.one()
        
        # Retraits
        withdrawals = await self.db.execute(
            select(
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.id).label("count")
            )
            .where(
                and_(
                    Transaction.transaction_type == TransactionType.WITHDRAWAL,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date,
                    Transaction.created_at <= end_date
                )
            )
        )
        withdrawal_stats = withdrawals.one()
        
        # Paris
        bets = await self.db.execute(
            select(
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.id).label("count")
            )
            .where(
                and_(
                    Transaction.transaction_type == TransactionType.BET,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date,
                    Transaction.created_at <= end_date
                )
            )
        )
        bet_stats = bets.one()
        
        # Gains
        wins = await self.db.execute(
            select(
                func.sum(Transaction.amount).label("total"),
                func.count(Transaction.id).label("count")
            )
            .where(
                and_(
                    Transaction.transaction_type == TransactionType.WIN,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= start_date,
                    Transaction.created_at <= end_date
                )
            )
        )
        win_stats = wins.one()
        
        total_deposits = deposit_stats.total or Decimal("0")
        total_withdrawals = withdrawal_stats.total or Decimal("0")
        total_bets = bet_stats.total or Decimal("0")
        total_wins = win_stats.total or Decimal("0")
        
        return {
            "date": date.date().isoformat(),
            "deposits": {
                "total": float(total_deposits),
                "count": deposit_stats.count or 0
            },
            "withdrawals": {
                "total": float(total_withdrawals),
                "count": withdrawal_stats.count or 0
            },
            "bets": {
                "total": float(total_bets),
                "count": bet_stats.count or 0
            },
            "wins": {
                "total": float(total_wins),
                "count": win_stats.count or 0
            },
            "net_cashflow": float(total_deposits - total_withdrawals),
            "house_win": float(total_bets - total_wins),
            "payout_rate": round(float(total_wins / total_bets * 100) if total_bets > 0 else 0, 2)
        }
    
    # ========== Export ==========
    
    async def export_transactions(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime,
        format: str = "csv"
    ) -> str:
        """Exporte les transactions d'un utilisateur (CSV ou JSON)"""
        
        transactions = await self.get_transactions_by_period(start_date, end_date)
        
        if not transactions:
            return ""
        
        if format == "csv":
            # Générer CSV
            import csv
            from io import StringIO
            
            output = StringIO()
            writer = csv.writer(output)
            
            # En-têtes
            writer.writerow([
                "Date", "Référence", "Type", "Montant", "Frais",
                "Solde avant", "Solde après", "Statut", "Méthode"
            ])
            
            # Données
            for tx in transactions:
                writer.writerow([
                    tx.created_at.isoformat(),
                    tx.reference,
                    tx.transaction_type.value,
                    float(tx.amount),
                    float(tx.fee),
                    float(tx.balance_before),
                    float(tx.balance_after),
                    tx.status.value,
                    tx.payment_method.value if tx.payment_method else ""
                ])
            
            return output.getvalue()
        
        elif format == "json":
            import json
            return json.dumps([
                {
                    "date": tx.created_at.isoformat(),
                    "reference": tx.reference,
                    "type": tx.transaction_type.value,
                    "amount": float(tx.amount),
                    "fee": float(tx.fee),
                    "balance_before": float(tx.balance_before),
                    "balance_after": float(tx.balance_after),
                    "status": tx.status.value,
                    "payment_method": tx.payment_method.value if tx.payment_method else None
                }
                for tx in transactions
            ], indent=2, ensure_ascii=False)
        
        return ""
    
    # ========== Reconcilation ==========
    
    async def get_pending_transactions(self) -> List[Transaction]:
        """Récupère les transactions en attente"""
        result = await self.db.execute(
            select(Transaction)
            .where(Transaction.status == TransactionStatus.PENDING)
            .order_by(Transaction.created_at)
        )
        return result.scalars().all()
    
    async def mark_as_completed(self, transaction_id: str) -> Transaction:
        """Marque une transaction comme complétée"""
        transaction = await self.get_or_raise(transaction_id)
        
        if transaction.status != TransactionStatus.PENDING:
            raise AppException(400, f"Transaction déjà {transaction.status}")
        
        transaction.status = TransactionStatus.COMPLETED
        transaction.completed_at = datetime.utcnow()
        
        await self.db.flush()
        
        self.logger.info(f"Transaction {transaction.reference} marked as completed")
        
        return transaction
    
    async def mark_as_failed(self, transaction_id: str, reason: str) -> Transaction:
        """Marque une transaction comme échouée"""
        transaction = await self.get_or_raise(transaction_id)
        
        if transaction.status != TransactionStatus.PENDING:
            raise AppException(400, f"Transaction déjà {transaction.status}")
        
        transaction.status = TransactionStatus.FAILED
        transaction.failure_reason = reason
        transaction.completed_at = datetime.utcnow()
        
        await self.db.flush()
        
        # Si c'était un débit, recréditer
        if transaction.transaction_type in [TransactionType.BET, TransactionType.WITHDRAWAL]:
            from app.services.wallet_service import WalletService
            wallet_service = WalletService(self.db, self.redis)
            await wallet_service.credit(
                user_id=transaction.user_id,
                amount=transaction.amount,
                transaction_type="REFUND",
                description=f"Remboursement suite à échec: {reason}"
            )
        
        self.logger.warning(f"Transaction {transaction.reference} failed: {reason}")
        
        return transaction