# app/schemas/ticket.py
"""Schémas pour les tickets (jeu sans compte)"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


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