"""Lucky6 : nouveaux types de pari dans game_bets

Revision ID: c4d8e1a6f2b9
Revises: b7c2e4f9a1d3
Create Date: 2026-10-02

Pourquoi (aucune nouvelle table) :
Lucky6 réutilise les tables génériques game_rounds / game_bets (comme
Horse Races, game_type = 'lucky6'). Seule la contrainte qui liste les
types de pari autorisés doit accepter les 3 types Lucky6 :
  SIX         6 numéros, gain selon la position du 6e numéro trouvé
  FIRST_ODD   1re boule impaire
  FIRST_EVEN  1re boule paire

Données existantes : aucune n'est modifiée (la nouvelle contrainte est
plus large que l'ancienne).

Rollback (downgrade) : remet l'ancienne contrainte ; refusé tant qu'un
pari Lucky6 existe (il faudrait sinon supprimer des paris réels).
"""
from alembic import op
import sqlalchemy as sa


revision = "c4d8e1a6f2b9"
down_revision = "b7c2e4f9a1d3"
branch_labels = None
depends_on = None

OLD_TYPES = ("WIN", "PLACE", "EXACTA", "TRIFECTA")
NEW_TYPES = OLD_TYPES + ("SIX", "FIRST_ODD", "FIRST_EVEN")


def _check(types):
    return "bet_type IN (" + ", ".join(f"'{t}'" for t in types) + ")"


def upgrade():
    op.drop_constraint("ck_game_bets_bet_type", "game_bets", type_="check")
    op.create_check_constraint("ck_game_bets_bet_type", "game_bets", _check(NEW_TYPES))


def downgrade():
    bind = op.get_bind()
    count = bind.execute(sa.text(
        "SELECT count(*) FROM game_bets WHERE bet_type IN ('SIX', 'FIRST_ODD', 'FIRST_EVEN')"
    )).scalar()
    if count:
        raise RuntimeError(
            f"Retour arrière impossible : {count} pari(s) Lucky6 existent dans game_bets."
        )
    op.drop_constraint("ck_game_bets_bet_type", "game_bets", type_="check")
    op.create_check_constraint("ck_game_bets_bet_type", "game_bets", _check(OLD_TYPES))
