# app/services/bureau_service.py
"""Service pour la gestion des bureaux et des sessions de caisse"""

from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
import redis.asyncio as redis

from app.core.exceptions import AppException, NotFoundException
from app.core.logger import get_logger
from app.models.bureau import Bureau, CashierSession
from app.models.user import User
from app.services.base import BaseService
from app.services.audit_service import AuditService, AuditAction
from app.schemas.bureau import BureauCreate, BureauUpdate, CashierSessionOpen, CashierSessionClose


class BureauService(BaseService[Bureau, BureauCreate, BureauUpdate]):
    """Service pour la gestion des bureaux"""
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, Bureau)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("BureauService")
    
    async def create_bureau(self, data: BureauCreate, user_id: str = None) -> Bureau:
        """Crée un nouveau bureau"""
        # Vérifier code unique
        existing = await self.db.execute(
            select(Bureau).where(Bureau.code == data.code)
        )
        if existing.scalar_one_or_none():
            raise AppException(400, f"Code {data.code} déjà utilisé")
        
        bureau = await self.create(data, user_id)
        
        self.logger.info(f"Bureau created: {bureau.code} - {bureau.name}")
        
        return bureau
    
    async def get_bureau_stats(self, bureau_id: str) -> Dict[str, Any]:
        """Récupère les statistiques d'un bureau"""
        bureau = await self.get_or_raise(bureau_id)
        
        # Nombre d'agents
        agents_result = await self.db.execute(
            select(func.count(User.id)).where(User.bureau_id == bureau_id)
        )
        agents_count = agents_result.scalar() or 0
        
        # Sessions de caisse actives
        active_sessions = await self.db.execute(
            select(CashierSession).where(
                and_(
                    CashierSession.bureau_id == bureau_id,
                    CashierSession.status == "OPEN"
                )
            )
        )
        active_sessions_list = active_sessions.scalars().all()
        
        return {
            "id": bureau.id,
            "name": bureau.name,
            "code": bureau.code,
            "city": bureau.city,
            "cash_balance": float(bureau.cash_balance),
            "safe_balance": float(bureau.safe_balance),
            "agents_count": agents_count,
            "active_sessions_count": len(active_sessions_list),
            "total_cash_in_today": float(bureau.total_cash_in_today),
            "total_cash_out_today": float(bureau.total_cash_out_today),
            "total_bets_today": bureau.total_bets_today,
            "is_active": bureau.is_active
        }


