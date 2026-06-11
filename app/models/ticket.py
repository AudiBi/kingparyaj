# app/models/ticket.py
"""Modèle de ticket pour les joueurs sans compte (jeu cash en bureau)"""

from decimal import Decimal

from sqlalchemy import (
    Column, String, Numeric, DateTime, ForeignKey, 
    Enum, Boolean, CheckConstraint, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import TicketStatus
import secrets
import string


class Ticket(BaseModel):
    """
    Ticket pour les joueurs sans compte.
    Permet de jouer au bureau avec de l'argent cash.
    """
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("balance >= 0", name="ck_ticket_balance_positive"),
        Index("idx_tickets_ticket_number", "ticket_number", unique=True),
        Index("idx_tickets_bureau_id", "bureau_id"),
        Index("idx_tickets_agent_id", "agent_id"),
        Index("idx_tickets_status", "status"),
        Index("idx_tickets_expires_at", "expires_at"),
    )
    
    # ========== Clés étrangères ==========
    bureau_id = Column(String(36), ForeignKey("bureaus.id"), nullable=False)
    agent_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    
    # ========== Identification ==========
    ticket_number = Column(String(20), unique=True, nullable=False, index=True)
    
    # ========== Informations joueur ==========
    player_name = Column(String(100), nullable=True)
    player_phone = Column(String(20), nullable=True)  # Pour notifications SMS
    
    # ========== Montants ==========
    balance = Column(Numeric(12, 2), default=0, nullable=False)
    initial_amount = Column(Numeric(12, 2), nullable=False)
    
    # ========== Statut ==========
    status = Column(Enum(TicketStatus), default=TicketStatus.ACTIVE, nullable=False)
    
    # ========== Dates ==========
    expires_at = Column(DateTime, nullable=False)
    paid_at = Column(DateTime, nullable=True)
    paid_by_agent = Column(String(36), nullable=True)
    
    # ========== Relations ==========
    bureau = relationship("Bureau", back_populates="tickets")
    agent = relationship("User", back_populates="tickets_created", foreign_keys=[agent_id])
    keno_bets = relationship("KenoBet", back_populates="ticket")
    lucky_plays = relationship("LuckyPlay", back_populates="ticket")
    
    # ========== Méthodes statiques ==========
    @staticmethod
    def generate_ticket_number() -> str:
        """
        Génère un numéro de ticket unique.
        Format: KNO-ABCD-1234
        """
        prefix = "KNO"
        random_part = ''.join(
            secrets.choice(string.ascii_uppercase + string.digits) 
            for _ in range(8)
        )
        return f"{prefix}-{random_part[:4]}-{random_part[4:]}"
    
    # ========== Méthodes ==========
    def is_expired(self) -> bool:
        """Vérifie si le ticket est expiré"""
        from datetime import datetime
        return datetime.utcnow() > self.expires_at
    
    def can_bet(self, amount: Decimal) -> bool:
        """Vérifie si on peut parier avec ce ticket"""
        return (self.status == TicketStatus.ACTIVE and 
                self.balance >= amount and 
                not self.is_expired())
    
    def __repr__(self) -> str:
        return f"<Ticket {self.ticket_number} balance={self.balance}>"