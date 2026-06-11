# app/models/wallet.py
"""Modèle de portefeuille et transactions financières"""

from sqlalchemy import (
    Column, String, Numeric, Boolean, DateTime, ForeignKey, 
    Enum, Integer, CheckConstraint, Index
)
from sqlalchemy.orm import relationship
from sqlalchemy.ext.hybrid import hybrid_property
from decimal import Decimal
from app.models.base import BaseModel
from app.models.enums import WalletStatus


class Wallet(BaseModel):
    """
    Portefeuille d'un utilisateur.
    Gère le solde, les bonus et les limites de jeu.
    """
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_wallet_balance_positive"),
        CheckConstraint("bonus_balance >= 0", name="ck_wallet_bonus_positive"),
        Index("idx_wallets_user_id", "user_id"),
        Index("idx_wallets_status", "status"),
    )
    
    # ========== Clés étrangères ==========
    user_id = Column(String(36), ForeignKey("users.id"), unique=True, nullable=False)
    
    # ========== Soldes ==========
    balance = Column(Numeric(12, 2), default=0, nullable=False)
    bonus_balance = Column(Numeric(12, 2), default=0, nullable=False)
    pending_withdrawals = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Statistiques ==========
    total_deposited = Column(Numeric(12, 2), default=0, nullable=False)
    total_withdrawn = Column(Numeric(12, 2), default=0, nullable=False)
    total_won = Column(Numeric(12, 2), default=0, nullable=False)
    total_bonus_received = Column(Numeric(12, 2), default=0, nullable=False)
    total_bonus_wagered = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Limites personnalisées ==========
    daily_deposit_limit = Column(Numeric(10, 2), nullable=True)
    daily_loss_limit = Column(Numeric(10, 2), nullable=True)
    weekly_deposit_limit = Column(Numeric(10, 2), nullable=True)
    monthly_deposit_limit = Column(Numeric(10, 2), nullable=True)
    single_bet_limit = Column(Numeric(10, 2), nullable=True)
    
    # ========== Compteurs journaliers ==========
    today_deposits = Column(Numeric(12, 2), default=0, nullable=False)
    today_losses = Column(Numeric(12, 2), default=0, nullable=False)
    today_bets = Column(Numeric(12, 2), default=0, nullable=False)
    last_reset_date = Column(DateTime, nullable=True)
    
    # ========== Statut ==========
    status = Column(Enum(WalletStatus), default=WalletStatus.ACTIVE, nullable=False)
    frozen_reason = Column(String(200), nullable=True)
    
    # ========== Relations ==========
    user = relationship("User", back_populates="wallet")
    transactions = relationship("Transaction", back_populates="wallet", cascade="all, delete-orphan")
    
    # ========== Hybrid properties ==========
    @hybrid_property
    def total_balance(self) -> Decimal:
        """Solde total (réel + bonus)"""
        return self.balance + self.bonus_balance
    
    @hybrid_property
    def withdrawable_balance(self) -> Decimal:
        """Solde retirable (hors bonus)"""
        return self.balance
    
    # ========== Méthodes ==========
    def can_bet(self, amount: Decimal) -> bool:
        """Vérifie si le joueur peut miser ce montant"""
        if self.status != WalletStatus.ACTIVE:
            return False
        if self.balance < amount:
            return False
        if self.single_bet_limit and amount > self.single_bet_limit:
            return False
        if self.daily_loss_limit and self.today_losses + amount > self.daily_loss_limit:
            return False
        return True
    
    def deduct(self, amount: Decimal, is_bonus: bool = False) -> bool:
        """Déduit un montant du portefeuille"""
        if is_bonus:
            if self.bonus_balance >= amount:
                self.bonus_balance -= amount
                return True
            return False
        else:
            if self.balance >= amount:
                self.balance -= amount
                return True
            return False
    
    def add(self, amount: Decimal, is_bonus: bool = False) -> None:
        """Ajoute un montant au portefeuille"""
        if is_bonus:
            self.bonus_balance += amount
            self.total_bonus_received += amount
        else:
            self.balance += amount
    
    def __repr__(self) -> str:
        return f"<Wallet user_id={self.user_id} balance={self.balance}>"