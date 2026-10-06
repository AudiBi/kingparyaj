"""Paiement des gains : montant réellement payé sur chaque ticket

Revision ID: e8f2a4c6b1d3
Revises: d5e9f3b7a2c1
Create Date: 2026-10-03

Pourquoi (aucune nouvelle table) :
Les tickets sont maintenant créés automatiquement à chaque pari au comptant
(montant = mise). Quand l'agent paie les gains, la somme versée au joueur
n'est plus égale au montant du ticket. Les rapports de caisse (« payé »)
utilisaient tickets.initial_amount : ils afficheraient la mise au lieu du
gain réellement payé. La colonne tickets.paid_amount enregistre ce qui a
été versé (cumul si paiement en plusieurs fois).

Données existantes : aucune n'est modifiée (colonne vide pour les anciens
tickets, les rapports retombent alors sur l'ancien calcul).

Rollback (downgrade) : supprime la colonne.
"""
from alembic import op
import sqlalchemy as sa


revision = "e8f2a4c6b1d3"
down_revision = "d5e9f3b7a2c1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tickets", sa.Column("paid_amount", sa.Numeric(12, 2), nullable=True))


def downgrade():
    op.drop_column("tickets", "paid_amount")
