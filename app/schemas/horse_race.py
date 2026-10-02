# app/schemas/horse_race.py
"""Schémas Horse Races (entrées API). Les cotes ne sont JAMAIS acceptées en
entrée : elles sont recalculées et figées par le serveur."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

BetType = Literal["WIN", "PLACE", "EXACTA", "TRIFECTA"]


class HorseRaceBetCreate(BaseModel):
    """Pari d'un joueur connecté (API)."""
    model_config = ConfigDict(extra="forbid")  # refuse "odds", "result"… envoyés par le client

    race_id: str
    bet_type: BetType
    selection: List[int] = Field(..., min_length=1, max_length=5, description="Numéros de joueurs, dans l'ordre pour EXACTA/TRIFECTA")
    stake: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)


class AgentHorseRaceBetCreate(HorseRaceBetCreate):
    """Pari placé au bureau par un agent, pour un compte ou un ticket."""
    # cash : paiement en espèces, le numéro de ticket est créé automatiquement
    player_type: Literal["cash", "account", "ticket"] = "cash"
    identifier: str = Field("", max_length=40, description="Téléphone du joueur ou numéro de ticket (inutile en espèces)")
    player_name: Optional[str] = Field(None, max_length=100)


class HorseRaceQuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    race_id: str
    bet_type: BetType
    selection: List[int] = Field(..., min_length=1, max_length=5)


class HorseRaceCreate(BaseModel):
    """Création manuelle d'une course (admin)."""
    model_config = ConfigDict(extra="forbid")

    scheduled_at: Optional[datetime] = Field(None, description="Départ (UTC). Par défaut : maintenant + intervalle")
    open_betting: bool = True


class HorseRaceCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=3, max_length=200)


class HorseRaceConfigUpdate(BaseModel):
    """Configuration du jeu (admin) : validée en détail par le service."""
    runners: List[Dict[str, Any]]
    margin: float = 0.15
    place_positions: int = 1
    min_bet: float = 10
    max_bet: float = 10000
    auto_enabled: bool = True
    interval_minutes: int = 5
    betting_close_seconds: int = 15
    race_duration_ms: int = 25000
    open_hour: int = 8
    close_hour: int = 23
    strength_variation: float = 0.15
    odds_min: float = 4.14
    odds_max: float = 7.79
