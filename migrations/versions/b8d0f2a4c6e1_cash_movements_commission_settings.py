"""Mouvements d'espèces datés, commission figée, réglages durables

Revision ID: b8d0f2a4c6e1
Revises: a7c9e1b3d5f2
Create Date: 2026-10-03

Pourquoi (revue des flux d'argent) :

1. ticket_cash_movements (NOUVELLE TABLE — justification) : un ticket peut être
   payé en plusieurs fois, à des dates différentes, rechargé ou remboursé à
   l'annulation. Une ligne de `tickets` n'a qu'un montant cumulé et une date
   (celle du paiement final) : un paiement partiel n'apparaissait dans aucun
   rapport (Finances, rapports agent) alors que l'argent était sorti du tiroir.
   Chaque mouvement est maintenant une ligne datée.
   Reprise de l'existant :
   - tickets payés : un paiement à la date de paiement (montant payé) ;
   - tickets encore actifs déjà payés en partie : un paiement à leur date de
     dernière modification (la date exacte des partiels n'était pas gardée) ;
   - tickets annulés : le solde rendu au joueur, recalculé depuis leurs paris
     (montant initial - mises + gains), à leur date de dernière modification.

2. keno_bets / game_bets : commission (figée à la vente) et counts_as_sale
   (faux pour un gain rejoué sur le même ticket). Les paris existants gardent
   commission NULL (calculée au taux actuel, comme avant) et comptent comme
   ventes.

3. system_settings (NOUVELLE TABLE — justification) : les réglages généraux
   vivent dans Redis (24 h) ; le taux de commission par défaut doit survivre à
   un redémarrage / vidage de Redis. La valeur actuelle (si elle a été fixée
   dans Redis) est reprise automatiquement à la première lecture.

Rollback (downgrade) : supprime les deux tables et les deux colonnes
(les montants payés des tickets ne sont pas touchés).
"""
from alembic import op
import sqlalchemy as sa


revision = "b8d0f2a4c6e1"
down_revision = "a7c9e1b3d5f2"
branch_labels = None
depends_on = None

NEW_ID = "md5(random()::text || clock_timestamp()::text || t.id)::uuid::text"


def upgrade():
    op.create_table(
        "ticket_cash_movements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticket_id", sa.String(36), sa.ForeignKey("tickets.id"), nullable=False),
        sa.Column("bureau_id", sa.String(36), nullable=True),
        sa.Column("agent_id", sa.String(36), nullable=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("updated_by", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("idx_ticket_cash_movements_ticket_id", "ticket_cash_movements", ["ticket_id"])
    op.create_index("idx_ticket_cash_movements_created_at", "ticket_cash_movements", ["created_at"])
    op.create_index("idx_ticket_cash_movements_bureau_id", "ticket_cash_movements", ["bureau_id"])
    op.create_index("idx_ticket_cash_movements_agent_id", "ticket_cash_movements", ["agent_id"])

    # --- reprise : tickets annulés -> solde rendu (recalculé depuis les paris)
    op.execute(
        """
        UPDATE tickets t
        SET paid_amount = GREATEST(0,
              t.initial_amount
            - COALESCE((SELECT SUM(k.stake) FROM keno_bets k
                        WHERE k.ticket_id = t.id AND upper(k.status::text) <> 'REFUNDED'), 0)
            + COALESCE((SELECT SUM(k.winnings) FROM keno_bets k
                        WHERE k.ticket_id = t.id AND upper(k.status::text) = 'WON'), 0)
            - COALESCE((SELECT SUM(g.stake) FROM game_bets g
                        WHERE g.ticket_id = t.id AND upper(g.status) NOT IN ('REFUNDED', 'VOID')), 0)
            + COALESCE((SELECT SUM(g.winnings) FROM game_bets g
                        WHERE g.ticket_id = t.id AND upper(g.status) = 'WON'), 0)
            - COALESCE((SELECT SUM(l.stake - l.winnings) FROM lucky_plays l
                        WHERE l.ticket_id = t.id AND upper(l.status) = 'COMPLETED'), 0)
            - COALESCE(t.balance, 0))
        WHERE upper(t.status::text) = 'CANCELLED' AND t.paid_amount IS NULL
        """
    )
    op.execute(
        f"""
        INSERT INTO ticket_cash_movements (id, ticket_id, bureau_id, agent_id, kind, amount, created_at, updated_at, is_deleted)
        SELECT {NEW_ID}, t.id, t.bureau_id, t.paid_by_agent, 'cancel_refund', t.paid_amount,
               t.updated_at, t.updated_at, false
        FROM tickets t
        WHERE upper(t.status::text) = 'CANCELLED' AND t.paid_amount > 0
        """
    )
    # --- reprise : tickets payés (totalement) -> un paiement à la date de paiement
    op.execute(
        f"""
        INSERT INTO ticket_cash_movements (id, ticket_id, bureau_id, agent_id, kind, amount, created_at, updated_at, is_deleted)
        SELECT {NEW_ID}, t.id, t.bureau_id, t.paid_by_agent, 'payout', t.paid_amount,
               COALESCE(t.paid_at, t.updated_at), COALESCE(t.paid_at, t.updated_at), false
        FROM tickets t
        WHERE upper(t.status::text) = 'PAID' AND t.paid_amount > 0
        """
    )
    # --- reprise : tickets actifs déjà payés en partie
    op.execute(
        f"""
        INSERT INTO ticket_cash_movements (id, ticket_id, bureau_id, agent_id, kind, amount, created_at, updated_at, is_deleted)
        SELECT {NEW_ID}, t.id, t.bureau_id, NULL, 'payout', t.paid_amount, t.updated_at, t.updated_at, false
        FROM tickets t
        WHERE upper(t.status::text) IN ('ACTIVE', 'EXPIRED') AND t.paid_amount > 0
        """
    )

    # --- commission figée sur les paris
    for table in ("keno_bets", "game_bets"):
        op.add_column(table, sa.Column("commission", sa.Numeric(10, 2), nullable=True))
        op.add_column(table, sa.Column("counts_as_sale", sa.Boolean(), nullable=False, server_default=sa.true()))

    # --- réglages durables
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(36), nullable=True),
    )


def downgrade():
    op.drop_table("system_settings")
    for table in ("game_bets", "keno_bets"):
        op.drop_column(table, "counts_as_sale")
        op.drop_column(table, "commission")
    op.drop_index("idx_ticket_cash_movements_agent_id", table_name="ticket_cash_movements")
    op.drop_index("idx_ticket_cash_movements_bureau_id", table_name="ticket_cash_movements")
    op.drop_index("idx_ticket_cash_movements_created_at", table_name="ticket_cash_movements")
    op.drop_index("idx_ticket_cash_movements_ticket_id", table_name="ticket_cash_movements")
    op.drop_table("ticket_cash_movements")
