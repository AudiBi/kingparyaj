"""Keno partagé : réglages figés dans chaque tirage

Revision ID: d5e9f3b7a2c1
Revises: c4d8e1a6f2b9
Create Date: 2026-10-02

Pourquoi (aucune nouvelle table : keno_draws est réutilisée) :
Le Keno devient partagé (un tirage commun toutes les N minutes). Les paris
d'un tirage sont pris pendant plusieurs minutes, alors que l'admin peut
modifier la table de paiement « à tout moment ». Sans copie des réglages
dans le tirage, un ticket accepté avec une table serait payé avec une
autre. La colonne keno_draws.config garde, pour chaque tirage partagé,
la table de paiement, les limites de mise, le gain maximum et le rythme
d'affichage en vigueur à sa création.

Données existantes : aucune n'est modifiée (colonne ajoutée, vide pour les
anciens tirages, qui continuent d'utiliser la configuration courante).

Rollback (downgrade) : supprime la colonne (les tirages en attente
reprendraient alors la configuration courante).
"""
from alembic import op
import sqlalchemy as sa


revision = "d5e9f3b7a2c1"
down_revision = "c4d8e1a6f2b9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("keno_draws", sa.Column("config", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("keno_draws", "config")
