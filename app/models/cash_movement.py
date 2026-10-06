# app/models/cash_movement.py
"""Mouvements d'espèces sur un ticket, datés un par un.

Pourquoi une table (et pas une colonne) : un ticket peut être payé en
plusieurs fois, à des dates différentes, rechargé, ou remboursé à
l'annulation. Les rapports (Finances, caisse, rapports agent) doivent dater
CHAQUE sortie / entrée d'argent ; une ligne de `tickets` ne peut contenir
qu'un montant cumulé (paid_amount) et une seule date (paid_at).

Écrit uniquement par les méthodes de Ticket (record_payout,
record_cancellation, record_recharge) : un seul point d'écriture.
"""

from sqlalchemy import Column, ForeignKey, Index, Numeric, String

from app.models.base import BaseModel

KIND_PAYOUT = "payout"              # gain / solde payé au joueur (total ou partiel)
KIND_CANCEL_REFUND = "cancel_refund"  # solde rendu au joueur à l'annulation du ticket
KIND_RECHARGE = "recharge"          # argent ajouté sur un ticket existant
MONEY_OUT_KINDS = (KIND_PAYOUT, KIND_CANCEL_REFUND)


class TicketCashMovement(BaseModel):
    __tablename__ = "ticket_cash_movements"
    __table_args__ = (
        Index("idx_ticket_cash_movements_ticket_id", "ticket_id"),
        Index("idx_ticket_cash_movements_created_at", "created_at"),
        Index("idx_ticket_cash_movements_bureau_id", "bureau_id"),
        Index("idx_ticket_cash_movements_agent_id", "agent_id"),
    )

    ticket_id = Column(String(36), ForeignKey("tickets.id"), nullable=False)
    bureau_id = Column(String(36), nullable=True)
    agent_id = Column(String(36), nullable=True)      # agent (ou admin) qui a remis / reçu l'argent
    session_id = Column(String(36), nullable=True)    # session de caisse, si l'argent est passé par une caisse
    kind = Column(String(20), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)   # toujours positif ; le sens dépend de kind

    def __repr__(self) -> str:
        return f"<TicketCashMovement {self.kind} {self.amount}>"
