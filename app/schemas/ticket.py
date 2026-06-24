# app/schemas/ticket.py
"""Schémas pour les tickets (jeu sans compte) et les sessions de caisse"""

from decimal import Decimal

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from app.models.enums import TicketStatus


# ========== Création ==========
class TicketCreate(BaseModel):
    """Création d'un ticket"""
    amount: float = Field(..., gt=0, le=500000, description="Montant en HTG")
    player_name: Optional[str] = Field(None, max_length=100)
    player_phone: Optional[str] = Field(None, description="Pour notifications SMS")


class TicketRechargeRequest(BaseModel):
    """Demande de recharge de ticket"""
    amount: Decimal = Field(..., gt=0, le=500000)


# ========== Réponses ==========
class TicketResponse(BaseModel):
    """Réponse ticket"""
    id: str
    ticket_number: str
    balance: float
    initial_amount: float
    status: str
    expires_at: datetime
    created_at: datetime
    qr_code: Optional[str] = None  # Base64 QR code
    
    class Config:
        from_attributes = True


class TicketInfoResponse(BaseModel):
    """Informations détaillées d'un ticket"""
    id: str
    ticket_number: str
    player_name: Optional[str]
    player_phone: Optional[str]
    balance: float
    initial_amount: float
    status: str
    expires_at: datetime
    created_at: datetime
    paid_at: Optional[datetime] = None
    bureau_id: Optional[str] = None
    agent_id: Optional[str] = None
    recent_bets: Optional[List[dict]] = None
    recent_lucky_plays: Optional[List[dict]] = None
    
    class Config:
        from_attributes = True


# ========== Paris ==========
class TicketBetRequest(BaseModel):
    """Pari avec ticket"""
    ticket_number: str
    draw_id: str
    picks: List[int]
    stake: float


class TicketBetResponse(BaseModel):
    """Réponse pari ticket"""
    success: bool
    bet_id: str
    new_balance: float
    message: str


# ========== Paiement ==========
class TicketPayoutRequest(BaseModel):
    """Paiement ticket"""
    ticket_number: str


class TicketPayoutResponse(BaseModel):
    """Réponse paiement ticket"""
    success: bool
    amount: float
    ticket_number: str
    message: str


# ========== Sessions de caisse ==========
class CashierSessionCreate(BaseModel):
    """Ouverture d'une session de caisse"""
    bureau_id: str = Field(..., description="ID du bureau")
    starting_balance: float = Field(..., ge=0, description="Solde initial en HTG")


class CashierSessionClose(BaseModel):
    """Fermeture d'une session de caisse"""
    actual_balance: float = Field(..., ge=0, description="Solde réel en caisse")
    difference_reason: Optional[str] = Field(None, description="Raison de l'écart éventuel")


class CashierSessionResponse(BaseModel):
    """Réponse session de caisse"""
    id: str
    bureau_id: str
    agent_id: str
    starting_balance: float
    current_balance: float
    expected_balance: float
    cash_in_count: int
    cash_in_amount: float
    cash_out_count: int
    cash_out_amount: float
    status: str
    opened_at: datetime
    closed_at: Optional[datetime]
    difference: float
    difference_reason: Optional[str]
    
    class Config:
        from_attributes = True


# ========== Listes ==========
class TicketListResponse(BaseModel):
    """Liste des tickets"""
    items: List[TicketResponse]
    total: int
    page: int
    page_size: int
    total_balance: float


# ========== Recherche ==========
class TicketSearchRequest(BaseModel):
    """Recherche de tickets"""
    ticket_number: Optional[str] = None
    player_name: Optional[str] = None
    player_phone: Optional[str] = None
    bureau_id: Optional[str] = None
    agent_id: Optional[str] = None
    status: Optional[TicketStatus] = None
    min_amount: Optional[Decimal] = None
    max_amount: Optional[Decimal] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=20, ge=1, le=100)


# ========== Statistiques ==========
class TicketStatisticsResponse(BaseModel):
    """Statistiques des tickets"""
    period: str
    start_date: datetime
    end_date: datetime
    created: dict
    paid: dict
    expired: dict
    active: dict
    conversion_rate: float


# ========== Transactions ==========
class TicketTransactionResponse(BaseModel):
    """Transaction d'un ticket"""
    id: str
    type: str
    amount: float
    balance_after: Optional[float]
    status: str
    created_at: datetime
    metadata: Optional[dict]


# ========== QR Code ==========
class TicketQRResponse(BaseModel):
    """QR Code d'un ticket"""
    ticket_number: str
    qr_code: str  # Base64
    expires_at: datetime