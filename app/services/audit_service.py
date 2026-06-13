# app/services/audit_service.py
"""Service de journal d'audit pour conformité LEH"""

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
import redis.asyncio as redis
import json

from app.core.logger import get_logger
from app.models.audit import AuditLog
from app.models.enums import AuditAction
from app.services.base import BaseService


class AuditService(BaseService[AuditLog, None, None]):
    """
    Service pour le journal d'audit.
    TOUTE action importante doit être loguée.
    Rétention: 7 ans minimum (conformité LEH).
    """
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, AuditLog)
        self.redis = redis_client
        self.logger = get_logger("AuditService")
    
    async def log(
        self,
        action: AuditAction,
        user_id: str = None,
        agent_id: str = None,
        resource_type: str = None,
        resource_id: str = None,
        old_values: Dict[str, Any] = None,
        new_values: Dict[str, Any] = None,
        reason: str = None,
        ip_address: str = None,
        user_agent: str = None,
        session_id: str = None,
        extra_data: Dict[str, Any] = None
    ) -> AuditLog:
        """
        Enregistre une action dans le journal d'audit
        """
        audit_log = AuditLog(
            user_id=user_id,
            agent_id=agent_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            old_values=old_values,
            new_values=new_values,
            reason=reason,
            extra_data=extra_data or {},
            ip_address=ip_address or "0.0.0.0",
            user_agent=user_agent,
            session_id=session_id,
            created_at=datetime.utcnow()
        )
        
        self.db.add(audit_log)
        await self.db.flush()
        
        self.logger.debug(f"Audit log: {action.value} - user={user_id or agent_id}")
        
        return audit_log
    
    async def get_user_audit(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 100,
        start_date: datetime = None,
        end_date: datetime = None,
        actions: List[AuditAction] = None
    ) -> List[AuditLog]:
        """Récupère l'audit d'un utilisateur"""
        query = select(AuditLog).where(AuditLog.user_id == user_id)
        
        if start_date:
            query = query.where(AuditLog.created_at >= start_date)
        if end_date:
            query = query.where(AuditLog.created_at <= end_date)
        if actions:
            query = query.where(AuditLog.action.in_(actions))
        
        query = query.order_by(AuditLog.created_at.desc()).offset(skip).limit(limit)
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    async def get_audit_by_resource(
        self,
        resource_type: str,
        resource_id: str,
        skip: int = 0,
        limit: int = 100
    ) -> List[AuditLog]:
        """Récupère l'audit d'une ressource spécifique"""
        result = await self.db.execute(
            select(AuditLog)
            .where(
                and_(
                    AuditLog.resource_type == resource_type,
                    AuditLog.resource_id == resource_id
                )
            )
            .order_by(AuditLog.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def export_for_leh(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict[str, Any]]:
        """
        Exporte les logs pour la conformité LEH
        Format à définir avec la LEH
        """
        result = await self.db.execute(
            select(AuditLog)
            .where(
                and_(
                    AuditLog.created_at >= start_date,
                    AuditLog.created_at <= end_date,
                    AuditLog.leh_exported == False
                )
            )
            .order_by(AuditLog.created_at)
        )
        logs = result.scalars().all()
        
        # Formater selon spécifications LEH
        export_data = []
        for log in logs:
            export_data.append({
                "log_id": log.id,
                "timestamp": log.created_at.isoformat(),
                "user_id": log.user_id,
                "agent_id": log.agent_id,
                "action": log.action.value,
                "resource_type": log.resource_type,
                "resource_id": log.resource_id,
                "old_values": log.old_values,
                "new_values": log.new_values,
                "reason": log.reason,
                "extra_data": log.extra_data,
                "ip_address": log.ip_address,
                "session_id": log.session_id
            })
            
            # Marquer comme exporté
            log.leh_exported = True
            log.leh_exported_at = datetime.utcnow()
        
        await self.db.flush()
        
        self.logger.info(f"Exported {len(export_data)} audit logs for LEH")
        
        return export_data
    
    async def cleanup_old_logs(self, retention_days: int = 2555) -> int:
        """
        Nettoie les vieux logs (soft delete)
        Rétention par défaut: 7 ans (2555 jours)
        """
        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)
        
        result = await self.db.execute(
            select(AuditLog).where(AuditLog.created_at < cutoff_date)
        )
        old_logs = result.scalars().all()
        
        for log in old_logs:
            log.soft_delete()
        
        await self.db.flush()
        
        self.logger.info(f"Cleaned up {len(old_logs)} old audit logs")
        
        return len(old_logs)