# app/schemas/lucky6.py
"""Schémas Lucky6 (entrées). Ni cote, ni résultat, ni gain ne sont acceptés
du navigateur : tout est recalculé par le serveur."""

from decimal import Decimal
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Lucky6BetType = Literal["SIX", "FIRST_ODD", "FIRST_EVEN"]


class AgentLucky6BetCreate(BaseModel):
    """Pari pris au bureau par un agent, pour un compte joueur ou un ticket."""
    model_config = ConfigDict(extra="forbid")

    race_id: str = Field(..., min_length=1, max_length=40)
    bet_type: Lucky6BetType
    selection: List[int] = Field(default_factory=list, max_length=6)
    stake: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    # cash : paiement en espèces, le numéro de ticket est créé automatiquement
    player_type: Literal["cash", "account", "ticket"] = "cash"
    identifier: str = Field("", max_length=40, description="Téléphone du joueur ou numéro de ticket (inutile en espèces)")
    player_name: Optional[str] = Field(None, max_length=100)


class Lucky6ConfigUpdate(BaseModel):
    """Configuration complète (validée en détail par lucky6_engine.validate_config)."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    auto_enabled: bool
    interval_minutes: int
    betting_close_seconds: int
    intro_ms: int
    ball_interval_ms: int
    outro_ms: int
    open_hour: int
    close_hour: int
    min_bet: float
    max_bet: float
    stake_options: List[float]
    max_payout: float
    max_rtp: float
    paytable: List[float]
    parity_enabled: bool
    parity_odds: float


class Lucky6PaytablePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paytable: List[float]
    target_rtp: Optional[float] = Field(None, gt=0, le=1)


class Lucky6RoundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open_betting: bool = True
    minutes: Optional[int] = Field(None, ge=1, le=240, description="Départ dans N minutes (sinon prochain créneau)")


class Lucky6Cancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(..., min_length=3, max_length=200)
