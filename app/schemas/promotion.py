# app/schemas/promotion.py
"""Schémas pour les promotions et bonus"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class PromotionCreate(BaseModel):
    """Création d'une promotion"""
    name: str = Field(..., max_length=100)
    code: Optional[str] = Field(None, max_length=50)
    description: Optional[str]
    type: str
    config: Dict[str, Any]
    start_date: datetime
    end_date: datetime
    min_deposit: Optional[float] = None
    max_bonus: Optional[float] = None
    wagering_requirement: int = 1
    eligible_games: List[str] = ["keno", "lucky"]
    total_budget: Optional[float] = None
    new_users_only: bool = False
    first_deposit_only: bool = False


class PromotionUpdate(BaseModel):
    """Mise à jour d'une promotion"""
    name: Optional[str] = None
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[str] = None
    is_active: Optional[bool] = None


class PromotionResponse(BaseModel):
    """Réponse promotion"""
    id: str
    name: str
    code: Optional[str]
    description: Optional[str]
    type: str
    config: Dict[str, Any]
    start_date: datetime
    end_date: datetime
    status: str
    is_active: bool
    total_budget: Optional[float]
    used_budget: float
    total_claims: int
    created_at: datetime
    
    class Config:
        from_attributes = True


class PromotionListResponse(BaseModel):
    """Liste des promotions"""
    items: List[PromotionResponse]
    total: int
    page: int
    page_size: int
    active_promotions: int


class ClaimPromotionRequest(BaseModel):
    """Demande de réclamation de promotion"""
    promotion_code: Optional[str] = None


class UserPromotionResponse(BaseModel):
    """Réponse promotion utilisateur"""
    id: str
    promotion_id: str
    promotion_name: str
    bonus_amount: float
    wagered_amount: float
    wagering_required: int
    is_completed: bool
    is_expired: bool
    claimed_at: datetime
    expires_at: datetime
    completed_at: Optional[datetime]
    
    class Config:
        from_attributes = True