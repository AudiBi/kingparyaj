# app/models/enums.py
"""Énumérations partagées pour tous les modèles"""

import enum


class UserRole(str, enum.Enum):
    """Rôles utilisateur"""
    PLAYER = "player"
    AGENT = "agent"
    MANAGER = "manager"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class KYCStatus(str, enum.Enum):
    """Statut KYC (Know Your Customer)"""
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"


class WalletStatus(str, enum.Enum):
    """Statut du portefeuille"""
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class TransactionType(str, enum.Enum):
    """Type de transaction"""
    DEPOSIT = "deposit"           # Dépôt (Mobile Money, Cash)
    WITHDRAWAL = "withdrawal"     # Retrait
    BET = "bet"                   # Pari placé
    WIN = "win"                   # Gain
    BONUS = "bonus"               # Bonus crédité
    REFUND = "refund"             # Remboursement
    ADJUSTMENT = "adjustment"     # Ajustement admin


class PaymentMethod(str, enum.Enum):
    """Méthode de paiement"""
    MONCASH = "moncash"           # Digicel Haïti
    NATCASH = "natcash"           # NatCom Haïti
    CASH = "cash"                 # Espèce au bureau
    BANK_TRANSFER = "bank_transfer"
    CRYPTO = "crypto"


class TransactionStatus(str, enum.Enum):
    """Statut d'une transaction"""
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KenoDrawStatus(str, enum.Enum):
    """Statut d'un tirage Keno"""
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class KenoBetStatus(str, enum.Enum):
    """Statut d'un pari Keno"""
    PENDING = "pending"
    WON = "won"
    LOST = "lost"


class LuckyGameType(str, enum.Enum):
    """Types de jeux Lucky"""
    WHEEL = "wheel"               # Roue de la chance


class TicketStatus(str, enum.Enum):
    """Statut d'un ticket"""
    ACTIVE = "active"
    PAID = "paid"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class NotificationChannel(str, enum.Enum):
    """Canaux de notification"""
    SMS = "sms"
    EMAIL = "email"
    PUSH = "push"
    IN_APP = "in_app"


class NotificationType(str, enum.Enum):
    """Types de notification"""
    BET_WON = "bet_won"
    DEPOSIT_CONFIRMED = "deposit_confirmed"
    WITHDRAWAL_PROCESSED = "withdrawal_processed"
    DRAW_RESULT = "draw_result"
    PROMOTION = "promotion"
    ACCOUNT_ALERT = "account_alert"
    SECURITY_ALERT = "security_alert"
    SELF_EXCLUSION = "self_exclusion"


class NotificationStatus(str, enum.Enum):
    """Statut d'une notification"""
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    READ = "read"


class ExclusionType(str, enum.Enum):
    """Type d'exclusion"""
    TEMPORARY = "temporary"
    PERMANENT = "permanent"
    COOLING_OFF = "cooling_off"


class ExclusionReason(str, enum.Enum):
    """Raison d'exclusion"""
    SELF_REQUEST = "self_request"
    COMPLIANCE = "compliance"
    FRAUD = "fraud"
    UNDERAGE = "underage"
    MONEY_LAUNDERING = "money_laundering"


class AuditAction(str, enum.Enum):
    """Actions d'audit"""
    # Authentification
    LOGIN = "login"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    PASSWORD_CHANGE = "password_change"
    
    # Jeu
    BET_PLACED = "bet_placed"
    BET_SETTLED = "bet_settled"
    DRAW_GENERATED = "draw_generated"
    LUCKY_SPIN = "lucky_spin"
    
    # Finances
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    
    # Administration
    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    USER_BLOCKED = "user_blocked"
    LIMIT_CHANGED = "limit_changed"
    
    # Conformité
    KYC_SUBMITTED = "kyc_submitted"
    KYC_VERIFIED = "kyc_verified"
    SELF_EXCLUSION = "self_exclusion"
    ACCOUNT_FROZEN = "account_frozen"


class PromotionType(str, enum.Enum):
    """Types de promotion"""
    DEPOSIT_BONUS = "deposit_bonus"
    CASHBACK = "cashback"
    FREE_BET = "free_bet"
    MULTIPLIER = "multiplier"
    REFERRAL = "referral"


class PromotionStatus(str, enum.Enum):
    """Statut d'une promotion"""
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"