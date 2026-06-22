# app/schemas/__init__.py
"""Schémas Pydantic pour l'API"""

from app.schemas.admin import (
    # Auth
    AdminLogin,
    AdminTokenResponse,
    
    # Users
    AdminUserBase,
    AdminUserCreate,
    AdminUserUpdate,
    AdminUserResponse,
    
    # Agents
    AdminAgentCreate,
    AdminAgentUpdate,
    
    # Bureaus
    AdminBureauBase,
    AdminBureauCreate,
    AdminBureauUpdate,
    
    # Keno
    AdminKenoConfig,
    AdminKenoPaytableUpdate,
    
    # Lucky
    LuckyWheelSegment,
    AdminLuckyConfig,
    
    # Transactions
    AdminTransactionFilter,
    
    # Reports
    AdminReportRequest,
    AdminFinancialReport,
    AdminGameReport,
    AdminComplianceReport,
    
    # Promotions
    AdminPromotionBase,
    AdminPromotionCreate,
    AdminPromotionUpdate,
    
    # Audit
    AdminAuditFilter,
    AdminAuditExportRequest,
    
    # Settings
    AdminSettings,
    AdminSecuritySettings,
    
    # Dashboard
    AdminDashboardStats,
    AdminDashboardCharts,
    
    # Responses
    AdminApiResponse,
    AdminPaginatedResponse,
)

from app.schemas.common import (
    PaginatedResponse,
    PaginationParams,
    MessageResponse,
    ErrorResponse,
    HealthResponse,
)
from app.schemas.user import (
    UserBase,
    UserCreate,
    UserUpdate,
    UserResponse,
    UserLogin,
    UserLoginResponse,
    TokenRefresh,
    TokenResponse,
    UserProfileUpdate,
    KYCSubmission,
    KYCResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
)
from app.schemas.wallet import (
    WalletResponse,
    DepositRequest,
    WithdrawRequest,
    DepositResponse,
    WithdrawResponse,
    WalletTransactionResponse,
    BalanceResponse,
    LimitUpdateRequest,
    LimitResponse,
)
from app.schemas.keno import (
    KenoDrawResponse,
    KenoDrawListResponse,
    KenoBetCreate,
    KenoBetResponse,
    KenoBetListResponse,
    KenoBetTicketCreate,
    KenoResultResponse,
    KenoStatisticsResponse,
)
from app.schemas.lucky import (
    LuckyWheelConfigResponse,
    LuckyWheelConfigUpdate,
    LuckySpinRequest,
    LuckySpinResponse,
    LuckySpinTicketRequest,
    LuckyPlayHistoryResponse,
    LuckyStatisticsResponse,
)
from app.schemas.ticket import (
    TicketCreate,
    TicketResponse,
    TicketBetRequest,
    TicketBetResponse,
    TicketPayoutRequest,
    TicketPayoutResponse,
    TicketListResponse,
)
from app.schemas.bureau import (
    BureauResponse,
    BureauCreate,
    BureauUpdate,
    CashierSessionResponse,
    CashierSessionOpen,
    CashierSessionClose,
    CashierStatsResponse,
)
from app.schemas.transaction import (
    TransactionResponse,
    TransactionListResponse,
    TransactionFilter,
)
from app.schemas.audit import (
    AuditLogResponse,
    AuditLogListResponse,
    AuditFilter,
)
from app.schemas.notification import (
    NotificationResponse,
    NotificationListResponse,
    NotificationMarkRead,
)
from app.schemas.responsible import (
    SelfExclusionRequest,
    SelfExclusionResponse,
    SelfExclusionListResponse,
    PlayerLimitRequest,
    PlayerLimitResponse,
    ResponsibleGamblingStatus,
)
from app.schemas.promotion import (
    PromotionResponse,
    PromotionCreate,
    PromotionUpdate,
    PromotionListResponse,
    ClaimPromotionRequest,
    UserPromotionResponse,
)

__all__ = [
      # Admin
    "AdminLogin",
    "AdminTokenResponse",
    "AdminUserBase",
    "AdminUserCreate",
    "AdminUserUpdate",
    "AdminUserResponse",
    "AdminAgentCreate",
    "AdminAgentUpdate",
    "AdminBureauBase",
    "AdminBureauCreate",
    "AdminBureauUpdate",
    "AdminKenoConfig",
    "AdminKenoPaytableUpdate",
    "LuckyWheelSegment",
    "AdminLuckyConfig",
    "AdminTransactionFilter",
    "AdminReportRequest",
    "AdminFinancialReport",
    "AdminGameReport",
    "AdminComplianceReport",
    "AdminPromotionBase",
    "AdminPromotionCreate",
    "AdminPromotionUpdate",
    "AdminAuditFilter",
    "AdminAuditExportRequest",
    "AdminSettings",
    "AdminSecuritySettings",
    "AdminDashboardStats",
    "AdminDashboardCharts",
    "AdminApiResponse",
    "AdminPaginatedResponse",
    # Common
    "PaginatedResponse",
    "PaginationParams",
    "MessageResponse",
    "ErrorResponse",
    "HealthResponse",
    # User
    "UserBase",
    "UserCreate",
    "UserUpdate",
    "UserResponse",
    "UserLogin",
    "UserLoginResponse",
    "TokenRefresh",
    "TokenResponse",
    "UserProfileUpdate",
    "KYCSubmission",
    "KYCResponse",
    "ChangePasswordRequest",
    "ForgotPasswordRequest",
    "ResetPasswordRequest",
    # Wallet
    "WalletResponse",
    "DepositRequest",
    "WithdrawRequest",
    "DepositResponse",
    "WithdrawResponse",
    "WalletTransactionResponse",
    "BalanceResponse",
    "LimitUpdateRequest",
    "LimitResponse",
    # Keno
    "KenoDrawResponse",
    "KenoDrawListResponse",
    "KenoBetCreate",
    "KenoBetResponse",
    "KenoBetListResponse",
    "KenoBetTicketCreate",
    "KenoResultResponse",
    "KenoStatisticsResponse",
    # Lucky
    "LuckyWheelConfigResponse",
    "LuckyWheelConfigUpdate",
    "LuckySpinRequest",
    "LuckySpinResponse",
    "LuckySpinTicketRequest",
    "LuckyPlayHistoryResponse",
    "LuckyStatisticsResponse",
    # Ticket
    "TicketCreate",
    "TicketResponse",
    "TicketBetRequest",
    "TicketBetResponse",
    "TicketPayoutRequest",
    "TicketPayoutResponse",
    "TicketListResponse",
    # Bureau
    "BureauResponse",
    "BureauCreate",
    "BureauUpdate",
    "CashierSessionResponse",
    "CashierSessionOpen",
    "CashierSessionClose",
    "CashierStatsResponse",
    # Transaction
    "TransactionResponse",
    "TransactionListResponse",
    "TransactionFilter",
    # Audit
    "AuditLogResponse",
    "AuditLogListResponse",
    "AuditFilter",
    # Notification
    "NotificationResponse",
    "NotificationListResponse",
    "NotificationMarkRead",
    # Responsible
    "SelfExclusionRequest",
    "SelfExclusionResponse",
    "SelfExclusionListResponse",
    "PlayerLimitRequest",
    "PlayerLimitResponse",
    "ResponsibleGamblingStatus",
    # Promotion
    "PromotionResponse",
    "PromotionCreate",
    "PromotionUpdate",
    "PromotionListResponse",
    "ClaimPromotionRequest",
    "UserPromotionResponse",
]