# app/models/keno.py
"""Modèles pour le jeu Keno (80 numéros, 20 tirés)"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Column, String, Numeric, Integer, DateTime, ForeignKey, 
    Enum, ARRAY, Boolean, CheckConstraint, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import KenoDrawStatus, KenoBetStatus


class KenoDraw(BaseModel):
    """
    Tirage Keno.
    Génère 20 numéros gagnants parmi 1-80.
    """
    __tablename__ = "keno_draws"
    __table_args__ = (
        Index("idx_keno_draws_draw_number", "draw_number", unique=True),
        Index("idx_keno_draws_draw_time", "draw_time"),
        Index("idx_keno_draws_status", "status"),
    )
    
    # ========== Identification ==========
    draw_number = Column(Integer, unique=True, nullable=False, index=True)
    
    # ========== Planning ==========
    draw_time = Column(DateTime, nullable=False)
    
    # ========== Résultat ==========
    numbers = Column(ARRAY(Integer), nullable=True)  # Les 20 numéros tirés
    
    # ========== Statut ==========
    status = Column(Enum(KenoDrawStatus), default=KenoDrawStatus.PENDING, nullable=False)
    
    # ========== Métriques ==========
    total_bets = Column(Integer, default=0, nullable=False)
    total_amount = Column(Numeric(12, 2), default=0, nullable=False)
    total_payout = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Jackpot progressif ==========
    jackpot_amount = Column(Numeric(12, 2), default=0, nullable=False)
    jackpot_won = Column(Boolean, default=False, nullable=False)
    jackpot_winner_id = Column(String(36), nullable=True)
    
    # ========== Navigation (liens tirages) ==========
    previous_draw_id = Column(String(36), nullable=True)
    next_draw_id = Column(String(36), nullable=True)
    
    # ========== Métadonnées ==========
    closed_at = Column(DateTime, nullable=True)
    closed_by = Column(String(36), nullable=True)
    
    # ========== Relations ==========
    bets = relationship("KenoBet", back_populates="draw", cascade="all, delete-orphan")
    
    # ========== Méthodes ==========
    def has_numbers(self) -> bool:
        """Vérifie si les numéros ont été tirés"""
        return self.numbers is not None and len(self.numbers) == 20
    
    def __repr__(self) -> str:
        return f"<KenoDraw #{self.draw_number} status={self.status}>"


class KenoBet(BaseModel):
    """
    Pari Keno placé par un joueur.
    """
    __tablename__ = "keno_bets"
    __table_args__ = (
        CheckConstraint("stake > 0", name="ck_keno_bet_stake_positive"),
        CheckConstraint("stake <= 100000", name="ck_keno_bet_stake_max"),
        CheckConstraint("array_length(picks, 1) >= 1", name="ck_keno_bet_picks_min"),
        CheckConstraint("array_length(picks, 1) <= 10", name="ck_keno_bet_picks_max"),
        Index("idx_keno_bets_user_id", "user_id"),
        Index("idx_keno_bets_draw_id", "draw_id"),
        Index("idx_keno_bets_ticket_id", "ticket_id"),
        Index("idx_keno_bets_agent_id", "agent_id"),
        Index("idx_keno_bets_status", "status"),
        Index("idx_keno_bets_placed_at", "placed_at"),
    )
    
    # ========== Clés étrangères ==========
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    ticket_id = Column(String(36), ForeignKey("tickets.id"), nullable=True)
    draw_id = Column(String(36), ForeignKey("keno_draws.id"), nullable=False)
    agent_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    
    # ========== Contenu du pari ==========
    picks = Column(ARRAY(Integer), nullable=False)  # Numéros choisis (1-80)
    stake = Column(Numeric(10, 2), nullable=False)
    
    # ========== Résultat ==========
    hits = Column(Integer, default=0, nullable=False)
    multiplier = Column(Numeric(5, 2), default=0, nullable=False)
    winnings = Column(Numeric(10, 2), default=0, nullable=False)
    
    # ========== Jackpot ==========
    jackpot_win = Column(Boolean, default=False, nullable=False)
    jackpot_amount = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Statut ==========
    status = Column(Enum(KenoBetStatus), default=KenoBetStatus.PENDING, nullable=False)
    
    # ========== Bonus ==========
    bonus_multiplier = Column(Numeric(3, 2), default=1, nullable=False)
    
    # ========== Dates ==========
    placed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    settled_at = Column(DateTime, nullable=True)
    
    # ========== Relations ==========
    user = relationship("User", back_populates="keno_bets", foreign_keys=[user_id])
    draw = relationship("KenoDraw", back_populates="bets")
    ticket = relationship("Ticket", back_populates="keno_bets")
    agent = relationship("User", foreign_keys=[agent_id])
    
    # ========== Méthodes ==========
    def calculate_winnings(self, draw_numbers: list) -> tuple:
        """Calcule les gains basés sur les numéros tirés"""
        self.hits = len(set(self.picks) & set(draw_numbers))
        
        # Table de paiement standard
        paytable = {
            1: {1: 2.5},
            2: {2: 6},
            3: {3: 12, 2: 1.5},
            4: {4: 30, 3: 3, 2: 1},
            5: {5: 60, 4: 6, 3: 2, 2: 0.5},
            6: {6: 120, 5: 15, 4: 4, 3: 1.5, 2: 0.5},
            7: {7: 300, 6: 30, 5: 8, 4: 2, 3: 1, 2: 0.5},
            8: {8: 600, 7: 60, 6: 15, 5: 4, 4: 1.5, 3: 0.5},
            9: {9: 1200, 8: 120, 7: 30, 6: 8, 5: 3, 4: 1},
            10: {10: 5000, 9: 500, 8: 60, 7: 15, 6: 5, 5: 2, 4: 0.5}
        }
        
        picks_count = len(self.picks)
        self.multiplier = paytable.get(picks_count, {}).get(self.hits, 0)
        self.winnings = self.stake * Decimal(str(self.multiplier)) * self.bonus_multiplier
        
        return self.winnings, self.hits
    
    def __repr__(self) -> str:
        return f"<KenoBet {self.id} stake={self.stake} status={self.status}>"