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
    # Montant réellement payé en espèces au joueur (gains), cumul des paiements.
    # NULL pour les tickets payés avant la migration : on retombe alors sur
    # initial_amount (ancien calcul). Voir amount_paid.
    paid_amount = Column(Numeric(12, 2), nullable=True)
    
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

    # ========== Validité : le jour même ==========
    @staticmethod
    def end_of_local_day():
        """Minuit (heure d'Haïti) ce soir, en UTC : un ticket se paie le jour même."""
        from app.core.timezone import local_date_end_utc, today_haiti

        return local_date_end_utc(today_haiti())

    def keep_payable_today(self) -> None:
        """Appelé quand un résultat crédite le ticket (gain ou remboursement) :
        le joueur peut encaisser jusqu'à minuit le jour où le résultat tombe,
        même si le pari a été pris la veille juste avant minuit."""
        if self.status == TicketStatus.EXPIRED:
            self.status = TicketStatus.ACTIVE  # expiré pendant que son pari attendait le tirage
        if self.status == TicketStatus.ACTIVE:
            end = Ticket.end_of_local_day()
            if self.expires_at is None or self.expires_at < end:
                self.expires_at = end

    @staticmethod
    def no_pending_bet_clause():
        """Condition SQL : aucun pari de ce ticket n'attend son tirage
        (un tel ticket ne doit pas expirer, son gain est peut-être à venir)."""
        from sqlalchemy import and_, exists, select

        from app.models.enums import KenoBetStatus
        from app.models.game import GameBet
        from app.models.keno import KenoBet

        return and_(
            ~exists(select(KenoBet.id).where(KenoBet.ticket_id == Ticket.id, KenoBet.status == KenoBetStatus.PENDING)),
            ~exists(select(GameBet.id).where(GameBet.ticket_id == Ticket.id, GameBet.status == "PENDING")),
        )

    # ========== Mouvements d'espèces (SEUL point d'écriture) ==========
    def _movement(self, kind: str, amount: Decimal, agent_id, session_id=None):
        """Trace datée du mouvement (table ticket_cash_movements)."""
        from sqlalchemy.orm import object_session

        from app.models.cash_movement import TicketCashMovement

        movement = TicketCashMovement(ticket_id=self.id, bureau_id=self.bureau_id, agent_id=agent_id,
                                      session_id=session_id, kind=kind, amount=amount)
        session = object_session(self)
        if session is not None:
            session.add(movement)
        return movement

    def record_payout(self, amount, agent_id, session_id=None) -> Decimal:
        """Paiement en espèces au joueur (total ou partiel), daté.
        Cumule paid_amount ; le ticket passe à PAYÉ quand son solde arrive à 0.
        Tous les chemins de paiement (agent, admin, API) passent ici."""
        from datetime import datetime

        from app.models.cash_movement import KIND_PAYOUT

        amount = Decimal(str(amount))
        if amount <= 0 or amount > Decimal(str(self.balance or 0)):
            raise ValueError("Montant de paiement invalide")
        self.balance = Decimal(str(self.balance or 0)) - amount
        self.paid_amount = Decimal(str(self.paid_amount or 0)) + amount
        if self.balance == 0:
            self.status = TicketStatus.PAID
            self.paid_at = datetime.utcnow()
            self.paid_by_agent = agent_id
        self._movement(KIND_PAYOUT, amount, agent_id, session_id)
        return amount

    def record_cancellation(self, agent_id, session_id=None) -> Decimal:
        """Annulation : le solde restant est rendu au joueur (sortie d'espèces datée)."""
        from app.models.cash_movement import KIND_CANCEL_REFUND

        amount = Decimal(str(self.balance or 0))
        self.status = TicketStatus.CANCELLED
        self.balance = Decimal("0")
        if amount > 0:
            self.paid_amount = Decimal(str(self.paid_amount or 0)) + amount
            self._movement(KIND_CANCEL_REFUND, amount, agent_id, session_id)
        return amount

    def record_recharge(self, amount, agent_id, session_id=None) -> Decimal:
        """Argent ajouté sur un ticket existant (entrée d'espèces datée)."""
        from app.models.cash_movement import KIND_RECHARGE

        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("Montant de recharge invalide")
        self.balance = Decimal(str(self.balance or 0)) + amount
        self._movement(KIND_RECHARGE, amount, agent_id, session_id)
        return amount

    @property
    def amount_paid(self):
        """Montant payé au joueur (ancien ticket sans paid_amount : initial_amount)."""
        if self.paid_amount is not None:
            return self.paid_amount
        return self.initial_amount if self.paid_at else 0
