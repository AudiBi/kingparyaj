"""Keno : tirage instantané vérifiable, remboursements, multiplicateurs élevés

Revision ID: b7c2e4f9a1d3
Revises: a1f3c9d2e7b4
Create Date: 2026-10-01

Pourquoi (aucune nouvelle table : keno_draws / keno_bets sont réutilisées) :

1. keno_draws.server_seed / server_seed_hash
   Équité vérifiable, même mécanisme que Horse Races : l'empreinte du seed
   est affichée AVANT le pari, le seed est révélé avec le résultat.
2. keno_draws.mode ('scheduled' | 'instant')
   Distingue les tirages planifiés (ancien fonctionnement, tous les tirages
   existants) des tirages instantanés (un tirage par ticket, au bureau).
3. Valeur 'REFUNDED' dans l'enum kenobetstatus
   Un pari d'un tirage annulé est remboursé et marqué comme tel (avant :
   les mises étaient perdues).
4. keno_bets.multiplier : Numeric(5,2) -> Numeric(10,2)
   Numeric(5,2) plafonne à 999.99 : un gain x1200 (9/9) ou x5000 (10/10)
   faisait échouer le règlement de tout le tirage.
5. Contrainte sur le nombre de numéros joués
   array_length('{}', 1) vaut NULL : la contrainte d'origine laissait passer
   une liste vide. cardinality() renvoie 0 et la refuse.

Données existantes : aucune n'est modifiée (colonnes ajoutées nullable ou
avec une valeur par défaut ; élargissement d'un type numérique). La
migration s'arrête si un pari existant a une liste de numéros vide ou de
plus de 10 numéros.

Rollback (downgrade) : supprime les 3 colonnes, remet Numeric(5,2) (refusé
si un multiplicateur dépasse 999.99) et l'ancienne contrainte. PostgreSQL ne
permet pas de retirer une valeur d'enum : 'REFUNDED' reste déclarée mais le
downgrade refuse de s'exécuter si un pari porte ce statut.
"""
from alembic import op
import sqlalchemy as sa


revision = "b7c2e4f9a1d3"
down_revision = "a1f3c9d2e7b4"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    bad = conn.execute(sa.text(
        "SELECT count(*) FROM keno_bets WHERE cardinality(picks) < 1 OR cardinality(picks) > 10"
    )).scalar()
    if bad:
        raise RuntimeError(f"{bad} pari(s) Keno ont une liste de numéros invalide : à corriger avant la migration")

    # 1-2. Équité vérifiable + mode de tirage
    op.add_column("keno_draws", sa.Column("server_seed", sa.String(64), nullable=True))
    op.add_column("keno_draws", sa.Column("server_seed_hash", sa.String(64), nullable=True))
    op.add_column("keno_draws", sa.Column("mode", sa.String(16), nullable=False, server_default="scheduled"))
    op.create_check_constraint("ck_keno_draws_mode", "keno_draws", "mode IN ('scheduled', 'instant')")

    # 3. Statut « remboursé » (ALTER TYPE … ADD VALUE ne peut pas s'exécuter
    #    dans un bloc de transaction sur les anciennes versions de PostgreSQL)
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE kenobetstatus ADD VALUE IF NOT EXISTS 'REFUNDED'")

    # 4. Multiplicateurs jusqu'à 99 999 999.99
    op.alter_column("keno_bets", "multiplier", type_=sa.Numeric(10, 2), existing_type=sa.Numeric(5, 2), existing_nullable=False)

    # 5. 1 à 10 numéros, liste vide refusée
    op.drop_constraint("ck_keno_bet_picks_min", "keno_bets", type_="check")
    op.drop_constraint("ck_keno_bet_picks_max", "keno_bets", type_="check")
    op.create_check_constraint("ck_keno_bet_picks_min", "keno_bets", "cardinality(picks) >= 1")
    op.create_check_constraint("ck_keno_bet_picks_max", "keno_bets", "cardinality(picks) <= 10")


def downgrade():
    conn = op.get_bind()
    refunded = conn.execute(sa.text("SELECT count(*) FROM keno_bets WHERE status::text = 'REFUNDED'")).scalar()
    if refunded:
        raise RuntimeError(f"{refunded} pari(s) Keno remboursé(s) : retour arrière impossible sans perdre ce statut")
    too_big = conn.execute(sa.text("SELECT count(*) FROM keno_bets WHERE multiplier > 999.99")).scalar()
    if too_big:
        raise RuntimeError(f"{too_big} pari(s) Keno ont un multiplicateur > 999.99 : retour arrière impossible")

    op.drop_constraint("ck_keno_bet_picks_min", "keno_bets", type_="check")
    op.drop_constraint("ck_keno_bet_picks_max", "keno_bets", type_="check")
    op.create_check_constraint("ck_keno_bet_picks_min", "keno_bets", "array_length(picks, 1) >= 1")
    op.create_check_constraint("ck_keno_bet_picks_max", "keno_bets", "array_length(picks, 1) <= 10")
    op.alter_column("keno_bets", "multiplier", type_=sa.Numeric(5, 2), existing_type=sa.Numeric(10, 2), existing_nullable=False)

    op.drop_constraint("ck_keno_draws_mode", "keno_draws", type_="check")
    op.drop_column("keno_draws", "mode")
    op.drop_column("keno_draws", "server_seed_hash")
    op.drop_column("keno_draws", "server_seed")
