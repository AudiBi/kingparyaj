# app/schemas/transaction.py
"""Schémas pour les transactions financières"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class TransactionFilter(BaseModel):
    """Filtres pour les transactions"""
    transaction_type: Optional[str] = None
    payment_method: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    min_amount: Optional[float] = None
    max_amount: Optional[float] = None


class TransactionResponse(BaseModel):
    """Réponse transaction"""
    id: str
    reference: str
    user_id: str
    transaction_type: str
    payment_method: Optional[str]
    amount: float
    fee: float
    bonus_amount: float
    balance_before: float
    balance_after: float
    status: str
    bet_id: Optional[str]
    draw_id: Optional[str]
    ticket_id: Optional[str]
    external_reference: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]
    
    class Config:
        from_attributes = True


class TransactionListResponse(BaseModel):
    """Liste des transactions"""
    items: List[TransactionResponse]
    total: int
    page: int
    page_size: int
    total_amount: float
    total_deposits: float
    total_withdrawals: float
    total_bets: float
    total_wins: float