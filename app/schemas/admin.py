# app/schemas/admin.py
"""Schémas Pydantic pour l'administration - Parier Keno & Lucky Haïti"""

from pydantic import BaseModel, Field, validator, EmailStr, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from decimal import Decimal
import re

from app.models.enums import UserRole, KYCStatus, TransactionType, TransactionStatus, PromotionType, PromotionStatus
from app.models.keno import KenoDrawStatus, KenoBetStatus


# ==================== AUTHENTIFICATION ====================

class AdminLogin(BaseModel):
    """Connexion administrateur"""
    email: EmailStr
    password: str = Field(..., min_length=6)
    remember: bool = False


class AdminTokenResponse(BaseModel):
    """Réponse token admin"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


# ==================== UTILISATEURS ====================

class AdminUserBase(BaseModel):
    """Base utilisateur admin"""
    phone: str = Field(..., description="Numéro de téléphone 8 chiffres")
    email: Optional[EmailStr] = None
    first_name: Optional[str] = Field(None, max_length=50)
    last_name: Optional[str] = Field(None, max_length=50)
    national_id: Optional[str] = Field(None, max_length=20)
    role: UserRole = UserRole.PLAYER
    
    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        """Valide le format du téléphone haïtien"""
        v = v.replace(' ', '').replace('-', '')
        if not re.match(r'^[34]\d{7}$', v):
            raise ValueError('Téléphone invalide. Format: 3X-XX-XXXX ou 4X-XX-XXXX')
        return v


class AdminUserCreate(AdminUserBase):
    """Création utilisateur par admin"""
    password: str = Field(..., min_length=8, description="Mot de passe (min 8 caractères)")
    kyc_verified: bool = False
    
    @field_validator('password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Valide la complexité du mot de passe"""
        if not re.search(r'[A-Z]', v):
            raise ValueError('Le mot de passe doit contenir au moins une majuscule')
        if not re.search(r'[a-z]', v):
            raise ValueError('Le mot de passe doit contenir au moins une minuscule')
        if not re.search(r'\d', v):
            raise ValueError('Le mot de passe doit contenir au moins un chiffre')
        return v


class AdminUserUpdate(BaseModel):
    """Mise à jour utilisateur par admin"""
    first_name: Optional[str] = Field(None, max_length=50)
    last_name: Optional[str] = Field(None, max_length=50)
    email: Optional[EmailStr] = None
    national_id: Optional[str] = Field(None, max_length=20)
    role: Optional[UserRole] = None
    kyc_status: Optional[KYCStatus] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=8)
    
    @field_validator('password')
    @classmethod
    def validate_password(cls, v: Optional[str]) -> Optional[str]:
        if v:
            if not re.search(r'[A-Z]', v):
                raise ValueError('Le mot de passe doit contenir au moins une majuscule')
            if not re.search(r'[a-z]', v):
                raise ValueError('Le mot de passe doit contenir au moins une minuscule')
            if not re.search(r'\d', v):
                raise ValueError('Le mot de passe doit contenir au moins un chiffre')
        return v


class AdminUserResponse(BaseModel):
    """Réponse utilisateur pour admin"""
    id: str
    phone: str
    email: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    full_name: Optional[str]
    national_id: Optional[str]
    role: str
    kyc_status: str
    is_active: bool
    is_locked: bool
    wallet_balance: float
    total_bets_count: int
    total_bets_amount: float
    total_wins: float
    created_at: datetime
    last_login: Optional[datetime]
    
    class Config:
        from_attributes = True


# ==================== AGENTS ====================

class AdminAgentCreate(BaseModel):
    """Création d'un agent"""
    phone: str = Field(..., description="Numéro de téléphone 8 chiffres")
    email: Optional[EmailStr] = None
    first_name: Optional[str] = Field(None, max_length=50)
    last_name: Optional[str] = Field(None, max_length=50)
    national_id: Optional[str] = Field(None, max_length=20)
    password: str = Field(..., min_length=8)
    bureau_id: Optional[str] = None
    
    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        v = v.replace(' ', '').replace('-', '')
        if not re.match(r'^[34]\d{7}$', v):
            raise ValueError('Téléphone invalide. Format: 3X-XX-XXXX ou 4X-XX-XXXX')
        return v


class AdminAgentUpdate(BaseModel):
    """Mise à jour d'un agent"""
    first_name: Optional[str] = Field(None, max_length=50)
    last_name: Optional[str] = Field(None, max_length=50)
    email: Optional[EmailStr] = None
    national_id: Optional[str] = Field(None, max_length=20)
    bureau_id: Optional[str] = None
    password: Optional[str] = Field(None, min_length=8)
    is_active: Optional[bool] = None


# ==================== BUREAUX ====================

class AdminBureauBase(BaseModel):
    """Base bureau"""
    name: str = Field(..., max_length=100)
    code: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[EmailStr] = None


