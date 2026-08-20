# app/models/lucky.py
"""Modèles pour le jeu Lucky (Roue de la chance)"""

from datetime import datetime

from sqlalchemy import (
    Column, String, Numeric, Integer, DateTime, ForeignKey, 
    Enum, JSON, Boolean, CheckConstraint, Index, Float
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import LuckyGameType


class LuckyWheelConfig(BaseModel):
    """
    Configuration de la roue de la chance.
    Modifiable par l'administrateur.
    """
    __tablename__ = "lucky_wheel_configs"
    __table_args__ = (
        Index("idx_lucky_wheel_configs_is_active", "is_active"),
        Index("idx_lucky_wheel_configs_is_default", "is_default"),
    )
    
    # ========== Identification ==========
    name = Column(String(50), nullable=False)
    description = Column(String(200), nullable=True)
    
    # ========== Configuration des segments ==========
    # Format: [
    #   {"label": "x0", "multiplier": 0, "weight": 20, "color": "#FF4444"},
    #   {"label": "x2", "multiplier": 2, "weight": 15, "color": "#44FF44"}
    # ]
    segments = Column(JSON, nullable=False)
    
    # ========== Limites ==========
    min_bet = Column(Numeric(10, 2), default=10, nullable=False)
    max_bet = Column(Numeric(10, 2), default=10000, nullable=False)
    
    # ========== RTP théorique ==========
    theoretical_rtp = Column(Float, default=0.0, nullable=False)
    
    # ========== Statut ==========
    is_active = Column(Boolean, default=True, nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)
    
    # ========== Métadonnées ==========
    created_by = Column(String(36), nullable=True)
    
    # ========== Relations ==========
    plays = relationship("LuckyPlay", back_populates="wheel_config")
    
    # ========== Méthodes ==========
    def calculate_rtp(self) -> float:
        """Calcule le RTP théorique basé sur les poids et multiplicateurs"""
        total_weight = sum(s["weight"] for s in self.segments)
        if total_weight == 0:
            return 0.0
        rtp = sum(s["multiplier"] * s["weight"] for s in self.segments) / total_weight
        self.theoretical_rtp = round(rtp, 4)
        return self.theoretical_rtp
    
    @classmethod
    def get_default_config(cls) -> "LuckyWheelConfig":
        """Retourne la configuration par défaut de la roue"""
        return cls(
            name="Roue Classique",
            description="Roue de la chance standard",
            segments=[
                {"label": "Perdu", "multiplier": 0, "weight": 20, "color": "#FF4444"},
                {"label": "x0.5", "multiplier": 0.5, "weight": 12, "color": "#FF8888"},
                {"label": "x1", "multiplier": 1, "weight": 12, "color": "#FFAA44"},
                {"label": "x2", "multiplier": 2, "weight": 10, "color": "#FFCC44"},
                {"label": "x3", "multiplier": 3, "weight": 10, "color": "#88FF88"},
                {"label": "x5", "multiplier": 5, "weight": 8, "color": "#44FF44"},
                {"label": "x10", "multiplier": 10, "weight": 6, "color": "#44AAFF"},
                {"label": "x20", "multiplier": 20, "weight": 5, "color": "#8844FF"},
                {"label": "x50", "multiplier": 50, "weight": 3, "color": "#FF44FF"},
                {"label": "x100", "multiplier": 100, "weight": 2, "color": "#FF4444"},
                {"label": "JACKPOT", "multiplier": 500, "weight": 1, "color": "#FF0000"},
            ],
            min_bet=10,
            max_bet=10000,
            is_default=True
        )
    
    def __repr__(self) -> str:
        return f"<LuckyWheelConfig {self.name} rtp={self.theoretical_rtp}>"


class LuckyPlay(BaseModel):
    """
    Partie Lucky jouée par un utilisateur.
    Résultat INSTANTANÉ.
    """
    __tablename__ = "lucky_plays"
    __table_args__ = (
        CheckConstraint("stake > 0", name="ck_lucky_play_stake_positive"),
        Index("idx_lucky_plays_user_id", "user_id"),
        Index("idx_lucky_plays_ticket_id", "ticket_id"),
        Index("idx_lucky_plays_agent_id", "agent_id"),
        Index("idx_lucky_plays_played_at", "played_at"),
        Index("idx_lucky_plays_wheel_config_id", "wheel_config_id"),
    )
    
    # ========== Clés étrangères ==========
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    ticket_id = Column(String(36), ForeignKey("tickets.id"), nullable=True)
    agent_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    wheel_config_id = Column(String(36), ForeignKey("lucky_wheel_configs.id"), nullable=False)
    
    # ========== Type de jeu ==========
    game_type = Column(Enum(LuckyGameType), default=LuckyGameType.WHEEL, nullable=False)
    
    # ========== Le pari ==========
    stake = Column(Numeric(10, 2), nullable=False)
    
    # ========== Le résultat ==========
    result_segment = Column(JSON, nullable=False)  # {"label": "x10", "multiplier": 10, "color": "#44AAFF"}
    multiplier = Column(Numeric(5, 2), default=0, nullable=False)
    winnings = Column(Numeric(12, 2), default=0, nullable=False)
    
    # ========== Preuve d'équité ==========
    random_seed = Column(String(100), nullable=True)
    verification_hash = Column(String(200), nullable=True)
    
    # ========== Statut ==========
    status = Column(String(20), default="COMPLETED", nullable=False)
    
    # ========== Dates ==========
    played_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # ========== Relations ==========
    user = relationship("User", back_populates="lucky_plays", foreign_keys=[user_id])
    ticket = relationship("Ticket", back_populates="lucky_plays")
    wheel_config = relationship("LuckyWheelConfig", back_populates="plays")
    
    # ========== Méthodes ==========
    @staticmethod
    def spin_wheel(segments: list) -> dict:
        """
        Génère un résultat aléatoire basé sur les poids des segments.
        Utilise secrets pour un vrai aléatoire cryptographique.
        """
        import secrets
        
        # Calculer le poids total
        total_weight = sum(s["weight"] for s in segments)
        
        # Générer un nombre aléatoire
        roll = secrets.randbelow(int(total_weight * 100)) / 100
        
        # Trouver le segment gagnant
        cumulative = 0
        for segment in segments:
            cumulative += segment["weight"]
            if roll < cumulative:
                return {
                    "label": segment["label"],
                    "multiplier": segment["multiplier"],
                    "color": segment["color"]
                }
        
        # Fallback (ne devrait jamais arriver)
        return segments[0]
    
    def __repr__(self) -> str:
        return f"<LuckyPlay {self.id} stake={self.stake} winnings={self.winnings}>"