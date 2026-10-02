# app/models/game.py
"""
Modèles génériques de jeux à manches : une manche (GameRound) et ses paris (GameBet).

Jeux qui les utilisent : Horse Races (game_type = "horse_races") et
Lucky6 (game_type = "lucky6"). Keno et Lucky Wheel gardent pour l'instant leurs propres tables ; ils pourront
migrer ici plus tard.

Choix techniques :
- Statuts et types de pari en String + CheckConstraint (pas d'enum PostgreSQL) :
  ajouter un type de pari (SHOW, QUINELLA…) = modifier la contrainte,
  pas d'ALTER TYPE ; compatible SQLite (tests).
- Participants, cotes, configuration et résultat en JSON, copiés dans la
  manche au moment de sa création : immuables pour l'audit et la vérification.
- La cote d'un pari est figée dans game_bets.odds à l'acceptation.
"""

import enum

from sqlalchemy import (
    Column, String, Integer, Numeric, DateTime, ForeignKey, JSON,
    CheckConstraint, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.models.base import BaseModel


class GameType(str, enum.Enum):
    HORSE_RACES = "horse_races"
    LUCKY6 = "lucky6"


class GameRoundStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    BETTING_OPEN = "BETTING_OPEN"
    BETTING_CLOSED = "BETTING_CLOSED"
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"


class GameBetType(str, enum.Enum):
    WIN = "WIN"
    PLACE = "PLACE"
    EXACTA = "EXACTA"
    TRIFECTA = "TRIFECTA"
    # Lucky6
    SIX = "SIX"                 # 6 numéros ; gain selon la position du 6e trouvé
    FIRST_ODD = "FIRST_ODD"     # 1re boule impaire
    FIRST_EVEN = "FIRST_EVEN"   # 1re boule paire


class GameBetStatus(str, enum.Enum):
    PENDING = "PENDING"
    WON = "WON"
    LOST = "LOST"
    VOID = "VOID"          # annulé sans remboursement nécessaire (ex. rejet avant débit)
    REFUNDED = "REFUNDED"  # mise remboursée (course annulée)


def _in(values) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


class GameRound(BaseModel):
    """Une manche d'un jeu (ex. une course Horse Races)."""

    __tablename__ = "game_rounds"
    __table_args__ = (
        UniqueConstraint("game_type", "round_number", name="uq_game_rounds_type_number"),
        CheckConstraint(f"status IN ({_in(GameRoundStatus)})", name="ck_game_rounds_status"),
        CheckConstraint("total_stake >= 0", name="ck_game_rounds_total_stake_positive"),
        CheckConstraint("total_payout >= 0", name="ck_game_rounds_total_payout_positive"),
        Index("idx_game_rounds_type_status", "game_type", "status"),
        Index("idx_game_rounds_scheduled_at", "scheduled_at"),
    )

    # ========== Identification ==========
    game_type = Column(String(30), nullable=False)
    round_number = Column(Integer, nullable=False)

    # ========== Cycle de vie ==========
    status = Column(String(20), default=GameRoundStatus.SCHEDULED.value, nullable=False)
    scheduled_at = Column(DateTime, nullable=False)          # départ prévu (UTC)
    betting_opens_at = Column(DateTime, nullable=True)
    betting_closes_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    settled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String(200), nullable=True)

    # ========== Contenu (copié à la création, immuable ensuite) ==========
    # [{"lane": 1, "player_number": 10, "player_name": "…", "probability": 0.25,
    #   "odds": {"WIN": 3.2, "PLACE": 1.4}, "assets": {"jersey": …, "rider": …, "horse": …}}]
    participants = Column(JSON, nullable=False)
    # {"place_positions": 2, "margin": 0.15, "min_bet": 10, "max_bet": 10000,
    #  "race_duration_ms": 25000, "bet_types": ["WIN", "PLACE", "EXACTA", "TRIFECTA"]}
    config = Column(JSON, nullable=False)

    # ========== Résultat ==========
    result = Column(JSON, nullable=True)         # [10, 7, 11, 9, 8, 6] : ordre d'arrivée
    race_script = Column(JSON, nullable=True)    # points de passage pour l'animation

    # ========== Preuve d'équité (commit / reveal) ==========
    server_seed_hash = Column(String(64), nullable=True)   # publié à l'ouverture des paris
    server_seed = Column(String(64), nullable=True)        # révélé après l'arrivée
    nonce = Column(Integer, default=0, nullable=False)

    # ========== Métriques ==========
    total_bets = Column(Integer, default=0, nullable=False)
    total_stake = Column(Numeric(14, 2), default=0, nullable=False)
    total_payout = Column(Numeric(14, 2), default=0, nullable=False)

    # ========== Relations ==========
    bets = relationship("GameBet", back_populates="round")

    def __repr__(self) -> str:
        return f"<GameRound {self.game_type} #{self.round_number} status={self.status}>"


class GameBet(BaseModel):
    """Un pari sur une manche. Financé par un compte (wallet) ou un ticket bureau."""

    __tablename__ = "game_bets"
    __table_args__ = (
        CheckConstraint("stake > 0", name="ck_game_bets_stake_positive"),
        CheckConstraint("odds >= 1", name="ck_game_bets_odds_min"),
        CheckConstraint("winnings >= 0", name="ck_game_bets_winnings_positive"),
        CheckConstraint("user_id IS NOT NULL OR ticket_id IS NOT NULL", name="ck_game_bets_funding_source"),
        CheckConstraint(f"bet_type IN ({_in(GameBetType)})", name="ck_game_bets_bet_type"),
        CheckConstraint(f"status IN ({_in(GameBetStatus)})", name="ck_game_bets_status"),
        Index("idx_game_bets_round_id", "round_id"),
        Index("idx_game_bets_user_id", "user_id"),
        Index("idx_game_bets_ticket_id", "ticket_id"),
        Index("idx_game_bets_agent_id", "agent_id"),
        Index("idx_game_bets_status", "status"),
        Index("idx_game_bets_placed_at", "placed_at"),
    )

    # ========== Clés étrangères ==========
    round_id = Column(String(36), ForeignKey("game_rounds.id"), nullable=False)
    game_type = Column(String(30), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    ticket_id = Column(String(36), ForeignKey("tickets.id"), nullable=True)
    agent_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    # ========== Le pari ==========
    bet_type = Column(String(20), nullable=False)
    selection = Column(JSON, nullable=False)          # [10] / [10, 7] / [10, 7, 11]
    stake = Column(Numeric(10, 2), nullable=False)
    odds = Column(Numeric(10, 2), nullable=False)     # figée à l'acceptation
    potential_payout = Column(Numeric(12, 2), nullable=False)

    # ========== Règlement ==========
    status = Column(String(20), default=GameBetStatus.PENDING.value, nullable=False)
    winnings = Column(Numeric(12, 2), default=0, nullable=False)
    placed_at = Column(DateTime, nullable=False)
    settled_at = Column(DateTime, nullable=True)
    debit_transaction_id = Column(String(36), ForeignKey("transactions.id"), nullable=True)
    payout_transaction_id = Column(String(36), ForeignKey("transactions.id"), nullable=True)

    # ========== Relations ==========
    round = relationship("GameRound", back_populates="bets")
    user = relationship("User", foreign_keys=[user_id])
    ticket = relationship("Ticket", foreign_keys=[ticket_id])
    agent = relationship("User", foreign_keys=[agent_id])

    def __repr__(self) -> str:
        return f"<GameBet {self.id} {self.bet_type} stake={self.stake} odds={self.odds} status={self.status}>"