class CashierSessionService(BaseService[CashierSession, CashierSessionOpen, CashierSessionClose]):
    """Service pour les sessions de caisse"""
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, CashierSession)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("CashierSessionService")
    
    async def open_session(
        self,
        bureau_id: str,
        agent_id: str,
        data: CashierSessionOpen
    ) -> CashierSession:
        """Ouvre une nouvelle session de caisse"""
        
        # Vérifier qu'il n'y a pas de session ouverte
        existing = await self.db.execute(
            select(CashierSession).where(
                and_(
                    CashierSession.bureau_id == bureau_id,
                    CashierSession.agent_id == agent_id,
                    CashierSession.status == "OPEN"
                )
            )
        )
        if existing.scalar_one_or_none():
            raise AppException(400, "Une session est déjà ouverte pour cet agent")
        
        # Vérifier le bureau
        bureau = await self.db.get(Bureau, bureau_id)
        if not bureau:
            raise NotFoundException("Bureau", bureau_id)
        
        session = CashierSession(
            bureau_id=bureau_id,
            agent_id=agent_id,
            starting_balance=data.starting_balance,
            current_balance=data.starting_balance,
            expected_balance=data.starting_balance,
            status="OPEN",
            opened_at=datetime.utcnow()
        )
        
        self.db.add(session)
        await self.db.flush()
        
        await self.audit_service.log(
            agent_id=agent_id,
            action=AuditAction.LOGIN,
            resource_type="cashier_session",
            resource_id=session.id,
            new_values={"starting_balance": float(data.starting_balance)}
        )
        
        self.logger.info(f"Cashier session opened: bureau={bureau_id}, agent={agent_id}")
        
        return session
    
    async def close_session(
        self,
        session_id: str,
        agent_id: str,
        data: CashierSessionClose
    ) -> CashierSession:
        """Ferme une session de caisse"""
        
        session = await self.get_or_raise(session_id)
        
        if session.agent_id != agent_id:
            raise AppException(403, "Vous n'êtes pas autorisé à fermer cette session")
        
        if session.status != "OPEN":
            raise AppException(400, f"Session déjà {session.status}")
        
        # Calculer l'écart
        expected = session.starting_balance + session.cash_in_amount - session.cash_out_amount
        difference = data.actual_balance - expected
        
        session.expected_balance = expected
        session.current_balance = data.actual_balance
        session.difference = difference
        session.difference_reason = data.difference_reason
        session.status = "CLOSED"
        session.closed_at = datetime.utcnow()
        
        await self.db.flush()
        
        # Mettre à jour la caisse du bureau
        bureau = await self.db.get(Bureau, session.bureau_id)
        if bureau:
            bureau.cash_balance = data.actual_balance
        
        await self.audit_service.log(
            agent_id=agent_id,
            action=AuditAction.LOGOUT,
            resource_type="cashier_session",
            resource_id=session.id,
            new_values={
                "expected_balance": float(expected),
                "actual_balance": float(data.actual_balance),
                "difference": float(difference),
                "difference_reason": data.difference_reason
            }
        )
        
        self.logger.info(f"Cashier session closed: {session_id}, difference={difference}")
        
        return session
    
    async def add_transaction(
        self,
        session_id: str,
        amount: Decimal,
        transaction_type: str
    ) -> None:
        """Ajoute une transaction à la session (encaissement ou paiement)"""
        
        session = await self.get_or_raise(session_id)
        
        if session.status != "OPEN":
            raise AppException(400, "Session fermée")
        
        if transaction_type == "CASH_IN":
            session.cash_in_count += 1
            session.cash_in_amount += amount
            session.current_balance += amount
        elif transaction_type == "CASH_OUT":
            session.cash_out_count += 1
            session.cash_out_amount += amount
            session.current_balance -= amount
        else:
            raise AppException(400, f"Type de transaction invalide: {transaction_type}")
        
        await self.db.flush()
    
    async def get_current_session(self, agent_id: str) -> Optional[CashierSession]:
        """Récupère la session active d'un agent"""
        result = await self.db.execute(
            select(CashierSession).where(
                and_(
                    CashierSession.agent_id == agent_id,
                    CashierSession.status == "OPEN"
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def get_session_stats(self, bureau_id: str, date: datetime = None) -> Dict[str, Any]:
        """Récupère les statistiques des sessions pour un bureau"""
        
        if date is None:
            date = datetime.utcnow()
        
        date_start = datetime.combine(date.date(), datetime.min.time())
        date_end = datetime.combine(date.date(), datetime.max.time())
        
        result = await self.db.execute(
            select(
                func.count(CashierSession.id).label("total_sessions"),
                func.sum(CashierSession.cash_in_amount).label("total_cash_in"),
                func.sum(CashierSession.cash_out_amount).label("total_cash_out"),
                func.avg(CashierSession.difference).label("avg_difference")
            )
            .where(
                and_(
                    CashierSession.bureau_id == bureau_id,
                    CashierSession.opened_at >= date_start,
                    CashierSession.opened_at <= date_end
                )
            )
        )
        stats = result.one()
        
        return {
            "date": date.date().isoformat(),
            "total_sessions": stats.total_sessions or 0,
            "total_cash_in": float(stats.total_cash_in or 0),
            "total_cash_out": float(stats.total_cash_out or 0),
            "net_cash": float((stats.total_cash_in or 0) - (stats.total_cash_out or 0)),
            "avg_difference": float(stats.avg_difference or 0)
        }