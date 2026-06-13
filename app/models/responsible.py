# app/models/responsible.py
"""Modèles pour le jeu responsable (self-exclusion, limites)"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Column, String, DateTime, ForeignKey, 
    Enum, Text, Numeric, Boolean, Integer, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import ExclusionType, ExclusionReason


class SelfExclusion(BaseModel):
    """
    Auto-exclusion d'un joueur.
    Requis par la conformité LEH pour le jeu responsable.
    """
    __tablename__ = "self_exclusions"
    __table_args__ = (
        Index("idx_self_exclusions_user_id", "user_id"),
        Index("idx_self_exclusions_is_active", "is_active"),
        Index("idx_self_exclusions_start_date", "start_date"),
        Index("idx_self_exclusions_end_date", "end_date"),
    )
    
    # ========== Clés étrangères ==========
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    
    # ========== Type et période ==========
    exclusion_type = Column(Enum(ExclusionType), nullable=False)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=True)          # Null pour permanent
    
    # ========== Raison ==========
    reason = Column(Enum(ExclusionReason), nullable=False)
    reason_details = Column(Text, nullable=True)
    
    # ========== Statut ==========
    is_active = Column(Boolean, default=True, nullable=False)
    activated_at = Column(DateTime, nullable=False)
    activated_by = Column(String(36), nullable=True)    # Admin ou système
    
    # ========== Levée d'exclusion ==========
    lifted_at = Column(DateTime, nullable=True)
    lifted_by = Column(String(36), nullable=True)
    lift_reason = Column(Text, nullable=True)
    
    # ========== Comportement détecté (optionnel) ==========
    detected_losses = Column(Numeric(12, 2), nullable=True)
    detected_bets_count = Column(Integer, nullable=True)
    detection_period_days = Column(Integer, nullable=True)
    
    # ========== Relations ==========
    user = relationship("User", back_populates="self_exclusions")
    
    # ========== Méthodes ==========
    def is_active_exclusion(self) -> bool:
        """Vérifie si l'exclusion est encore active"""
        from datetime import datetime
        if not self.is_active:
            return False
        if self.end_date and datetime.utcnow() > self.end_date:
            return False
        return True
    
    def lift(self, lifted_by: str, reason: str = None) -> None:
        """Lève l'exclusion"""
        self.is_active = False
        self.lifted_at = datetime.utcnow()
        self.lifted_by = lifted_by
        self.lift_reason = reason
    
    def __repr__(self) -> str:
        return f"<SelfExclusion user={self.user_id} type={self.exclusion_type}>"


class PlayerLimit(BaseModel):
    """
    Limites personnalisées par joueur.
    """
    __tablename__ = "player_limits"
    __table_args__ = (
        Index("idx_player_limits_user_id", "user_id"),
        Index("idx_player_limits_limit_type", "limit_type"),
        Index("idx_player_limits_is_active", "is_active"),
    )
    
    # ========== Clés étrangères ==========
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    
    # ========== Type de limite ==========
    limit_type = Column(String(30), nullable=False)     # DEPOSIT_DAILY, LOSS_DAILY, BET_SINGLE, etc.
    limit_amount = Column(Numeric(10, 2), nullable=False)
    
    # ========== Période ==========
    period_days = Column(Integer, nullable=True)        # Pour limites récurrentes
    
    # ========== Statut ==========
    is_active = Column(Boolean, default=True, nullable=False)
    
    # ========== Audit ==========
    set_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    set_by = Column(String(36), nullable=True)          # User lui-même ou admin
    
    # ========== Historique ==========
    previous_limit = Column(Numeric(10, 2), nullable=True)
    modified_at = Column(DateTime, nullable=True)
    modified_by = Column(String(36), nullable=True)
    
    # ========== Méthodes ==========
    def update_limit(self, new_amount: Decimal, modified_by: str) -> None:
        """Met à jour la limite"""
        self.previous_limit = self.limit_amount
        self.limit_amount = new_amount
        self.modified_at = datetime.utcnow()
        self.modified_by = modified_by
    
    def __repr__(self) -> str:
        return f"<PlayerLimit user={self.user_id} type={self.limit_type} amount={self.limit_amount}>"