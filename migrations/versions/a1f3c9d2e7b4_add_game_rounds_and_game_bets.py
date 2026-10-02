"""Ajout des tables génériques game_rounds et game_bets (Horse Races)

Revision ID: a1f3c9d2e7b4
Revises: 331737ed4e0f
Create Date: 2026-10-01

Additive uniquement : aucune table existante n'est modifiée.
Rollback : downgrade() supprime les deux tables (aucune donnée existante touchée).
"""
from alembic import op
import sqlalchemy as sa


revision = "a1f3c9d2e7b4"
down_revision = "331737ed4e0f"
branch_labels = None
depends_on = None


ROUND_STATUSES = "'SCHEDULED', 'BETTING_OPEN', 'BETTING_CLOSED', 'RUNNING', 'FINISHED', 'SETTLED', 'CANCELLED'"
BET_TYPES = "'WIN', 'PLACE', 'EXACTA', 'TRIFECTA'"
BET_STATUSES = "'PENDING', 'WON', 'LOST', 'VOID', 'REFUNDED'"


def _audit_columns():
    """Colonnes de BaseModel (identiques aux autres tables)."""
    return [
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=True),
        sa.Column("updated_by", sa.String(36), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "game_rounds",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("game_type", sa.String(30), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("betting_opens_at", sa.DateTime(), nullable=True),
        sa.Column("betting_closes_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancel_reason", sa.String(200), nullable=True),
        sa.Column("participants", sa.JSON(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("race_script", sa.JSON(), nullable=True),
        sa.Column("server_seed_hash", sa.String(64), nullable=True),
        sa.Column("server_seed", sa.String(64), nullable=True),
        sa.Column("nonce", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_bets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_stake", sa.Numeric(14, 2), server_default="0", nullable=False),
        sa.Column("total_payout", sa.Numeric(14, 2), server_default="0", nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_type", "round_number", name="uq_game_rounds_type_number"),
        sa.CheckConstraint(f"status IN ({ROUND_STATUSES})", name="ck_game_rounds_status"),
        sa.CheckConstraint("total_stake >= 0", name="ck_game_rounds_total_stake_positive"),
        sa.CheckConstraint("total_payout >= 0", name="ck_game_rounds_total_payout_positive"),
    )
    op.create_index("idx_game_rounds_type_status", "game_rounds", ["game_type", "status"])
    op.create_index("idx_game_rounds_scheduled_at", "game_rounds", ["scheduled_at"])

    op.create_table(
        "game_bets",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("round_id", sa.String(36), nullable=False),
        sa.Column("game_type", sa.String(30), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=True),
        sa.Column("ticket_id", sa.String(36), nullable=True),
        sa.Column("agent_id", sa.String(36), nullable=True),
        sa.Column("bet_type", sa.String(20), nullable=False),
        sa.Column("selection", sa.JSON(), nullable=False),
        sa.Column("stake", sa.Numeric(10, 2), nullable=False),
        sa.Column("odds", sa.Numeric(10, 2), nullable=False),
        sa.Column("potential_payout", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("winnings", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("placed_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("debit_transaction_id", sa.String(36), nullable=True),
        sa.Column("payout_transaction_id", sa.String(36), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["round_id"], ["game_rounds.id"], name="fk_game_bets_round_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_game_bets_user_id"),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], name="fk_game_bets_ticket_id"),
        sa.ForeignKeyConstraint(["agent_id"], ["users.id"], name="fk_game_bets_agent_id"),
        sa.ForeignKeyConstraint(["debit_transaction_id"], ["transactions.id"], name="fk_game_bets_debit_tx"),
        sa.ForeignKeyConstraint(["payout_transaction_id"], ["transactions.id"], name="fk_game_bets_payout_tx"),
        sa.CheckConstraint("stake > 0", name="ck_game_bets_stake_positive"),
        sa.CheckConstraint("odds >= 1", name="ck_game_bets_odds_min"),
        sa.CheckConstraint("winnings >= 0", name="ck_game_bets_winnings_positive"),
        sa.CheckConstraint("user_id IS NOT NULL OR ticket_id IS NOT NULL", name="ck_game_bets_funding_source"),
        sa.CheckConstraint(f"bet_type IN ({BET_TYPES})", name="ck_game_bets_bet_type"),
        sa.CheckConstraint(f"status IN ({BET_STATUSES})", name="ck_game_bets_status"),
    )
    for name, cols in [
        ("idx_game_bets_round_id", ["round_id"]),
        ("idx_game_bets_user_id", ["user_id"]),
        ("idx_game_bets_ticket_id", ["ticket_id"]),
        ("idx_game_bets_agent_id", ["agent_id"]),
        ("idx_game_bets_status", ["status"]),
        ("idx_game_bets_placed_at", ["placed_at"]),
    ]:
        op.create_index(name, "game_bets", cols)


def downgrade() -> None:
    op.drop_table("game_bets")      # supprime aussi ses index et FK
    op.drop_table("game_rounds")