class AdminBureauCreate(AdminBureauBase):
    """Création d'un bureau"""
    pass


class AdminBureauUpdate(BaseModel):
    """Mise à jour d'un bureau"""
    name: Optional[str] = Field(None, max_length=100)
    code: Optional[str] = Field(None, max_length=20)
    address: Optional[str] = Field(None, max_length=200)
    city: Optional[str] = Field(None, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None


# ==================== CONFIGURATION KENO ====================

class AdminKenoConfig(BaseModel):
    """Configuration du jeu Keno"""
    draw_interval: int = Field(5, ge=1, le=60, description="Intervalle entre les tirages (minutes)")
    start_hour: int = Field(8, ge=0, le=23, description="Heure de début des tirages")
    end_hour: int = Field(23, ge=0, le=23, description="Heure de fin des tirages")
    min_bet: int = Field(10, ge=1, description="Mise minimum (HTG)")
    max_bet: int = Field(100000, ge=10, description="Mise maximum (HTG)")
    
    @field_validator('end_hour')
    @classmethod
    def validate_hours(cls, v: int, info) -> int:
        if 'start_hour' in info.data and v <= info.data['start_hour']:
            raise ValueError('L\'heure de fin doit être après l\'heure de début')
        return v


class AdminKenoPaytableUpdate(BaseModel):
    """Mise à jour de la table de paiement Keno"""
    paytable: Dict[int, Dict[int, float]] = Field(..., description="Table de paiement {picks: {hits: multiplier}}")
    
    @field_validator('paytable')
    @classmethod
    def validate_paytable(cls, v: Dict) -> Dict:
        """Valide la table de paiement"""
        for picks, hits_map in v.items():
            if not 1 <= int(picks) <= 10:
                raise ValueError(f'Nombre de numéros invalide: {picks}')
            for hits, multiplier in hits_map.items():
                if int(hits) > int(picks):
                    raise ValueError(f'Les hits ({hits}) ne peuvent pas dépasser les picks ({picks})')
                if multiplier < 0:
                    raise ValueError(f'Le multiplicateur doit être positif: {multiplier}')
        return v


# ==================== CONFIGURATION LUCKY ====================

class LuckyWheelSegment(BaseModel):
    """Segment de la roue de la chance"""
    label: str = Field(..., description="Nom du segment (ex: 'x10')")
    multiplier: float = Field(..., ge=0, description="Multiplicateur")
    weight: int = Field(..., ge=1, description="Poids (probabilité)")
    color: str = Field(..., description="Couleur hexadécimale (ex: '#44AAFF')")
    
    @field_validator('color')
    @classmethod
    def validate_color(cls, v: str) -> str:
        if not re.match(r'^#[0-9A-Fa-f]{6}$', v):
            raise ValueError('Couleur invalide. Format: #RRGGBB')
        return v


class AdminLuckyConfig(BaseModel):
    """Configuration du jeu Lucky Wheel"""
    name: str = Field(..., max_length=50)
    description: Optional[str] = Field(None, max_length=200)
    segments: List[LuckyWheelSegment] = Field(..., min_items=5, max_items=20)
    min_bet: Decimal = Field(10, ge=1)
    max_bet: Decimal = Field(10000, ge=10)
    
    @field_validator('segments')
    @classmethod
    def validate_segments(cls, v: List) -> List:
        """Valide les segments"""
        if not v:
            raise ValueError('La roue doit avoir au moins 5 segments')
        total_weight = sum(s.weight for s in v)
        if total_weight == 0:
            raise ValueError('Le poids total des segments doit être > 0')
        # Vérifier les labels uniques
        labels = [s.label for s in v]
        if len(labels) != len(set(labels)):
            raise ValueError('Les labels des segments doivent être uniques')
        return v


# ==================== TRANSACTIONS ====================

class AdminTransactionFilter(BaseModel):
    """Filtres pour les transactions"""
    transaction_type: Optional[TransactionType] = None
    status: Optional[TransactionStatus] = None
    user_id: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    min_amount: Optional[Decimal] = None
    max_amount: Optional[Decimal] = None
    page: int = Field(1, ge=1)
    per_page: int = Field(20, ge=1, le=100)


# ==================== RAPPORTS ====================

class AdminReportRequest(BaseModel):
    """Demande de rapport"""
    start_date: datetime
    end_date: datetime
    report_type: str = Field(..., pattern="^(financial|game|compliance|users)$")
    format: str = Field("json", pattern="^(json|csv|excel|pdf)$")
    filters: Optional[Dict[str, Any]] = None


class AdminFinancialReport(BaseModel):
    """Rapport financier"""
    period: Dict[str, str] = Field(..., description="Période du rapport")
    summary: Dict[str, float] = Field(..., description="Résumé des transactions")
    daily_data: List[Dict[str, Any]] = Field(..., description="Données journalières")
    revenue_breakdown: Dict[str, float] = Field(..., description="Répartition des revenus")
    edge: float = Field(..., description="Marge globale")


class AdminGameReport(BaseModel):
    """Rapport de jeu"""
    period: Dict[str, str]
    keno: Dict[str, Any]
    lucky: Dict[str, Any]
    total_bets: int
    total_volume: float
    total_payout: float


class AdminComplianceReport(BaseModel):
    """Rapport de conformité LEH"""
    period: Dict[str, str]
    total_users: int
    kyc_status: Dict[str, int]
    total_transactions: int
    total_volume: float
    self_exclusions: int
    suspicious_activity: int


# ==================== PROMOTIONS ====================

class AdminPromotionBase(BaseModel):
    """Base promotion"""
    name: str = Field(..., max_length=100)
    code: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = Field(None, max_length=500)
    type: PromotionType
    config: Dict[str, Any] = Field(..., description="Configuration spécifique au type")
    start_date: datetime
    end_date: datetime
    min_deposit: Optional[Decimal] = None
    max_bonus: Optional[Decimal] = None
    wagering_requirement: int = Field(1, ge=1)
    eligible_games: List[str] = Field(["keno", "lucky"])
    new_users_only: bool = False
    first_deposit_only: bool = False
    total_budget: Optional[Decimal] = None


class AdminPromotionCreate(AdminPromotionBase):
    """Création d'une promotion"""
    pass


class AdminPromotionUpdate(BaseModel):
    """Mise à jour d'une promotion"""
    name: Optional[str] = Field(None, max_length=100)
    code: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = Field(None, max_length=500)
    config: Optional[Dict[str, Any]] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    min_deposit: Optional[Decimal] = None
    max_bonus: Optional[Decimal] = None
    wagering_requirement: Optional[int] = Field(None, ge=1)
    eligible_games: Optional[List[str]] = None
    new_users_only: Optional[bool] = None
    first_deposit_only: Optional[bool] = None
    total_budget: Optional[Decimal] = None
    status: Optional[PromotionStatus] = None


# ==================== AUDIT ====================

class AdminAuditFilter(BaseModel):
    """Filtres pour les logs d'audit"""
    action: Optional[str] = None
    user_id: Optional[str] = None
    resource_type: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    page: int = Field(1, ge=1)
    per_page: int = Field(50, ge=1, le=200)


class AdminAuditExportRequest(BaseModel):
    """Demande d'export des logs d'audit"""
    start_date: datetime
    end_date: datetime
    format: str = Field("csv", pattern="^(csv|json|excel)$")


# ==================== PARAMÈTRES GÉNÉRAUX ====================

class AdminSettings(BaseModel):
    """Paramètres généraux de l'application"""
    app_name: str = Field("Parier Keno Haïti", max_length=50)
    app_version: str = Field("1.0.0", max_length=10)
    timezone: str = Field("America/Port-au-Prince")
    currency: str = Field("HTG", max_length=3)
    
    # Limites globales
    max_daily_deposit: Decimal = Field(500000, ge=100)
    max_daily_withdrawal: Decimal = Field(200000, ge=100)
    max_single_bet: Decimal = Field(100000, ge=10)
    
    # Maintenance
    maintenance_mode: bool = False
    maintenance_message: Optional[str] = None
    
    # Conformité
    leh_api_enabled: bool = False
    leh_api_url: Optional[str] = None
    kyc_required_amount: Decimal = Field(10000, ge=0)


class AdminSecuritySettings(BaseModel):
    """Paramètres de sécurité"""
    two_factor_auth: bool = True
    session_timeout_minutes: int = Field(60, ge=15, le=1440)
    max_login_attempts: int = Field(5, ge=3, le=10)
    password_policy: Dict[str, Any] = {
        "min_length": 8,
        "require_uppercase": True,
        "require_lowercase": True,
        "require_numbers": True,
        "require_special": False
    }
    ip_whitelist: Optional[List[str]] = None
    rate_limit_requests: int = Field(100, ge=10)
    rate_limit_period: int = Field(60, ge=10)


# ==================== TABLEAU DE BORD ====================

class AdminDashboardStats(BaseModel):
    """Statistiques du tableau de bord"""
    users: Dict[str, Any]
    transactions: Dict[str, Any]
    games: Dict[str, Any]
    tickets: Dict[str, Any]
    bureaus: Dict[str, Any]
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class AdminDashboardCharts(BaseModel):
    """Données des graphiques du tableau de bord"""
    transactions: Dict[str, List[Any]]
    games: Dict[str, Any]
    period: int


# ==================== RÉPONSES ====================

class AdminApiResponse(BaseModel):
    """Réponse API standard pour l'admin"""
    success: bool
    message: str
    data: Optional[Any] = None
    errors: Optional[List[str]] = None


class AdminPaginatedResponse(BaseModel):
    """Réponse paginée pour l'admin"""
    items: List[Any]
    total: int
    page: int
    per_page: int
    pages: int
    has_next: bool
    has_prev: bool