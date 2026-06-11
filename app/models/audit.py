# app/models/audit.py
"""Journal d'audit pour conformité LEH"""

from datetime import datetime

from sqlalchemy import (
    Boolean, Column, String, DateTime, ForeignKey, 
    Enum, Text, JSON, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import AuditAction


class AuditLog(BaseModel):
    """
    Journal d'audit pour conformité LEH.
    TOUTE action importante doit être loguée.
    Rétention: 7 ans minimum.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_logs_user_id", "user_id"),
        Index("idx_audit_logs_action", "action"),
        Index("idx_audit_logs_resource_type", "resource_type"),
        Index("idx_audit_logs_created_at", "created_at"),
        Index("idx_audit_logs_ip_address", "ip_address"),
        Index("idx_audit_logs_leh_exported", "leh_exported"),
    )
    
    # ========== Qui ==========
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    agent_id = Column(String(36), nullable=True)        # Agent qui a agi (si différent)
    ip_address = Column(String(45), nullable=False)
    user_agent = Column(String(500), nullable=True)
    session_id = Column(String(100), nullable=True)
    
    # ========== Quoi ==========
    action = Column(Enum(AuditAction), nullable=False)
    resource_type = Column(String(50), nullable=True)   # user, bet, wallet, etc.
    resource_id = Column(String(36), nullable=True)     # ID de l'objet concerné
    
    # ========== Avant/Après (pour modifications) ==========
    old_values = Column(JSON, nullable=True)
    new_values = Column(JSON, nullable=True)
    
    # ========== Contexte ==========
    reason = Column(Text, nullable=True)
    metadata = Column(JSON, default={})                 # Info supplémentaire
    
    # ========== Pour LEH ==========
    leh_exported = Column(Boolean, default=False, nullable=False)
    leh_exported_at = Column(DateTime, nullable=True)
    
    # ========== Relations ==========
    user = relationship("User", back_populates="audit_logs")
    
    # ========== Méthodes ==========
    def mark_exported(self) -> None:
        """Marque le log comme exporté vers la LEH"""
        self.leh_exported = True
        self.leh_exported_at = datetime.utcnow()
    
    def __repr__(self) -> str:
        return f"<AuditLog user={self.user_id} action={self.action}>"