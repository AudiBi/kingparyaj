# app/schemas/bureau.py
"""Schémas pour les bureaux et caisses"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime


class BureauCreate(BaseModel):
    """Création d'un bureau"""
    name: str = Field(..., max_length=100)
    code: str = Field(..., max_length=20)
    address: Optional[str] = None
    city: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None


class BureauUpdate(BaseModel):
    """Mise à jour d'un bureau"""
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    opening_hours: Optional[Dict] = None


class BureauResponse(BaseModel):
    """Réponse bureau"""
    id: str
    name: str
    code: str
    address: Optional[str]
    city: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    cash_balance: float
    safe_balance: float
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


# ========== Sessions de caisse ==========
class CashierSessionOpen(BaseModel):
    """Ouverture session caisse"""
    starting_balance: float = Field(..., ge=0)


class CashierSessionClose(BaseModel):
    """Fermeture session caisse"""
    actual_balance: float = Field(..., ge=0)
    difference_reason: Optional[str] = None


class CashierSessionResponse(BaseModel):
    """Réponse session caisse"""
    id: str
    bureau_id: str
    bureau_name: str
    agent_id: str
    agent_name: str
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


class CashierStatsResponse(BaseModel):
    """Statistiques caisse"""
    total_cash_in: float
    total_cash_out: float
    total_bets: int
    current_cash_balance: float
    today_cash_in: float
    today_cash_out: float
    today_bets: int
    active_sessions: int