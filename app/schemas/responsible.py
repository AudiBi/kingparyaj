# app/schemas/responsible.py
"""Schémas pour le jeu responsable"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class SelfExclusionRequest(BaseModel):
    """Demande d'auto-exclusion"""
    exclusion_type: str = Field(..., description="temporary, permanent, cooling_off")
    duration_days: Optional[int] = Field(None, description="Durée en jours (pour temporary)")
    reason: Optional[str] = None


class SelfExclusionResponse(BaseModel):
    """Réponse auto-exclusion"""
    id: str
    user_id: str
    exclusion_type: str
    start_date: datetime
    end_date: Optional[datetime]
    reason: str
    is_active: bool
    activated_at: datetime
    
    class Config:
        from_attributes = True


class SelfExclusionListResponse(BaseModel):
    """Liste des exclusions"""
    items: List[SelfExclusionResponse]
    total: int
    page: int
    page_size: int


class PlayerLimitRequest(BaseModel):
    """Demande de modification des limites"""
    daily_deposit_limit: Optional[float] = Field(None, ge=0)
    daily_loss_limit: Optional[float] = Field(None, ge=0)
    weekly_deposit_limit: Optional[float] = Field(None, ge=0)
    monthly_deposit_limit: Optional[float] = Field(None, ge=0)
    single_bet_limit: Optional[float] = Field(None, ge=0)


class PlayerLimitResponse(BaseModel):
    """Réponse limites joueur"""
    id: str
    user_id: str
    limit_type: str
    limit_amount: float
    is_active: bool
    set_at: datetime
    
    class Config:
        from_attributes = True


class ResponsibleGamblingStatus(BaseModel):
    """Statut jeu responsable"""
    is_self_excluded: bool
    self_exclusion_end_date: Optional[datetime]
    active_limits: List[PlayerLimitResponse]
    total_losses_24h: float
    total_bets_24h: int
    warnings_sent: int
    cooling_off_active: bool