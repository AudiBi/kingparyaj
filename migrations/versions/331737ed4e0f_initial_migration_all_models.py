"""Initial migration - all models (clean version with no circular dependencies)

Revision ID: 331737ed4e0f
Revises: 
Create Date: 2026-06-26 11:40:51.039527

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# Révision
revision = '331737ed4e0f'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Migration propre en 3 étapes :
    1. Création de toutes les tables SANS clés étrangères
    2. Ajout des clés étrangères
    3. Création des indexes
    """
    
    # ============================================================
    # ÉTAPE 1 : CRÉATION DE TOUTES LES TABLES (SANS FK)
    # ============================================================
    
    # 1. Table users (corrigée - une seule colonne id)
    op.create_table('users',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('email', sa.String(120), nullable=True),
        sa.Column('phone', sa.String(20), nullable=False),
        sa.Column('first_name', sa.String(50), nullable=True),
        sa.Column('last_name', sa.String(50), nullable=True),
        sa.Column('national_id', sa.String(20), nullable=True),
        sa.Column('password_hash', sa.String(200), nullable=False),
        sa.Column('two_factor_secret', sa.String(32), nullable=True),
        sa.Column('two_factor_enabled', sa.Boolean(), nullable=False),
        sa.Column('refresh_token', sa.String(500), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('is_locked', sa.Boolean(), nullable=False),
        sa.Column('locked_at', sa.DateTime(), nullable=True),
        sa.Column('lock_reason', sa.String(200), nullable=True),
        sa.Column('kyc_status', sa.Enum('PENDING', 'VERIFIED', 'REJECTED', 'EXPIRED', name='kycstatus'), nullable=False),
        sa.Column('kyc_verified_at', sa.DateTime(), nullable=True),
        sa.Column('kyc_verified_by', sa.String(36), nullable=True),
        sa.Column('kyc_documents', sa.Text(), nullable=True),
        sa.Column('role', sa.Enum('PLAYER', 'AGENT', 'MANAGER', 'ADMIN', 'SUPER_ADMIN', name='userrole'), nullable=False),
        sa.Column('bureau_id', sa.String(36), nullable=True),
        sa.Column('total_bets_count', sa.Integer(), nullable=False),
        sa.Column('total_bets_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_wins', sa.Numeric(12, 2), nullable=False),
        sa.Column('last_login', sa.DateTime(), nullable=True),
        sa.Column('last_ip', sa.String(45), nullable=True),
        sa.Column('referrer_id', sa.String(36), nullable=True),
        sa.Column('referral_code', sa.String(20), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
        sa.UniqueConstraint('phone'),
        sa.UniqueConstraint('national_id'),
        sa.UniqueConstraint('referral_code')
    )
    
    # 2. Table bureaus
    op.create_table('bureaus',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('address', sa.String(200), nullable=True),
        sa.Column('city', sa.String(50), nullable=True),
        sa.Column('commune', sa.String(50), nullable=True),
        sa.Column('department', sa.String(50), nullable=True),
        sa.Column('latitude', sa.String(20), nullable=True),
        sa.Column('longitude', sa.String(20), nullable=True),
        sa.Column('phone', sa.String(20), nullable=True),
        sa.Column('email', sa.String(120), nullable=True),
        sa.Column('manager_id', sa.String(36), nullable=True),
        sa.Column('cash_balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('safe_balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('cash_in_transit', sa.Numeric(12, 2), nullable=False),
        sa.Column('opening_hours', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('total_cash_in_today', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_cash_out_today', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_bets_today', sa.Integer(), nullable=False),
        sa.Column('last_cash_count', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )
    
    # 3. Table keno_draws
    op.create_table('keno_draws',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('draw_number', sa.Integer(), nullable=False),
        sa.Column('draw_time', sa.DateTime(), nullable=False),
        sa.Column('numbers', sa.ARRAY(sa.Integer()), nullable=True),
        sa.Column('status', sa.Enum('PENDING', 'COMPLETED', 'CANCELLED', name='kenodrawstatus'), nullable=False),
        sa.Column('total_bets', sa.Integer(), nullable=False),
        sa.Column('total_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_payout', sa.Numeric(12, 2), nullable=False),
        sa.Column('jackpot_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('jackpot_won', sa.Boolean(), nullable=False),
        sa.Column('jackpot_winner_id', sa.String(36), nullable=True),
        sa.Column('previous_draw_id', sa.String(36), nullable=True),
        sa.Column('next_draw_id', sa.String(36), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('closed_by', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('draw_number')
    )
    
    # 4. Table lucky_wheel_configs
    op.create_table('lucky_wheel_configs',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('description', sa.String(200), nullable=True),
        sa.Column('segments', sa.JSON(), nullable=False),
        sa.Column('min_bet', sa.Numeric(10, 2), nullable=False),
        sa.Column('max_bet', sa.Numeric(10, 2), nullable=False),
        sa.Column('theoretical_rtp', sa.Float(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # 5. Table promotions
    op.create_table('promotions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('code', sa.String(50), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('type', sa.Enum('DEPOSIT_BONUS', 'CASHBACK', 'FREE_BET', 'MULTIPLIER', 'REFERRAL', name='promotiontype'), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('start_date', sa.DateTime(), nullable=False),
        sa.Column('end_date', sa.DateTime(), nullable=False),
        sa.Column('min_deposit', sa.Numeric(10, 2), nullable=True),
        sa.Column('max_bonus', sa.Numeric(10, 2), nullable=True),
        sa.Column('wagering_requirement', sa.Integer(), nullable=False),
        sa.Column('eligible_games', sa.JSON(), nullable=False),
        sa.Column('eligible_countries', sa.JSON(), nullable=False),
        sa.Column('min_user_age', sa.Integer(), nullable=False),
        sa.Column('new_users_only', sa.Boolean(), nullable=False),
        sa.Column('first_deposit_only', sa.Boolean(), nullable=False),
        sa.Column('status', sa.Enum('DRAFT', 'ACTIVE', 'PAUSED', 'EXPIRED', name='promotionstatus'), nullable=False),
        sa.Column('total_budget', sa.Numeric(12, 2), nullable=True),
        sa.Column('used_budget', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_claims', sa.Integer(), nullable=False),
        sa.Column('total_bonus_given', sa.Numeric(12, 2), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code')
    )
    
    # 6. Table wallets
    op.create_table('wallets',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('bonus_balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('pending_withdrawals', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_deposited', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_withdrawn', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_won', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_bonus_received', sa.Numeric(12, 2), nullable=False),
        sa.Column('total_bonus_wagered', sa.Numeric(12, 2), nullable=False),
        sa.Column('daily_deposit_limit', sa.Numeric(10, 2), nullable=True),
        sa.Column('daily_loss_limit', sa.Numeric(10, 2), nullable=True),
        sa.Column('weekly_deposit_limit', sa.Numeric(10, 2), nullable=True),
        sa.Column('monthly_deposit_limit', sa.Numeric(10, 2), nullable=True),
        sa.Column('single_bet_limit', sa.Numeric(10, 2), nullable=True),
        sa.Column('today_deposits', sa.Numeric(12, 2), nullable=False),
        sa.Column('today_losses', sa.Numeric(12, 2), nullable=False),
        sa.Column('today_bets', sa.Numeric(12, 2), nullable=False),
        sa.Column('last_reset_date', sa.DateTime(), nullable=True),
        sa.Column('status', sa.Enum('ACTIVE', 'FROZEN', 'CLOSED', name='walletstatus'), nullable=False),
        sa.Column('frozen_reason', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
        sa.CheckConstraint('balance >= 0', name='ck_wallet_balance_positive'),
        sa.CheckConstraint('bonus_balance >= 0', name='ck_wallet_bonus_positive')
    )
    
    # 7. Table tickets
    op.create_table('tickets',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('bureau_id', sa.String(36), nullable=False),
        sa.Column('agent_id', sa.String(36), nullable=True),
        sa.Column('ticket_number', sa.String(20), nullable=False),
        sa.Column('player_name', sa.String(100), nullable=True),
        sa.Column('player_phone', sa.String(20), nullable=True),
        sa.Column('balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('initial_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', sa.Enum('ACTIVE', 'PAID', 'EXPIRED', 'CANCELLED', name='ticketstatus'), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('paid_at', sa.DateTime(), nullable=True),
        sa.Column('paid_by_agent', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ticket_number'),
        sa.CheckConstraint('balance >= 0', name='ck_ticket_balance_positive')
    )
    
    # 8. Table cashier_sessions
    op.create_table('cashier_sessions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('bureau_id', sa.String(36), nullable=False),
        sa.Column('agent_id', sa.String(36), nullable=False),
        sa.Column('starting_balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('current_balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('expected_balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('cash_in_count', sa.Integer(), nullable=False),
        sa.Column('cash_in_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('cash_out_count', sa.Integer(), nullable=False),
        sa.Column('cash_out_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('opened_at', sa.DateTime(), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('difference', sa.Numeric(12, 2), nullable=False),
        sa.Column('difference_reason', sa.String(200), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('starting_balance >= 0', name='ck_session_starting_positive'),
        sa.CheckConstraint('current_balance >= 0', name='ck_session_current_positive')
    )
    
    # 9. Table keno_bets
    op.create_table('keno_bets',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=True),
        sa.Column('ticket_id', sa.String(36), nullable=True),
        sa.Column('draw_id', sa.String(36), nullable=False),
        sa.Column('agent_id', sa.String(36), nullable=True),
        sa.Column('picks', sa.ARRAY(sa.Integer()), nullable=False),
        sa.Column('stake', sa.Numeric(10, 2), nullable=False),
        sa.Column('hits', sa.Integer(), nullable=False),
        sa.Column('multiplier', sa.Numeric(5, 2), nullable=False),
        sa.Column('winnings', sa.Numeric(10, 2), nullable=False),
        sa.Column('jackpot_win', sa.Boolean(), nullable=False),
        sa.Column('jackpot_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', sa.Enum('PENDING', 'WON', 'LOST', name='kenobetstatus'), nullable=False),
        sa.Column('bonus_multiplier', sa.Numeric(3, 2), nullable=False),
        sa.Column('placed_at', sa.DateTime(), nullable=False),
        sa.Column('settled_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('stake > 0', name='ck_keno_bet_stake_positive'),
        sa.CheckConstraint('stake <= 100000', name='ck_keno_bet_stake_max'),
        sa.CheckConstraint('array_length(picks, 1) >= 1', name='ck_keno_bet_picks_min'),
        sa.CheckConstraint('array_length(picks, 1) <= 10', name='ck_keno_bet_picks_max')
    )
    
    # 10. Table lucky_plays
    op.create_table('lucky_plays',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=True),
        sa.Column('ticket_id', sa.String(36), nullable=True),
        sa.Column('agent_id', sa.String(36), nullable=True),
        sa.Column('wheel_config_id', sa.String(36), nullable=False),
        sa.Column('game_type', sa.Enum('WHEEL', name='luckygametype'), nullable=False),
        sa.Column('stake', sa.Numeric(10, 2), nullable=False),
        sa.Column('result_segment', sa.JSON(), nullable=False),
        sa.Column('multiplier', sa.Numeric(5, 2), nullable=False),
        sa.Column('winnings', sa.Numeric(12, 2), nullable=False),
        sa.Column('random_seed', sa.String(100), nullable=True),
        sa.Column('verification_hash', sa.String(200), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('played_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('stake > 0', name='ck_lucky_play_stake_positive')
    )
    
    # 11. Table transactions
    op.create_table('transactions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('wallet_id', sa.String(36), nullable=False),
        sa.Column('reference', sa.String(50), nullable=False),
        sa.Column('transaction_type', sa.Enum('DEPOSIT', 'WITHDRAWAL', 'BET', 'WIN', 'BONUS', 'REFUND', 'ADJUSTMENT', name='transactiontype'), nullable=False),
        sa.Column('payment_method', sa.Enum('MONCASH', 'NATCASH', 'CASH', 'BANK_TRANSFER', 'CRYPTO', name='paymentmethod'), nullable=True),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('fee', sa.Numeric(10, 2), nullable=False),
        sa.Column('bonus_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('balance_before', sa.Numeric(12, 2), nullable=False),
        sa.Column('balance_after', sa.Numeric(12, 2), nullable=False),
        sa.Column('bet_id', sa.String(36), nullable=True),
        sa.Column('draw_id', sa.String(36), nullable=True),
        sa.Column('ticket_id', sa.String(36), nullable=True),
        sa.Column('status', sa.Enum('PENDING', 'COMPLETED', 'FAILED', 'CANCELLED', name='transactionstatus'), nullable=False),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('external_reference', sa.String(100), nullable=True),
        sa.Column('external_status', sa.String(50), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('user_agent', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('reference'),
        sa.CheckConstraint('amount >= 0', name='ck_transaction_amount_positive'),
        sa.CheckConstraint('fee >= 0', name='ck_transaction_fee_positive')
    )
    
    # 12. Table audit_logs
    op.create_table('audit_logs',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=True),
        sa.Column('agent_id', sa.String(36), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=False),
        sa.Column('user_agent', sa.String(500), nullable=True),
        sa.Column('session_id', sa.String(100), nullable=True),
        sa.Column('action', sa.Enum('LOGIN', 'LOGOUT', 'LOGIN_FAILED', 'PASSWORD_CHANGE', 'BET_PLACED', 'BET_SETTLED', 'DRAW_GENERATED', 'LUCKY_SPIN', 'DEPOSIT', 'WITHDRAWAL', 'TRANSFER', 'USER_CREATED', 'USER_UPDATED', 'USER_BLOCKED', 'LIMIT_CHANGED', 'KYC_SUBMITTED', 'KYC_VERIFIED', 'SELF_EXCLUSION', 'ACCOUNT_FROZEN', name='auditaction'), nullable=False),
        sa.Column('resource_type', sa.String(50), nullable=True),
        sa.Column('resource_id', sa.String(36), nullable=True),
        sa.Column('old_values', sa.JSON(), nullable=True),
        sa.Column('new_values', sa.JSON(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('leh_exported', sa.Boolean(), nullable=False),
        sa.Column('leh_exported_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # 13. Table notifications
    op.create_table('notifications',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('notification_type', sa.Enum('BET_WON', 'DEPOSIT_CONFIRMED', 'WITHDRAWAL_PROCESSED', 'DRAW_RESULT', 'PROMOTION', 'ACCOUNT_ALERT', 'SECURITY_ALERT', 'SELF_EXCLUSION', name='notificationtype'), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=True),
        sa.Column('channel', sa.Enum('SMS', 'EMAIL', 'PUSH', 'IN_APP', name='notificationchannel'), nullable=False),
        sa.Column('status', sa.Enum('PENDING', 'SENT', 'DELIVERED', 'FAILED', 'READ', name='notificationstatus'), nullable=False),
        sa.Column('is_read', sa.Boolean(), nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.Column('external_message_id', sa.String(100), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False),
        sa.Column('max_retries', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # 14. Table self_exclusions
    op.create_table('self_exclusions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('exclusion_type', sa.Enum('TEMPORARY', 'PERMANENT', 'COOLING_OFF', name='exclusiontype'), nullable=False),
        sa.Column('start_date', sa.DateTime(), nullable=False),
        sa.Column('end_date', sa.DateTime(), nullable=True),
        sa.Column('reason', sa.Enum('SELF_REQUEST', 'COMPLIANCE', 'FRAUD', 'UNDERAGE', 'MONEY_LAUNDERING', name='exclusionreason'), nullable=False),
        sa.Column('reason_details', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('activated_at', sa.DateTime(), nullable=False),
        sa.Column('activated_by', sa.String(36), nullable=True),
        sa.Column('lifted_at', sa.DateTime(), nullable=True),
        sa.Column('lifted_by', sa.String(36), nullable=True),
        sa.Column('lift_reason', sa.Text(), nullable=True),
        sa.Column('detected_losses', sa.Numeric(12, 2), nullable=True),
        sa.Column('detected_bets_count', sa.Integer(), nullable=True),
        sa.Column('detection_period_days', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # 15. Table player_limits
    op.create_table('player_limits',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('limit_type', sa.String(30), nullable=False),
        sa.Column('limit_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('period_days', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('set_at', sa.DateTime(), nullable=False),
        sa.Column('set_by', sa.String(36), nullable=True),
        sa.Column('previous_limit', sa.Numeric(10, 2), nullable=True),
        sa.Column('modified_at', sa.DateTime(), nullable=True),
        sa.Column('modified_by', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # 16. Table user_promotions
    op.create_table('user_promotions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('promotion_id', sa.String(36), nullable=False),
        sa.Column('bonus_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('wagered_amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('wagering_required', sa.Integer(), nullable=False),
        sa.Column('is_completed', sa.Boolean(), nullable=False),
        sa.Column('is_expired', sa.Boolean(), nullable=False),
        sa.Column('claimed_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=True),
        sa.Column('updated_by', sa.String(36), nullable=True),
        sa.Column('is_deleted', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # ============================================================
    # ÉTAPE 2 : AJOUT DES CLÉS ÉTRANGÈRES
    # ============================================================
    
    # Foreign keys pour users
    op.create_foreign_key('fk_users_bureau_id', 'users', 'bureaus', ['bureau_id'], ['id'])
    op.create_foreign_key('fk_users_referrer_id', 'users', 'users', ['referrer_id'], ['id'])
    
    # Foreign keys pour bureaus
    op.create_foreign_key('fk_bureaus_manager_id', 'bureaus', 'users', ['manager_id'], ['id'])
    
    # Foreign keys pour wallets
    op.create_foreign_key('fk_wallets_user_id', 'wallets', 'users', ['user_id'], ['id'])
    
    # Foreign keys pour tickets
    op.create_foreign_key('fk_tickets_bureau_id', 'tickets', 'bureaus', ['bureau_id'], ['id'])
    op.create_foreign_key('fk_tickets_agent_id', 'tickets', 'users', ['agent_id'], ['id'])
    
    # Foreign keys pour cashier_sessions
    op.create_foreign_key('fk_cashier_sessions_bureau_id', 'cashier_sessions', 'bureaus', ['bureau_id'], ['id'])
    op.create_foreign_key('fk_cashier_sessions_agent_id', 'cashier_sessions', 'users', ['agent_id'], ['id'])
    
    # Foreign keys pour keno_bets
    op.create_foreign_key('fk_keno_bets_user_id', 'keno_bets', 'users', ['user_id'], ['id'])
    op.create_foreign_key('fk_keno_bets_ticket_id', 'keno_bets', 'tickets', ['ticket_id'], ['id'])
    op.create_foreign_key('fk_keno_bets_draw_id', 'keno_bets', 'keno_draws', ['draw_id'], ['id'])
    op.create_foreign_key('fk_keno_bets_agent_id', 'keno_bets', 'users', ['agent_id'], ['id'])
    
    # Foreign keys pour lucky_plays
    op.create_foreign_key('fk_lucky_plays_user_id', 'lucky_plays', 'users', ['user_id'], ['id'])
    op.create_foreign_key('fk_lucky_plays_ticket_id', 'lucky_plays', 'tickets', ['ticket_id'], ['id'])
    op.create_foreign_key('fk_lucky_plays_agent_id', 'lucky_plays', 'users', ['agent_id'], ['id'])
    op.create_foreign_key('fk_lucky_plays_wheel_config_id', 'lucky_plays', 'lucky_wheel_configs', ['wheel_config_id'], ['id'])
    
    # Foreign keys pour transactions
    op.create_foreign_key('fk_transactions_user_id', 'transactions', 'users', ['user_id'], ['id'])
    op.create_foreign_key('fk_transactions_wallet_id', 'transactions', 'wallets', ['wallet_id'], ['id'])
    
    # Foreign keys pour audit_logs
    op.create_foreign_key('fk_audit_logs_user_id', 'audit_logs', 'users', ['user_id'], ['id'])
    
    # Foreign keys pour notifications
    op.create_foreign_key('fk_notifications_user_id', 'notifications', 'users', ['user_id'], ['id'])
    
    # Foreign keys pour self_exclusions
    op.create_foreign_key('fk_self_exclusions_user_id', 'self_exclusions', 'users', ['user_id'], ['id'])
    
    # Foreign keys pour player_limits
    op.create_foreign_key('fk_player_limits_user_id', 'player_limits', 'users', ['user_id'], ['id'])
    
    # Foreign keys pour user_promotions
    op.create_foreign_key('fk_user_promotions_user_id', 'user_promotions', 'users', ['user_id'], ['id'])
    op.create_foreign_key('fk_user_promotions_promotion_id', 'user_promotions', 'promotions', ['promotion_id'], ['id'])
    
    # ============================================================
    # ÉTAPE 3 : CRÉATION DES INDEX
    # ============================================================
    
    # Index pour users
    op.create_index('idx_users_phone', 'users', ['phone'])
    op.create_index('idx_users_email', 'users', ['email'])
    op.create_index('idx_users_national_id', 'users', ['national_id'])
    op.create_index('idx_users_role', 'users', ['role'])
    op.create_index('idx_users_bureau_id', 'users', ['bureau_id'])
    op.create_index('idx_users_referral_code', 'users', ['referral_code'])
    op.create_index('idx_users_created_at', 'users', ['created_at'])
    
    # Index pour bureaus
    op.create_index('idx_bureaus_code', 'bureaus', ['code'], unique=True)
    op.create_index('idx_bureaus_city', 'bureaus', ['city'])
    op.create_index('idx_bureaus_manager_id', 'bureaus', ['manager_id'])
    
    # Index pour keno_draws
    op.create_index('idx_keno_draws_draw_number', 'keno_draws', ['draw_number'], unique=True)
    op.create_index('idx_keno_draws_draw_time', 'keno_draws', ['draw_time'])
    op.create_index('idx_keno_draws_status', 'keno_draws', ['status'])
    
    # Index pour lucky_wheel_configs
    op.create_index('idx_lucky_wheel_configs_is_active', 'lucky_wheel_configs', ['is_active'])
    op.create_index('idx_lucky_wheel_configs_is_default', 'lucky_wheel_configs', ['is_default'])
    
    # Index pour promotions
    op.create_index('idx_promotions_code', 'promotions', ['code'], unique=True)
    op.create_index('idx_promotions_type', 'promotions', ['type'])
    op.create_index('idx_promotions_status', 'promotions', ['status'])
    op.create_index('idx_promotions_start_date', 'promotions', ['start_date'])
    op.create_index('idx_promotions_end_date', 'promotions', ['end_date'])
    
    # Index pour wallets
    op.create_index('idx_wallets_user_id', 'wallets', ['user_id'])
    op.create_index('idx_wallets_status', 'wallets', ['status'])
    
    # Index pour tickets
    op.create_index('idx_tickets_ticket_number', 'tickets', ['ticket_number'], unique=True)
    op.create_index('idx_tickets_bureau_id', 'tickets', ['bureau_id'])
    op.create_index('idx_tickets_agent_id', 'tickets', ['agent_id'])
    op.create_index('idx_tickets_status', 'tickets', ['status'])
    op.create_index('idx_tickets_expires_at', 'tickets', ['expires_at'])
    
    # Index pour cashier_sessions
    op.create_index('idx_cashier_sessions_bureau_id', 'cashier_sessions', ['bureau_id'])
    op.create_index('idx_cashier_sessions_agent_id', 'cashier_sessions', ['agent_id'])
    op.create_index('idx_cashier_sessions_status', 'cashier_sessions', ['status'])
    op.create_index('idx_cashier_sessions_opened_at', 'cashier_sessions', ['opened_at'])
    
    # Index pour keno_bets
    op.create_index('idx_keno_bets_user_id', 'keno_bets', ['user_id'])
    op.create_index('idx_keno_bets_draw_id', 'keno_bets', ['draw_id'])
    op.create_index('idx_keno_bets_ticket_id', 'keno_bets', ['ticket_id'])
    op.create_index('idx_keno_bets_agent_id', 'keno_bets', ['agent_id'])
    op.create_index('idx_keno_bets_status', 'keno_bets', ['status'])
    op.create_index('idx_keno_bets_placed_at', 'keno_bets', ['placed_at'])
    
    # Index pour lucky_plays
    op.create_index('idx_lucky_plays_user_id', 'lucky_plays', ['user_id'])
    op.create_index('idx_lucky_plays_ticket_id', 'lucky_plays', ['ticket_id'])
    op.create_index('idx_lucky_plays_agent_id', 'lucky_plays', ['agent_id'])
    op.create_index('idx_lucky_plays_played_at', 'lucky_plays', ['played_at'])
    op.create_index('idx_lucky_plays_wheel_config_id', 'lucky_plays', ['wheel_config_id'])
    
    # Index pour transactions
    op.create_index('idx_transactions_reference', 'transactions', ['reference'], unique=True)
    op.create_index('idx_transactions_user_id', 'transactions', ['user_id'])
    op.create_index('idx_transactions_wallet_id', 'transactions', ['wallet_id'])
    op.create_index('idx_transactions_type', 'transactions', ['transaction_type'])
    op.create_index('idx_transactions_status', 'transactions', ['status'])
    op.create_index('idx_transactions_created_at', 'transactions', ['created_at'])
    op.create_index('idx_transactions_external_reference', 'transactions', ['external_reference'])
    
    # Index pour audit_logs
    op.create_index('idx_audit_logs_user_id', 'audit_logs', ['user_id'])
    op.create_index('idx_audit_logs_action', 'audit_logs', ['action'])
    op.create_index('idx_audit_logs_resource_type', 'audit_logs', ['resource_type'])
    op.create_index('idx_audit_logs_created_at', 'audit_logs', ['created_at'])
    op.create_index('idx_audit_logs_ip_address', 'audit_logs', ['ip_address'])
    op.create_index('idx_audit_logs_leh_exported', 'audit_logs', ['leh_exported'])
    
    # Index pour notifications
    op.create_index('idx_notifications_user_id', 'notifications', ['user_id'])
    op.create_index('idx_notifications_type', 'notifications', ['notification_type'])
    op.create_index('idx_notifications_channel', 'notifications', ['channel'])
    op.create_index('idx_notifications_status', 'notifications', ['status'])
    op.create_index('idx_notifications_created_at', 'notifications', ['created_at'])
    op.create_index('idx_notifications_is_read', 'notifications', ['is_read'])
    
    # Index pour self_exclusions
    op.create_index('idx_self_exclusions_user_id', 'self_exclusions', ['user_id'])
    op.create_index('idx_self_exclusions_is_active', 'self_exclusions', ['is_active'])
    op.create_index('idx_self_exclusions_start_date', 'self_exclusions', ['start_date'])
    op.create_index('idx_self_exclusions_end_date', 'self_exclusions', ['end_date'])
    
    # Index pour player_limits
    op.create_index('idx_player_limits_user_id', 'player_limits', ['user_id'])
    op.create_index('idx_player_limits_limit_type', 'player_limits', ['limit_type'])
    op.create_index('idx_player_limits_is_active', 'player_limits', ['is_active'])
    
    # Index pour user_promotions
    op.create_index('idx_user_promotions_user_id', 'user_promotions', ['user_id'])
    op.create_index('idx_user_promotions_promotion_id', 'user_promotions', ['promotion_id'])
    op.create_index('idx_user_promotions_is_completed', 'user_promotions', ['is_completed'])


def downgrade() -> None:
    """Annulation de la migration - suppression dans l'ordre inverse"""
    
    # Supprimer les index
    op.drop_index('idx_user_promotions_is_completed', table_name='user_promotions')
    op.drop_index('idx_user_promotions_promotion_id', table_name='user_promotions')
    op.drop_index('idx_user_promotions_user_id', table_name='user_promotions')
    op.drop_index('idx_player_limits_is_active', table_name='player_limits')
    op.drop_index('idx_player_limits_limit_type', table_name='player_limits')
    op.drop_index('idx_player_limits_user_id', table_name='player_limits')
    op.drop_index('idx_self_exclusions_end_date', table_name='self_exclusions')
    op.drop_index('idx_self_exclusions_is_active', table_name='self_exclusions')
    op.drop_index('idx_self_exclusions_start_date', table_name='self_exclusions')
    op.drop_index('idx_self_exclusions_user_id', table_name='self_exclusions')
    op.drop_index('idx_notifications_is_read', table_name='notifications')
    op.drop_index('idx_notifications_created_at', table_name='notifications')
    op.drop_index('idx_notifications_status', table_name='notifications')
    op.drop_index('idx_notifications_channel', table_name='notifications')
    op.drop_index('idx_notifications_type', table_name='notifications')
    op.drop_index('idx_notifications_user_id', table_name='notifications')
    op.drop_index('idx_audit_logs_leh_exported', table_name='audit_logs')
    op.drop_index('idx_audit_logs_ip_address', table_name='audit_logs')
    op.drop_index('idx_audit_logs_created_at', table_name='audit_logs')
    op.drop_index('idx_audit_logs_resource_type', table_name='audit_logs')
    op.drop_index('idx_audit_logs_action', table_name='audit_logs')
    op.drop_index('idx_audit_logs_user_id', table_name='audit_logs')
    op.drop_index('idx_transactions_external_reference', table_name='transactions')
    op.drop_index('idx_transactions_created_at', table_name='transactions')
    op.drop_index('idx_transactions_status', table_name='transactions')
    op.drop_index('idx_transactions_type', table_name='transactions')
    op.drop_index('idx_transactions_wallet_id', table_name='transactions')
    op.drop_index('idx_transactions_user_id', table_name='transactions')
    op.drop_index('idx_transactions_reference', table_name='transactions')
    op.drop_index('idx_lucky_plays_wheel_config_id', table_name='lucky_plays')
    op.drop_index('idx_lucky_plays_played_at', table_name='lucky_plays')
    op.drop_index('idx_lucky_plays_agent_id', table_name='lucky_plays')
    op.drop_index('idx_lucky_plays_ticket_id', table_name='lucky_plays')
    op.drop_index('idx_lucky_plays_user_id', table_name='lucky_plays')
    op.drop_index('idx_keno_bets_placed_at', table_name='keno_bets')
    op.drop_index('idx_keno_bets_status', table_name='keno_bets')
    op.drop_index('idx_keno_bets_agent_id', table_name='keno_bets')
    op.drop_index('idx_keno_bets_ticket_id', table_name='keno_bets')
    op.drop_index('idx_keno_bets_draw_id', table_name='keno_bets')
    op.drop_index('idx_keno_bets_user_id', table_name='keno_bets')
    op.drop_index('idx_cashier_sessions_opened_at', table_name='cashier_sessions')
    op.drop_index('idx_cashier_sessions_status', table_name='cashier_sessions')
    op.drop_index('idx_cashier_sessions_agent_id', table_name='cashier_sessions')
    op.drop_index('idx_cashier_sessions_bureau_id', table_name='cashier_sessions')
    op.drop_index('idx_tickets_expires_at', table_name='tickets')
    op.drop_index('idx_tickets_status', table_name='tickets')
    op.drop_index('idx_tickets_agent_id', table_name='tickets')
    op.drop_index('idx_tickets_bureau_id', table_name='tickets')
    op.drop_index('idx_tickets_ticket_number', table_name='tickets')
    op.drop_index('idx_wallets_status', table_name='wallets')
    op.drop_index('idx_wallets_user_id', table_name='wallets')
    op.drop_index('idx_promotions_end_date', table_name='promotions')
    op.drop_index('idx_promotions_start_date', table_name='promotions')
    op.drop_index('idx_promotions_status', table_name='promotions')
    op.drop_index('idx_promotions_type', table_name='promotions')
    op.drop_index('idx_promotions_code', table_name='promotions')
    op.drop_index('idx_lucky_wheel_configs_is_default', table_name='lucky_wheel_configs')
    op.drop_index('idx_lucky_wheel_configs_is_active', table_name='lucky_wheel_configs')
    op.drop_index('idx_keno_draws_status', table_name='keno_draws')
    op.drop_index('idx_keno_draws_draw_time', table_name='keno_draws')
    op.drop_index('idx_keno_draws_draw_number', table_name='keno_draws')
    op.drop_index('idx_bureaus_manager_id', table_name='bureaus')
    op.drop_index('idx_bureaus_city', table_name='bureaus')
    op.drop_index('idx_bureaus_code', table_name='bureaus')
    op.drop_index('idx_users_created_at', table_name='users')
    op.drop_index('idx_users_referral_code', table_name='users')
    op.drop_index('idx_users_bureau_id', table_name='users')
    op.drop_index('idx_users_role', table_name='users')
    op.drop_index('idx_users_national_id', table_name='users')
    op.drop_index('idx_users_email', table_name='users')
    op.drop_index('idx_users_phone', table_name='users')
    
    # Supprimer les clés étrangères
    op.drop_constraint('fk_user_promotions_promotion_id', 'user_promotions', type_='foreignkey')
    op.drop_constraint('fk_user_promotions_user_id', 'user_promotions', type_='foreignkey')
    op.drop_constraint('fk_player_limits_user_id', 'player_limits', type_='foreignkey')
    op.drop_constraint('fk_self_exclusions_user_id', 'self_exclusions', type_='foreignkey')
    op.drop_constraint('fk_notifications_user_id', 'notifications', type_='foreignkey')
    op.drop_constraint('fk_audit_logs_user_id', 'audit_logs', type_='foreignkey')
    op.drop_constraint('fk_transactions_wallet_id', 'transactions', type_='foreignkey')
    op.drop_constraint('fk_transactions_user_id', 'transactions', type_='foreignkey')
    op.drop_constraint('fk_lucky_plays_wheel_config_id', 'lucky_plays', type_='foreignkey')
    op.drop_constraint('fk_lucky_plays_agent_id', 'lucky_plays', type_='foreignkey')
    op.drop_constraint('fk_lucky_plays_ticket_id', 'lucky_plays', type_='foreignkey')
    op.drop_constraint('fk_lucky_plays_user_id', 'lucky_plays', type_='foreignkey')
    op.drop_constraint('fk_keno_bets_agent_id', 'keno_bets', type_='foreignkey')
    op.drop_constraint('fk_keno_bets_draw_id', 'keno_bets', type_='foreignkey')
    op.drop_constraint('fk_keno_bets_ticket_id', 'keno_bets', type_='foreignkey')
    op.drop_constraint('fk_keno_bets_user_id', 'keno_bets', type_='foreignkey')
    op.drop_constraint('fk_cashier_sessions_agent_id', 'cashier_sessions', type_='foreignkey')
    op.drop_constraint('fk_cashier_sessions_bureau_id', 'cashier_sessions', type_='foreignkey')
    op.drop_constraint('fk_tickets_agent_id', 'tickets', type_='foreignkey')
    op.drop_constraint('fk_tickets_bureau_id', 'tickets', type_='foreignkey')
    op.drop_constraint('fk_wallets_user_id', 'wallets', type_='foreignkey')
    op.drop_constraint('fk_bureaus_manager_id', 'bureaus', type_='foreignkey')
    op.drop_constraint('fk_users_referrer_id', 'users', type_='foreignkey')
    op.drop_constraint('fk_users_bureau_id', 'users', type_='foreignkey')
    
    # Supprimer les tables dans l'ordre inverse de la création
    op.drop_table('user_promotions')
    op.drop_table('player_limits')
    op.drop_table('self_exclusions')
    op.drop_table('notifications')
    op.drop_table('audit_logs')
    op.drop_table('transactions')
    op.drop_table('lucky_plays')
    op.drop_table('keno_bets')
    op.drop_table('cashier_sessions')
    op.drop_table('tickets')
    op.drop_table('wallets')
    op.drop_table('promotions')
    op.drop_table('lucky_wheel_configs')
    op.drop_table('keno_draws')
    op.drop_table('bureaus')
    op.drop_table('users')