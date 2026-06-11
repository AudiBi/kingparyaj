# app/models/__init__.py
"""Modèles SQLAlchemy pour le projet Parier Keno & Lucky"""

from app.models.base import BaseModel
from app.models.user import User, UserRole, KYCStatus
from app.models.wallet import Wallet, WalletStatus
from app.models.ticket import Ticket, TicketStatus
from app.models.bureau import Bureau, CashierSession
from app.models.keno import KenoDraw, KenoBet, KenoDrawStatus, KenoBetStatus
from app.models.lucky import LuckyWheelConfig, LuckyPlay, LuckyGameType
from app.models.transaction import Transaction, TransactionStatus, TransactionType, PaymentMethod
from app.models.audit import AuditLog, AuditAction
from app.models.notification import Notification, NotificationChannel, NotificationType, NotificationStatus
from app.models.responsible import SelfExclusion, ExclusionType, ExclusionReason, PlayerLimit
from app.models.promotion import Promotion, PromotionType, PromotionStatus, UserPromotion

__all__ = [
    # Base
    "BaseModel",
    
    # User
    "User", "UserRole", "KYCStatus",
    
    # Wallet
    "Wallet", "WalletStatus",
    
    # Ticket
    "Ticket", "TicketStatus",
    
    # Bureau
    "Bureau", "CashierSession",
    
    # Keno
    "KenoDraw", "KenoBet", "KenoDrawStatus", "KenoBetStatus",
    
    # Lucky
    "LuckyWheelConfig", "LuckyPlay", "LuckyGameType",
    
    # Transaction
    "Transaction", "TransactionStatus", "TransactionType", "PaymentMethod",
    
    # Audit
    "AuditLog", "AuditAction",
    
    # Notification
    "Notification", "NotificationChannel", "NotificationType", "NotificationStatus",
    
    # Responsible
    "SelfExclusion", "ExclusionType", "ExclusionReason", "PlayerLimit",
    
    # Promotion
    "Promotion", "PromotionType", "PromotionStatus", "UserPromotion",
]