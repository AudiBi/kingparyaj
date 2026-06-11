# app/models/promotion.py
"""Modèles pour les bonus et promotions"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Column, String, Numeric, Boolean, DateTime, 
    Enum, JSON, Integer, ForeignKey, Text, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import PromotionType, PromotionStatus


class Promotion(BaseModel):
    """
    Promotion ou bonus offert aux joueurs.
    """
    __tablename__ = "promotions"
    __table_args__ = (
        Index("idx_promotions_code", "code", unique=True),
        Index("idx_promotions_type", "type"),
        Index("idx_promotions_status", "status"),
        Index("idx_promotions_start_date", "start_date"),
        Index("idx_promotions_end_date", "end_date"),
    )
    
    # ========== Identification ==========
    name = Column(String(100), nullable=False)
    code = Column(String(50), unique=True, nullable=True)
    description = Column(Text, nullable=True)
    type = Column(Enum(PromotionType), nullable=False)
    
    # ========== Configuration ==========
    # Exemple deposit_bonus: {"min_deposit": 100, "bonus_percent": 100, "max_bonus": 500, "wagering": 10}
    # Exemple cashback: {"percentage": 10, "max_cashback": 1000, "period_days": 7}
    config = Column(JSON, nullable=False)
    
    # ========== Période de validité ==========
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    
    # ========== Conditions ==========
    min_deposit = Column(Numeric(10, 2), nullable=True)
    max_bonus = Column(Numeric(10, 2), nullable=True)
    wagering_requirement = Column(Integer, default=1, nullable=False)  # Mise x fois le bonus
    eligible_games = Column(JSON, default=["keno", "lucky"], nullable=False)
    
    # ========== Public ciblé ==========
    eligible_countries = Column(JSON, default=["HT"], nullable=False)
    min_user_age = Column(Integer, default=18, nullable=False)
    new_users_only = Column(Boolean, default=False, nullable=False)
    first_deposit_only = Column(Boolean, default=False, nullable=False)
    
    # ========== Statut ==========
    status = Column(Enum(PromotionStatus), default=PromotionStatus.DRAFT, nullable=False)
    
    # ========== Budget ==========
    total_budget = Column(Numeric(12, 2), nullable=True)
    used_budget = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Métriques ==========
    total_claims = Column(Integer, default=0, nullable=False)
    total_bonus_given = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Métadonnées ==========
    created_by = Column(String(36), nullable=False)
    
    # ========== Relations ==========
    user_promotions = relationship("UserPromotion", back_populates="promotion", cascade="all, delete-orphan")
    
    # ========== Méthodes ==========
    def is_active(self) -> bool:
        """Vérifie si la promotion est active"""
        from datetime import datetime
        now = datetime.utcnow()
        return (self.status == PromotionStatus.ACTIVE and 
                self.start_date <= now <= self.end_date)
    
    def has_budget_left(self) -> bool:
        """Vérifie s'il reste du budget"""
        if self.total_budget is None:
            return True
        return self.used_budget < self.total_budget
    
    def use_budget(self, amount: Decimal) -> None:
        """Utilise une partie du budget"""
        self.used_budget += amount
    
    def __repr__(self) -> str:
        return f"<Promotion {self.name} type={self.type}>"


class UserPromotion(BaseModel):
    """
    Promotion réclamée par un utilisateur.
    """
    __tablename__ = "user_promotions"
    __table_args__ = (
        Index("idx_user_promotions_user_id", "user_id"),
        Index("idx_user_promotions_promotion_id", "promotion_id"),
        Index("idx_user_promotions_is_completed", "is_completed"),
    )
    
    # ========== Clés étrangères ==========
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    promotion_id = Column(String(36), ForeignKey("promotions.id"), nullable=False)
    
    # ========== Montants ==========
    bonus_amount = Column(Numeric(10, 2), nullable=False)
    wagered_amount = Column(Numeric(12, 2), default=0, nullable=False)
    wagering_required = Column(Integer, nullable=False)
    
    # ========== Statut ==========
    is_completed = Column(Boolean, default=False, nullable=False)
    is_expired = Column(Boolean, default=False, nullable=False)
    
    # ========== Dates ==========
    claimed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    
    # ========== Relations ==========
    user = relationship("User")
    promotion = relationship("Promotion", back_populates="user_promotions")
    
    # ========== Méthodes ==========
    def add_wagered(self, amount: Decimal) -> None:
        """Ajoute du montant misé"""
        self.wagered_amount += amount
        if self.wagered_amount >= self.wagering_required:
            self.complete()
    
    def complete(self) -> None:
        """Marque la promotion comme complétée"""
        self.is_completed = True
        self.completed_at = datetime.utcnow()
    
    def is_expired_check(self) -> bool:
        """Vérifie si la promotion est expirée"""
        from datetime import datetime
        if datetime.utcnow() > self.expires_at:
            self.is_expired = True
        return self.is_expired
    
    def __repr__(self) -> str:
        return f"<UserPromotion user={self.user_id} promotion={self.promotion_id}>"