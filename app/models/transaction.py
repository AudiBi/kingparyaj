# app/models/transaction.py
"""Modèle de transaction financière (CRITIQUE pour conformité)"""

from datetime import datetime

from sqlalchemy import (
    Column, String, Numeric, DateTime, ForeignKey, 
    Enum, Text, CheckConstraint, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import TransactionType, PaymentMethod, TransactionStatus
import secrets


class Transaction(BaseModel):
    """
    Toute transaction financière.
    CRITIQUE pour la conformité LEH et les audits.
    """
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_transaction_amount_positive"),
        CheckConstraint("fee >= 0", name="ck_transaction_fee_positive"),
        Index("idx_transactions_reference", "reference", unique=True),
        Index("idx_transactions_user_id", "user_id"),
        Index("idx_transactions_wallet_id", "wallet_id"),
        Index("idx_transactions_type", "transaction_type"),
        Index("idx_transactions_status", "status"),
        Index("idx_transactions_created_at", "created_at"),
        Index("idx_transactions_external_reference", "external_reference"),
    )
    
    # ========== Clés étrangères ==========
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    wallet_id = Column(String(36), ForeignKey("wallets.id"), nullable=False)
    
    # ========== Référence unique ==========
    reference = Column(String(50), unique=True, nullable=False)
    
    # ========== Type ==========
    transaction_type = Column(Enum(TransactionType), nullable=False)
    payment_method = Column(Enum(PaymentMethod), nullable=True)
    
    # ========== Montants ==========
    amount = Column(Numeric(12, 2), nullable=False)
    fee = Column(Numeric(10, 2), default=0, nullable=False)
    bonus_amount = Column(Numeric(10, 2), default=0, nullable=False)
    balance_before = Column(Numeric(12, 2), nullable=False)
    balance_after = Column(Numeric(12, 2), nullable=False)
    
    # ========== Contexte ==========
    bet_id = Column(String(36), nullable=True)      # Référence au pari (si BET ou WIN)
    draw_id = Column(String(36), nullable=True)     # Référence au tirage
    ticket_id = Column(String(36), nullable=True)   # Référence au ticket
    
    # ========== Statut ==========
    status = Column(Enum(TransactionStatus), default=TransactionStatus.PENDING, nullable=False)
    failure_reason = Column(Text, nullable=True)
    
    # ========== Références externes (Mobile Money) ==========
    external_reference = Column(String(100), nullable=True)
    external_status = Column(String(50), nullable=True)
    
    # ========== Dates ==========
    completed_at = Column(DateTime, nullable=True)
    
    # ========== Métadonnées de sécurité ==========
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    
    # ========== Relations ==========
    user = relationship("User", back_populates="transactions")
    wallet = relationship("Wallet", back_populates="transactions")
    
    # ========== Méthodes statiques ==========
    @staticmethod
    def generate_reference(prefix: str = "TX") -> str:
        """Génère une référence unique pour la transaction"""
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        random = secrets.token_hex(4).upper()
        return f"{prefix}-{timestamp}-{random}"
    
    # ========== Méthodes ==========
    def complete(self) -> None:
        """Marque la transaction comme complétée"""
        self.status = TransactionStatus.COMPLETED
        self.completed_at = datetime.utcnow()
    
    def fail(self, reason: str) -> None:
        """Marque la transaction comme échouée"""
        self.status = TransactionStatus.FAILED
        self.failure_reason = reason
        self.completed_at = datetime.utcnow()
    
    def __repr__(self) -> str:
        return f"<Transaction {self.reference} amount={self.amount} status={self.status}>"