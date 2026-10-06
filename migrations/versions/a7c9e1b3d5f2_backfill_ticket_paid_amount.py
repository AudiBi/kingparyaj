"""Tickets déjà payés : reconstituer le montant réellement payé

Revision ID: a7c9e1b3d5f2
Revises: f1a3c5e7b9d2
Create Date: 2026-10-03

Pourquoi :
Plusieurs chemins de paiement (bouton « Payer » de l'admin, paiement groupé,
anciennes API ticket/agent) ne remplissaient pas tickets.paid_amount. Pour ces
tickets, les rapports retombaient sur initial_amount, c'est-à-dire la MISE
d'un ticket vendu au comptant, au lieu du gain payé (ex. Finances : « Gains
payés sur tickets » 525 HTG au lieu d'environ 1 714 HTG).

Le code passe maintenant par Ticket.record_payout() partout. Cette migration
corrige les tickets DÉJÀ payés (statut PAID, paid_amount vide) en recalculant
ce qui a été versé à partir de leurs paris :

    payé = montant initial
           - mises des paris (hors remboursés / annulés)
           + gains des paris gagnés
           - solde restant (0 pour un ticket payé)

(Keno, Lucky6, Horse Races, et l'historique Lucky Wheel.)

Aucune nouvelle table, aucune autre donnée modifiée. Les tickets qui ont déjà
un paid_amount ne sont pas touchés.

Rollback (downgrade) : ne fait rien (les montants recalculés restent ; ils
sont exacts et l'ancien comportement les ignorait de toute façon).
"""
from alembic import op


revision = "a7c9e1b3d5f2"
down_revision = "f1a3c5e7b9d2"
branch_labels = None
depends_on = None


def upgrade():
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
            - COALESCE(t.balance, 0)
        )
        WHERE upper(t.status::text) = 'PAID' AND t.paid_amount IS NULL
        """
    )


def downgrade():
    pass
