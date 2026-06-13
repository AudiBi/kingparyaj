# app/models/__init__.py
"""Modèles SQLAlchemy pour le projet Parier Keno & Lucky"""

# IMPORTANT: D'abord importer Base depuis database
from app.core.database import Base

# Enums d'abord (pas de dépendances circulaires)
from app.models.enums import (
    UserRole, KYCStatus, WalletStatus, TicketStatus,
    KenoDrawStatus, KenoBetStatus, LuckyGameType,
    TransactionType, PaymentMethod, TransactionStatus,
    AuditAction, NotificationChannel, NotificationType, NotificationStatus,
    ExclusionType, ExclusionReason, PromotionType, PromotionStatus
)

# Puis les modèles (importation après les enums)
from app.models.base import BaseModel
from app.models.user import User
from app.models.wallet import Wallet
from app.models.ticket import Ticket
from app.models.bureau import Bureau, CashierSession
from app.models.keno import KenoDraw, KenoBet
from app.models.lucky import LuckyWheelConfig, LuckyPlay
from app.models.transaction import Transaction
from app.models.audit import AuditLog
from app.models.notification import Notification
from app.models.responsible import SelfExclusion, PlayerLimit
from app.models.promotion import Promotion, UserPromotion

# Exporter Base pour Alembic (CRITIQUE)
__all__ = [
    # Alembic - NE PAS ENLEVER
    "Base",
    
    # Enums
    "UserRole", "KYCStatus", "WalletStatus", "TicketStatus",
    "KenoDrawStatus", "KenoBetStatus", "LuckyGameType",
    "TransactionType", "PaymentMethod", "TransactionStatus",
    "AuditAction", "NotificationChannel", "NotificationType", "NotificationStatus",
    "ExclusionType", "ExclusionReason", "PromotionType", "PromotionStatus",
    
    # Modèles
    "BaseModel",
    "User", "Wallet", "Ticket", "Bureau", "CashierSession",
    "KenoDraw", "KenoBet", "LuckyWheelConfig", "LuckyPlay",
    "Transaction", "AuditLog", "Notification",
    "SelfExclusion", "PlayerLimit", "Promotion", "UserPromotion",
]