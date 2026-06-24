# app/schemas/keno.py
"""Schémas pour le jeu Keno"""

from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Dict
from datetime import datetime
from decimal import Decimal


# ========== Tirages ==========
class KenoDrawResponse(BaseModel):
    """Réponse tirage Keno"""
    id: str
    draw_number: int
    draw_time: datetime
    numbers: Optional[List[int]] = None
    status: str
    total_bets: int
    total_amount: float
    total_payout: float
    jackpot_amount: float
    jackpot_won: bool
    
    class Config:
        from_attributes = True


class KenoDrawListResponse(BaseModel):
    """Liste des tirages"""
    items: List[KenoDrawResponse]
    total: int
    page: int
    page_size: int
    next_draw_time: Optional[datetime] = None


# ========== Paris ==========
class KenoBetCreate(BaseModel):
    """Création d'un pari Keno (joueur connecté)"""
    draw_id: str = Field(..., description="ID du tirage")
    picks: List[int] = Field(..., description="Numéros choisis (1-80)", min_length=1, max_length=10)
    stake: float = Field(..., gt=0, le=100000, description="Mise en HTG")
    
    @field_validator("picks")
    @classmethod
    def validate_picks(cls, v: List[int]) -> List[int]:
        """Valide les numéros choisis"""
        if not all(1 <= pick <= 80 for pick in v):
            raise ValueError("Les numéros doivent être entre 1 et 80")
        if len(set(v)) != len(v):
            raise ValueError("Les numéros ne doivent pas se répéter")
        return v


class KenoBetTicketCreate(BaseModel):
    """Création d'un pari Keno avec ticket (bureau)"""
    ticket_number: str = Field(..., description="Numéro du ticket")
    draw_id: str
    picks: List[int]
    stake: float


class KenoBetResponse(BaseModel):
    """Réponse pari Keno"""
    id: str
    draw_id: str
    draw_number: Optional[int] = None
    picks: List[int]
    stake: float
    hits: int
    multiplier: float
    winnings: float
    jackpot_win: bool
    jackpot_amount: float
    status: str
    placed_at: datetime
    settled_at: Optional[datetime] = None
    
    class Config:
        from_attributes = True


class KenoBetListResponse(BaseModel):
    """Liste des paris"""
    items: List[KenoBetResponse]
    total: int
    page: int
    page_size: int
    total_stake: float
    total_winnings: float


# ========== Historique ==========
class KenoHistoryResponse(BaseModel):
    """Historique des paris Keno du joueur"""
    total_bets: int
    total_stake: float
    total_wins: float
    best_win: float
    best_multiplier: float
    recent_bets: List[KenoBetResponse]
    win_rate: float = Field(..., description="Taux de réussite en %")


# ========== Résultats ==========
class KenoResultResponse(BaseModel):
    """Résultat d'un tirage"""
    draw_id: str
    draw_number: int
    draw_time: datetime
    numbers: List[int]
    total_bets: int
    total_payout: float
    winners_count: int
    jackpot_won: bool
    jackpot_winner: Optional[str] = None
    your_bet: Optional[KenoBetResponse] = None


# ========== Statistiques ==========
class KenoStatsResponse(BaseModel):
    """Statistiques Keno (admin/global)"""
    total_draws: int
    total_bets: int
    total_volume: float
    total_payout: float
    house_edge: float
    popular_numbers: List[Dict[str, int]] = Field(default_factory=list)
    least_popular_numbers: List[Dict[str, int]] = Field(default_factory=list)
    rtp: float


class KenoStatisticsResponse(BaseModel):
    """Statistiques joueur Keno"""
    total_bets: int
    total_stake: float
    total_winnings: float
    win_rate: float
    best_win: float
    most_played_numbers: List[int]
    last_10_results: List[List[int]]
    hits_distribution: dict  # {"5": 10, "4": 25, ...}


# ========== Quick Pick ==========
class KenoQuickPickRequest(BaseModel):
    """Demande de Quick Pick (numéros aléatoires)"""
    numbers_count: int = Field(..., ge=1, le=10, description="Nombre de numéros à générer")
    draw_id: str = Field(..., description="ID du tirage")
    stake: float = Field(..., gt=0, le=100000, description="Mise en HTG")


class KenoQuickPickResponse(BaseModel):
    """Réponse Quick Pick"""
    picks: List[int]
    bet: KenoBetResponse


# ========== Stats globales ==========
class KenoGlobalStatsResponse(BaseModel):
    """Statistiques globales Keno (admin)"""
    total_draws: int
    total_bets: int
    total_volume: float
    total_payout: float
    house_edge: float
    popular_numbers: List[Dict[str, int]]  # [{"number": 7, "count": 150}, ...]
    least_popular_numbers: List[Dict[str, int]]
    rtp: float