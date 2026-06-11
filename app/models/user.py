# app/models/user.py
"""Modèle utilisateur avec gestion des rôles, KYC et parrainage"""

from sqlalchemy import (
    Column, String, Boolean, DateTime, Numeric, Integer, 
    ForeignKey, Enum, Index, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.ext.hybrid import hybrid_property
from app.models.base import BaseModel
from app.models.enums import UserRole, KYCStatus


class User(BaseModel):
    """
    Utilisateur du système.
    Peut être joueur, agent, manager ou administrateur.
    """
    __tablename__ = "users"
    __table_args__ = (
        Index("idx_users_phone", "phone"),
        Index("idx_users_email", "email"),
        Index("idx_users_national_id", "national_id"),
        Index("idx_users_referral_code", "referral_code"),
        Index("idx_users_bureau_id", "bureau_id"),
        Index("idx_users_role", "role"),
        Index("idx_users_created_at", "created_at"),
    )
    
    # ========== Informations personnelles ==========
    email = Column(String(120), unique=True, nullable=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    first_name = Column(String(50), nullable=True)
    last_name = Column(String(50), nullable=True)
    national_id = Column(String(20), unique=True, nullable=True)
    
    # ========== Sécurité ==========
    password_hash = Column(String(200), nullable=False)
    two_factor_secret = Column(String(32), nullable=True)
    two_factor_enabled = Column(Boolean, default=False, nullable=False)
    refresh_token = Column(String(500), nullable=True)
    
    # ========== Statut ==========
    is_active = Column(Boolean, default=True, nullable=False)
    is_locked = Column(Boolean, default=False, nullable=False)
    locked_at = Column(DateTime, nullable=True)
    lock_reason = Column(String(200), nullable=True)
    
    # ========== KYC (Know Your Customer) ==========
    kyc_status = Column(Enum(KYCStatus), default=KYCStatus.PENDING, nullable=False)
    kyc_verified_at = Column(DateTime, nullable=True)
    kyc_verified_by = Column(String(36), nullable=True)
    kyc_documents = Column(Text, nullable=True)  # URLs des documents JSON
    
    # ========== Rôle et affectation ==========
    role = Column(Enum(UserRole), default=UserRole.PLAYER, nullable=False)
    bureau_id = Column(String(36), ForeignKey("bureaus.id"), nullable=True)
    
    # ========== Métriques ==========
    total_bets_count = Column(Integer, default=0, nullable=False)
    total_bets_amount = Column(Numeric(12, 2), default=0, nullable=False)
    total_wins = Column(Numeric(12, 2), default=0, nullable=False)
    last_login = Column(DateTime, nullable=True)
    last_ip = Column(String(45), nullable=True)
    
    # ========== Parrainage (référence) ==========
    referrer_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    referral_code = Column(String(20), unique=True, nullable=True)
    
    # ========== Relations ==========
    # Wallet (One-to-One)
    wallet = relationship("Wallet", back_populates="user", uselist=False, cascade="all, delete-orphan")
    
    # Paris
    keno_bets = relationship("KenoBet", back_populates="user", foreign_keys="KenoBet.user_id")
    lucky_plays = relationship("LuckyPlay", back_populates="user")
    
    # Tickets créés (en tant qu'agent)
    tickets_created = relationship("Ticket", back_populates="agent", foreign_keys="Ticket.agent_id")
    
    # Transactions
    transactions = relationship("Transaction", back_populates="user")
    
    # Notifications
    notifications = relationship("Notification", back_populates="user")
    
    # Exclusion
    self_exclusions = relationship("SelfExclusion", back_populates="user")
    
    # Audit
    audit_logs = relationship("AuditLog", back_populates="user")
    
    # Parrainage
    referrals = relationship("User", backref="referrer", remote_side=[id])
    
    # ========== Propriétés hybrides ==========
    @hybrid_property
    def full_name(self) -> str:
        """Nom complet de l'utilisateur"""
        if self.first_name or self.last_name:
            return f"{self.first_name or ''} {self.last_name or ''}".strip()
        return self.phone
    
    @hybrid_property
    def is_agent(self) -> bool:
        """Vérifie si l'utilisateur est un agent"""
        return self.role in [UserRole.AGENT, UserRole.MANAGER]
    
    @hybrid_property
    def is_admin(self) -> bool:
        """Vérifie si l'utilisateur est un administrateur"""
        return self.role in [UserRole.ADMIN, UserRole.SUPER_ADMIN]
    
    # ========== Méthodes ==========
    def __repr__(self) -> str:
        return f"<User {self.phone}>"