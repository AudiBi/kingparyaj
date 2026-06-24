# app/schemas/wallet.py
"""Schémas pour le portefeuille et les transactions"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime
from decimal import Decimal
from enum import Enum


class WalletStatus(str, Enum):
    ACTIVE = "active"
    FROZEN = "frozen"
    CLOSED = "closed"


class PaymentMethod(str, Enum):
    MONCASH = "moncash"
    NATCASH = "natcash"
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    CRYPTO = "crypto"


# ========== Wallet ==========
class WalletResponse(BaseModel):
    """Réponse portefeuille"""
    id: str
    user_id: str
    balance: float = Field(..., description="Solde réel (retirable)")
    bonus_balance: float = Field(..., description="Solde bonus (non retirable)")
    total_balance: float = Field(..., description="Solde total")
    withdrawable_balance: float = Field(..., description="Solde retirable")
    total_deposited: float
    total_withdrawn: float
    total_won: float
    status: WalletStatus
    daily_deposit_limit: Optional[float] = None
    daily_loss_limit: Optional[float] = None
    single_bet_limit: Optional[float] = None
    
    class Config:
        from_attributes = True


class BalanceResponse(BaseModel):
    """Réponse simple de solde"""
    balance: float
    bonus_balance: float
    total_balance: float


# ========== Dépôt ==========
class DepositRequest(BaseModel):
    """Demande de dépôt"""
    amount: float = Field(..., gt=0, le=500000, description="Montant à déposer")
    payment_method: PaymentMethod
    phone: Optional[str] = Field(None, description="Numéro mobile money (si applicable)")
    
    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: float) -> float:
        if v < 50:
            raise ValueError("Le montant minimum est de 50 HTG")
        if v > 500000:
            raise ValueError("Le montant maximum est de 500 000 HTG")
        return v


class DepositResponse(BaseModel):
    """Réponse dépôt"""
    success: bool
    transaction_id: str
    reference: str
    amount: float
    new_balance: float
    payment_url: Optional[str] = None  # Pour redirection mobile money
    message: str


# ========== Retrait ==========
class WithdrawRequest(BaseModel):
    """Demande de retrait"""
    amount: float = Field(..., gt=0, le=50000, description="Montant à retirer")
    payment_method: PaymentMethod
    phone: Optional[str] = Field(None, description="Numéro mobile money")
    
    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: float) -> float:
        if v < 100:
            raise ValueError("Le montant minimum est de 100 HTG")
        if v > 50000:
            raise ValueError("Le montant maximum est de 50 000 HTG")
        return v


class WithdrawResponse(BaseModel):
    """Réponse retrait"""
    success: bool
    transaction_id: str
    reference: str
    amount: float
    new_balance: float
    message: str


# ========== Transactions ==========
class TransactionResponse(BaseModel):
    """Réponse transaction standard"""
    id: str
    reference: str
    transaction_type: str
    amount: float
    fee: float
    balance_before: float
    balance_after: float
    status: str
    payment_method: Optional[str] = None
    external_reference: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class WalletTransactionResponse(BaseModel):
    """Transaction pour le portefeuille (alias de TransactionResponse)"""
    id: str
    reference: str
    transaction_type: str
    amount: float
    fee: float
    balance_before: float
    balance_after: float
    status: str
    payment_method: Optional[str] = None
    external_reference: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


# ========== Limites ==========
class SetLimitRequest(BaseModel):
    """Définition d'une limite de jeu"""
    limit_type: str = Field(..., description="Type de limite (daily_deposit, daily_loss, weekly_deposit, monthly_deposit, single_bet)")
    limit_amount: Optional[float] = Field(None, gt=0, description="Montant de la limite (null pour supprimer)")


class LimitUpdateRequest(BaseModel):
    """Mise à jour des limites"""
    daily_deposit_limit: Optional[float] = Field(None, gt=0, le=1000000)
    daily_loss_limit: Optional[float] = Field(None, gt=0, le=500000)
    weekly_deposit_limit: Optional[float] = Field(None, gt=0, le=2000000)
    monthly_deposit_limit: Optional[float] = Field(None, gt=0, le=5000000)
    single_bet_limit: Optional[float] = Field(None, gt=0, le=100000)


class LimitResponse(BaseModel):
    """Réponse limites"""
    daily_deposit_limit: Optional[float] = None
    daily_loss_limit: Optional[float] = None
    weekly_deposit_limit: Optional[float] = None
    monthly_deposit_limit: Optional[float] = None
    single_bet_limit: Optional[float] = None
    today_deposits: float
    today_losses: float
    remaining_daily_deposit: Optional[float] = None
    remaining_daily_loss: Optional[float] = None


# ========== Transfert ==========
class TransferRequest(BaseModel):
    """Demande de transfert entre utilisateurs"""
    recipient_phone: str = Field(..., description="Téléphone du destinataire")
    amount: float = Field(..., gt=0, le=10000, description="Montant à transférer")
    description: Optional[str] = Field(None, max_length=200, description="Description du transfert")


class TransferResponse(BaseModel):
    """Réponse transfert"""
    success: bool
    transaction_id: str
    reference: str
    amount: float
    sender_balance: float
    recipient_phone: str
    message: str


# ========== Dépôt Mobile Money ==========
class MobileMoneyDepositRequest(BaseModel):
    """Dépôt via Mobile Money"""
    amount: float = Field(..., gt=0, le=500000)
    phone: str = Field(..., description="Numéro mobile money")
    provider: str = Field(..., description="moncash ou natcash")


class MobileMoneyDepositResponse(BaseModel):
    """Réponse dépôt Mobile Money"""
    success: bool
    transaction_id: str
    reference: str
    amount: float
    payment_url: Optional[str] = None
    message: str