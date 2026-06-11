# app/models/bureau.py
"""Modèles pour les points de vente physiques (bureaux) et caisses"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, DateTime, JSON, 
    ForeignKey, CheckConstraint, Index, Time
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel


class Bureau(BaseModel):
    """
    Point de vente physique (bureau) où les joueurs peuvent:
    - Déposer de l'argent cash
    - Retirer des gains
    - Jouer avec des tickets
    """
    __tablename__ = "bureaus"
    __table_args__ = (
        Index("idx_bureaus_code", "code", unique=True),
        Index("idx_bureaus_city", "city"),
        Index("idx_bureaus_manager_id", "manager_id"),
    )
    
    # ========== Identification ==========
    name = Column(String(100), nullable=False)
    code = Column(String(20), unique=True, nullable=False)
    
    # ========== Adresse ==========
    address = Column(String(200), nullable=True)
    city = Column(String(50), nullable=True)
    commune = Column(String(50), nullable=True)
    department = Column(String(50), nullable=True)
    latitude = Column(String(20), nullable=True)
    longitude = Column(String(20), nullable=True)
    
    # ========== Contact ==========
    phone = Column(String(20), nullable=True)
    email = Column(String(120), nullable=True)
    
    # ========== Gestion ==========
    manager_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    
    # ========== Caisse ==========
    cash_balance = Column(Numeric(12, 2), default=0, nullable=False)
    safe_balance = Column(Numeric(12, 2), default=0, nullable=False)
    cash_in_transit = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Horaires d'ouverture ==========
    opening_hours = Column(JSON, default={
        "monday": {"open": "08:00", "close": "20:00"},
        "tuesday": {"open": "08:00", "close": "20:00"},
        "wednesday": {"open": "08:00", "close": "20:00"},
        "thursday": {"open": "08:00", "close": "20:00"},
        "friday": {"open": "08:00", "close": "20:00"},
        "saturday": {"open": "08:00", "close": "18:00"},
        "sunday": {"open": "09:00", "close": "14:00"}
    })
    
    # ========== Statut ==========
    is_active = Column(Boolean, default=True, nullable=False)
    
    # ========== Métriques journalières ==========
    total_cash_in_today = Column(Numeric(12, 2), default=0, nullable=False)
    total_cash_out_today = Column(Numeric(12, 2), default=0, nullable=False)
    total_bets_today = Column(Integer, default=0, nullable=False)
    last_cash_count = Column(DateTime, nullable=True)
    
    # ========== Relations ==========
    agents = relationship("User", backref="bureau")
    tickets = relationship("Ticket", back_populates="bureau")
    cashier_sessions = relationship("CashierSession", back_populates="bureau")
    
    # ========== Méthodes ==========
    def __repr__(self) -> str:
        return f"<Bureau {self.code} - {self.name}>"


class CashierSession(BaseModel):
    """
    Session de caisse pour chaque agent de bureau.
    Permet de tracer toutes les opérations cash.
    """
    __tablename__ = "cashier_sessions"
    __table_args__ = (
        CheckConstraint("starting_balance >= 0", name="ck_session_starting_positive"),
        CheckConstraint("current_balance >= 0", name="ck_session_current_positive"),
        Index("idx_cashier_sessions_bureau_id", "bureau_id"),
        Index("idx_cashier_sessions_agent_id", "agent_id"),
        Index("idx_cashier_sessions_status", "status"),
        Index("idx_cashier_sessions_opened_at", "opened_at"),
    )
    
    # ========== Clés étrangères ==========
    bureau_id = Column(String(36), ForeignKey("bureaus.id"), nullable=False)
    agent_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    
    # ========== Montants ==========
    starting_balance = Column(Numeric(12, 2), nullable=False)
    current_balance = Column(Numeric(12, 2), nullable=False)
    expected_balance = Column(Numeric(12, 2), nullable=False)
    
    # ========== Compteurs ==========
    cash_in_count = Column(Integer, default=0, nullable=False)
    cash_in_amount = Column(Numeric(12, 2), default=0, nullable=False)
    cash_out_count = Column(Integer, default=0, nullable=False)
    cash_out_amount = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Statut ==========
    status = Column(String(20), default="OPEN", nullable=False)  # OPEN, CLOSED, SUSPENDED
    
    # ========== Dates ==========
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    
    # ========== Écart caisse ==========
    difference = Column(Numeric(12, 2), default=0, nullable=False)
    difference_reason = Column(String(200), nullable=True)
    
    # ========== Relations ==========
    bureau = relationship("Bureau", back_populates="cashier_sessions")
    agent = relationship("User", foreign_keys=[agent_id])
    
    # ========== Méthodes ==========
    def calculate_expected_balance(self) -> Decimal:
        """Calcule le solde attendu"""
        return self.starting_balance + self.cash_in_amount - self.cash_out_amount
    
    def close(self, actual_balance: Decimal, reason: str = None) -> None:
        """Ferme la session de caisse"""
        self.expected_balance = self.calculate_expected_balance()
        self.difference = actual_balance - self.expected_balance
        self.difference_reason = reason
        self.current_balance = actual_balance
        self.status = "CLOSED"
        self.closed_at = datetime.utcnow()
    
    def __repr__(self) -> str:
        return f"<CashierSession agent={self.agent_id} status={self.status}>"