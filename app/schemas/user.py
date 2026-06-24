# app/schemas/user.py
"""Schémas pour les utilisateurs et l'authentification"""

from pydantic import BaseModel, Field, field_validator, EmailStr
from typing import Optional, List
from datetime import datetime
from enum import Enum


class UserRole(str, Enum):
    PLAYER = "player"
    AGENT = "agent"
    MANAGER = "manager"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class KYCStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ========== Base ==========
class UserBase(BaseModel):
    """Informations de base de l'utilisateur"""
    email: Optional[EmailStr] = Field(default=None, description="Email")
    phone: str = Field(..., description="Numéro de téléphone", min_length=8, max_length=20)
    first_name: Optional[str] = Field(default=None, max_length=50)
    last_name: Optional[str] = Field(default=None, max_length=50)
    
    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        """Valide le format du téléphone haïtien"""
        v = v.replace(" ", "").replace("-", "")
        if not v.isdigit():
            raise ValueError("Le numéro de téléphone doit contenir uniquement des chiffres")
        if len(v) not in [8, 10, 11]:
            raise ValueError("Format invalide (8, 10 ou 11 chiffres)")
        return v


# ========== Création ==========
class UserCreate(UserBase):
    """Création d'un utilisateur"""
    password: str = Field(..., min_length=6, max_length=100, description="Mot de passe")
    national_id: Optional[str] = Field(default=None, max_length=20, description="Carte d'identité")
    referral_code: Optional[str] = Field(default=None, description="Code de parrainage")
    
    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Le mot de passe doit contenir au moins 6 caractères")
        return v


class UserUpdate(BaseModel):
    """Mise à jour d'un utilisateur"""
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None


# ========== Authentification ==========
class UserLogin(BaseModel):
    """Connexion utilisateur"""
    phone: str = Field(..., description="Numéro de téléphone")
    password: str = Field(..., description="Mot de passe")


class TokenResponse(BaseModel):
    """Réponse avec tokens JWT"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Expiration en secondes")


class UserLoginResponse(BaseModel):
    """Réponse complète de connexion"""
    user: "UserResponse"
    tokens: TokenResponse


class TokenRefresh(BaseModel):
    """Rafraîchissement du token"""
    refresh_token: str


# ========== VÉRIFICATION EMAIL ==========
class VerifyEmailRequest(BaseModel):
    """Vérification d'email"""
    code: str = Field(..., min_length=6, max_length=6, description="Code de vérification")


class ResendVerificationRequest(BaseModel):
    """Demande de renvoi de vérification"""
    email: EmailStr


# ========== RÉINITIALISATION MOT DE PASSE ==========
class ForgotPasswordRequest(BaseModel):
    """Demande de réinitialisation de mot de passe"""
    phone: str = Field(..., description="Numéro de téléphone")


class ResetPasswordRequest(BaseModel):
    """Réinitialisation du mot de passe avec code"""
    phone: str = Field(..., description="Numéro de téléphone")
    code: str = Field(..., min_length=6, max_length=6, description="Code de réinitialisation")
    new_password: str = Field(..., min_length=6, description="Nouveau mot de passe")


# ========== Réponses ==========
class UserResponse(UserBase):
    """Réponse utilisateur complète"""
    id: str
    role: UserRole
    is_active: bool
    is_locked: bool
    kyc_status: KYCStatus
    total_bets_count: int
    total_bets_amount: float
    total_wins: float
    last_login: Optional[datetime]
    created_at: datetime
    wallet_balance: Optional[float] = Field(default=None, description="Solde du portefeuille")
    bureau_id: Optional[str] = None
    referral_code: Optional[str] = None
    
    class Config:
        from_attributes = True


class UserProfileUpdate(BaseModel):
    """Mise à jour du profil utilisateur"""
    email: Optional[EmailStr] = None
    first_name: Optional[str] = Field(None, max_length=50)
    last_name: Optional[str] = Field(None, max_length=50)


# ========== KYC ==========
class KYCSubmission(BaseModel):
    """Soumission de documents KYC"""
    document_type: str = Field(..., description="Type de document (id_card, passport, etc.)")
    document_number: str = Field(..., description="Numéro du document")
    document_front_url: str = Field(..., description="URL recto")
    document_back_url: Optional[str] = Field(None, description="URL verso")
    selfie_url: Optional[str] = Field(None, description="URL selfie")


class KYCUpdate(BaseModel):
    """Mise à jour du statut KYC (admin)"""
    status: KYCStatus = Field(..., description="Nouveau statut KYC")
    national_id: Optional[str] = Field(None, description="Carte d'identité")
    documents: Optional[List[str]] = Field(None, description="URLs des documents")
    rejection_reason: Optional[str] = Field(None, description="Raison du rejet")


class KYCStatusUpdate(BaseModel):
    """Mise à jour du statut KYC (admin) - alias de KYCUpdate"""
    status: KYCStatus
    national_id: Optional[str] = None
    documents: Optional[List[str]] = None
    rejection_reason: Optional[str] = None


class KYCResponse(BaseModel):
    """Réponse KYC"""
    status: KYCStatus
    verified_at: Optional[datetime]
    submitted_at: datetime
    message: Optional[str] = None


# ========== Mot de passe ==========
class ChangePasswordRequest(BaseModel):
    """Changement de mot de passe"""
    current_password: str
    new_password: str = Field(..., min_length=6)
    confirm_password: str = Field(..., min_length=6)
    
    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        if len(v) < 6:
            raise ValueError("Le mot de passe doit contenir au moins 6 caractères")
        return v


# ========== Admin ==========
class UserAdminUpdate(BaseModel):
    """Mise à jour utilisateur par admin"""
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    is_locked: Optional[bool] = None
    kyc_status: Optional[KYCStatus] = None
    bureau_id: Optional[str] = None
    lock_reason: Optional[str] = None


class UserListResponse(BaseModel):
    """Liste des utilisateurs"""
    items: List[UserResponse]
    total: int
    page: int
    page_size: int
    pages: int
    has_next: bool
    has_prev: bool