# app/schemas/ticket.py
"""Schémas pour les tickets (jeu sans compte)"""

from decimal import Decimal

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from app.models.enums import TicketStatus


class TicketCreate(BaseModel):
    """Création d'un ticket"""
    amount: float = Field(..., gt=0, le=500000, description="Montant en HTG")
    player_name: Optional[str] = Field(None, max_length=100)
    player_phone: Optional[str] = Field(None, description="Pour notifications SMS")


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


class TicketPayoutRequest(BaseModel):
    """Paiement ticket"""
    ticket_number: str


class TicketPayoutResponse(BaseModel):
    """Réponse paiement ticket"""
    success: bool
    amount: float
    ticket_number: str
    message: str


class TicketListResponse(BaseModel):
    """Liste des tickets"""
    items: List[TicketResponse]
    total: int
    page: int
    page_size: int
    total_balance: float

class TicketRechargeRequest(BaseModel):
    '''Demande de recharge de ticket'''
    amount: Decimal = Field(..., gt=0, le=500000)

class TicketSearchRequest(BaseModel):
    '''Recherche de tickets'''
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

class TicketStatisticsResponse(BaseModel):
    '''Statistiques des tickets'''
    period: str
    start_date: datetime
    end_date: datetime
    created: dict
    paid: dict
    expired: dict
    active: dict
    conversion_rate: float

class TicketTransactionResponse(BaseModel):
    '''Transaction d'un ticket'''
    id: str
    type: str
    amount: float
    balance_after: Optional[float]
    status: str
    created_at: datetime
    metadata: Optional[dict]
