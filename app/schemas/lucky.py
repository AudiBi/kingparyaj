# app/schemas/lucky.py
"""Schémas pour le jeu Lucky (Roue de la chance)"""

from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict, Any
from datetime import datetime


# ========== Configuration Roue ==========
class WheelSegment(BaseModel):
    """Segment de la roue"""
    label: str
    multiplier: float
    weight: int
    color: str


class LuckyWheelConfigResponse(BaseModel):
    """Configuration de la roue"""
    id: str
    name: str
    description: Optional[str]
    segments: List[WheelSegment]
    min_bet: float
    max_bet: float
    theoretical_rtp: float
    is_active: bool
    
    class Config:
        from_attributes = True


class LuckyWheelConfigUpdate(BaseModel):
    """Mise à jour configuration roue"""
    name: Optional[str] = None
    segments: Optional[List[WheelSegment]] = None
    min_bet: Optional[float] = None
    max_bet: Optional[float] = None
    is_active: Optional[bool] = None


# ========== Jeu ==========
class LuckySpinRequest(BaseModel):
    """Requête tour de roue (joueur connecté)"""
    stake: float = Field(..., gt=0, le=10000, description="Mise en HTG")
    
    @field_validator("stake")
    @classmethod
    def validate_stake(cls, v: float) -> float:
        if v < 10:
            raise ValueError("La mise minimum est de 10 HTG")
        if v > 10000:
            raise ValueError("La mise maximum est de 10 000 HTG")
        return v


class LuckySpinTicketRequest(BaseModel):
    """Requête tour de roue avec ticket (bureau)"""
    ticket_number: str
    stake: float


class LuckySpinResponse(BaseModel):
    """Réponse tour de roue"""
    success: bool
    segment: str
    multiplier: float
    winnings: float
    color: str
    play_id: str
    verification_hash: str
    new_balance: float
    message: str


# ========== Historique ==========
class LuckyPlayHistoryResponse(BaseModel):
    """Historique des parties Lucky"""
    id: str
    stake: float
    multiplier: float
    winnings: float
    segment: str
    played_at: datetime
    
    class Config:
        from_attributes = True


# ========== Statistiques ==========
class LuckyStatsResponse(BaseModel):
    """Statistiques Lucky (admin/global)"""
    total_plays: int
    total_stake: float
    total_winnings: float
    win_rate: float
    best_win: float
    best_multiplier: float
    segment_distribution: Dict[str, int]
    theoretical_rtp: float
    actual_rtp: float


class LuckyStatisticsResponse(BaseModel):
    """Statistiques Lucky (joueur)"""
    total_plays: int
    total_stake: float
    total_winnings: float
    win_rate: float
    best_win: float
    segment_distribution: Dict[str, int]  # {"x10": 50, "x2": 100}
    theoretical_rtp: float
    actual_rtp: float


# ========== Vérification ==========
class LuckyVerifyResponse(BaseModel):
    """Vérification d'équité"""
    play_id: str
    is_valid: bool
    random_seed: str
    expected_hash: str
    actual_hash: str
    verified_at: datetime


# ========== Admin ==========
class LuckyGlobalStatsResponse(BaseModel):
    """Statistiques globales Lucky (admin)"""
    total_plays: int
    total_volume: float
    total_payout: float
    house_edge: float
    rtp: float
    popular_segments: List[Dict[str, Any]]
    segment_distribution: Dict[str, int]