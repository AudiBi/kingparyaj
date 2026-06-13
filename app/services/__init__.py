# app/services/__init__.py
"""Services métier de l'application Parier Keno & Lucky

Tous les services sont organisés par domaine fonctionnel :
- Authentification et utilisateurs
- Portefeuille et transactions
- Jeux (Keno, Lucky Wheel)
- Tickets et bureaux
- Audit et conformité LEH
- Notifications
"""

# ========== Services de base ==========
from app.services.base import BaseService

# ========== Services d'authentification et utilisateurs ==========
from app.services.auth_service import AuthService
from app.services.promotion_service import PromotionService
from app.services.responsible_service import ResponsibleService
from app.services.user_service import UserService

# ========== Services financiers ==========
from app.services.wallet_service import WalletService
from app.services.transaction_service import TransactionService

# ========== Services de jeux ==========
from app.services.keno_service import KenoService
from app.services.lucky_service import LuckyWheelService

# ========== Services de gestion des tickets et bureaux ==========
from app.services.ticket_service import TicketService
from app.services.bureau_service import BureauService, CashierSessionService

# ========== Services de conformité et audit ==========
from app.services.audit_service import AuditService

# ========== Services de communication ==========
from app.services.notification_service import NotificationService

# ========== Services techniques ==========
from app.services.rng_service import RNGService
from app.services.draw_scheduler import DrawScheduler

# ========== Services optionnels (à implémenter) ==========
# from app.services.responsible_service import ResponsibleService
# from app.services.promotion_service import PromotionService


__all__ = [
    # Base
    "BaseService",
    
    # Auth & Users
    "AuthService",
    "UserService",
    
    # Finances
    "WalletService",
    "TransactionService",
    
    # Jeux
    "KenoService",
    "LuckyWheelService",
    
    # Bureaux & Tickets
    "TicketService",
    "BureauService",
    "CashierSessionService",
    
    # Conformité
    "AuditService",
    
    # Communication
    "NotificationService",
    
    # Technique
    "RNGService",
    "DrawScheduler",

    # Transactions
    "TransactionService",
    
    # Jeu responsable
    "ResponsibleService",
    
    # Promotions
    "PromotionService",
]


# ========== Factory pour faciliter l'injection des dépendances ==========

class ServiceFactory:
    """
    Factory pour créer et gérer les services.
    Facilite l'injection des dépendances (db, redis).
    """
    
    def __init__(self, db_session, redis_client):
        self.db = db_session
        self.redis = redis_client
        self._services = {}
    
    @property
    def auth(self) -> AuthService:
        """Service d'authentification"""
        if "auth" not in self._services:
            self._services["auth"] = AuthService(self.db, self.redis)
        return self._services["auth"]
    
    @property
    def user(self) -> UserService:
        """Service utilisateur"""
        if "user" not in self._services:
            self._services["user"] = UserService(self.db, self.redis)
        return self._services["user"]
    
    @property
    def wallet(self) -> WalletService:
        """Service portefeuille"""
        if "wallet" not in self._services:
            self._services["wallet"] = WalletService(self.db, self.redis)
        return self._services["wallet"]
    
    @property
    def transaction(self) -> TransactionService:
        """Service transactions"""
        if "transaction" not in self._services:
            self._services["transaction"] = TransactionService(self.db, self.redis)
        return self._services["transaction"]
    
    @property
    def keno(self) -> KenoService:
        """Service Keno"""
        if "keno" not in self._services:
            self._services["keno"] = KenoService(self.db, self.redis)
        return self._services["keno"]
    
    @property
    def lucky(self) -> LuckyWheelService:
        """Service Lucky Wheel"""
        if "lucky" not in self._services:
            self._services["lucky"] = LuckyWheelService(self.db, self.redis)
        return self._services["lucky"]
    
    @property
    def ticket(self) -> TicketService:
        """Service tickets"""
        if "ticket" not in self._services:
            self._services["ticket"] = TicketService(self.db, self.redis)
        return self._services["ticket"]
    
    @property
    def bureau(self) -> BureauService:
        """Service bureaux"""
        if "bureau" not in self._services:
            self._services["bureau"] = BureauService(self.db, self.redis)
        return self._services["bureau"]
    
    @property
    def cashier_session(self) -> CashierSessionService:
        """Service sessions de caisse"""
        if "cashier_session" not in self._services:
            self._services["cashier_session"] = CashierSessionService(self.db, self.redis)
        return self._services["cashier_session"]
    
    @property
    def audit(self) -> AuditService:
        """Service audit"""
        if "audit" not in self._services:
            self._services["audit"] = AuditService(self.db, self.redis)
        return self._services["audit"]
    
    @property
    def notification(self) -> NotificationService:
        """Service notifications"""
        if "notification" not in self._services:
            self._services["notification"] = NotificationService(self.db, self.redis)
        return self._services["notification"]
    
    @property
    def rng(self) -> RNGService:
        """Service RNG (pas de dépendances DB)"""
        if "rng" not in self._services:
            self._services["rng"] = RNGService()
        return self._services["rng"]
    
    @property
    def draw_scheduler(self) -> DrawScheduler:
        """Scheduler de tirages"""
        if "draw_scheduler" not in self._services:
            self._services["draw_scheduler"] = DrawScheduler(self.db, self.redis)
        return self._services["draw_scheduler"]
    
    def clear_cache(self):
        """Vide le cache des services"""
        self._services.clear()
    
    def __repr__(self) -> str:
        return f"<ServiceFactory services={list(self._services.keys())}>"

    @property
    def transaction(self) -> TransactionService:
        """Service transactions"""
        if "transaction" not in self._services:
            self._services["transaction"] = TransactionService(self.db, self.redis)
        return self._services["transaction"]
    
    @property
    def responsible(self) -> ResponsibleService:
        """Service jeu responsable"""
        if "responsible" not in self._services:
            self._services["responsible"] = ResponsibleService(self.db, self.redis)
        return self._services["responsible"]
    
    @property
    def promotion(self) -> PromotionService:
        """Service promotions"""
        if "promotion" not in self._services:
            self._services["promotion"] = PromotionService(self.db, self.redis)
        return self._services["promotion"]
# ========== Helper pour obtenir les services dans les dépendances FastAPI ==========

async def get_service_factory(db_session, redis_client) -> ServiceFactory:
    """
    Fonction utilitaire pour obtenir la factory de services.
    À utiliser comme dépendance FastAPI.
    """
    return ServiceFactory(db_session, redis_client)

    