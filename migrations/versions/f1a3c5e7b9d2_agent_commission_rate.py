"""Commission des agents : taux propre à un agent

Revision ID: f1a3c5e7b9d2
Revises: e8f2a4c6b1d3
Create Date: 2026-10-03

Pourquoi (aucune nouvelle table) :
La commission affichée dans le tableau de bord de l'agent valait toujours 0.
Elle est maintenant calculée : % des ventes (mises encaissées par l'agent).
L'admin fixe un taux par défaut ; users.commission_rate permet de donner un
taux différent à un agent précis (NULL = taux par défaut).

Données existantes : aucune n'est modifiée (colonne vide = taux par défaut).

Rollback (downgrade) : supprime la colonne.
"""
from alembic import op
import sqlalchemy as sa


revision = "f1a3c5e7b9d2"
down_revision = "e8f2a4c6b1d3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("commission_rate", sa.Numeric(5, 2), nullable=True))


def downgrade():
    op.drop_column("users", "commission_rate")
