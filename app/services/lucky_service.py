# app/services/lucky_service.py
"""Service pour le jeu Lucky (Roue de la chance)"""

import secrets
import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
import redis.asyncio as redis

from app.core.exceptions import AppException, GameException, InsufficientBalanceException
from app.core.logger import get_logger
from app.models.lucky import LuckyWheelConfig, LuckyPlay, LuckyGameType
from app.models.user import User
from app.models.ticket import Ticket
from app.services.base import BaseService
from app.services.wallet_service import WalletService
from app.services.audit_service import AuditService, AuditAction
from app.schemas.lucky import LuckySpinRequest, LuckySpinResponse


class LuckyWheelService(BaseService[LuckyWheelConfig, None, None]):
    """
    Service pour la Roue de la Chance.
    Résultat INSTANTANÉ - pas de tirage à attendre.
    """
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, LuckyWheelConfig)
        self.redis = redis_client
        self.wallet_service = WalletService(db, redis_client)
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("LuckyWheelService")
    
    # ========== Configuration ==========
    
    async def get_active_config(self) -> LuckyWheelConfig:
        """Récupère la configuration active de la roue"""
        # Vérifier le cache Redis
        cached = await self.redis.get("lucky:wheel:config")
        if cached:
            import json
            config_data = json.loads(cached)
            return LuckyWheelConfig(**config_data)
        
        # Requête base de données
        result = await self.db.execute(
            select(LuckyWheelConfig)
            .where(LuckyWheelConfig.is_active == True)
            .order_by(LuckyWheelConfig.is_default.desc())
        )
        config = result.scalar_one_or_none()
        
        if not config:
            config = LuckyWheelConfig.get_default_config()
            self.db.add(config)
            await self.db.flush()
        
        # Mettre en cache
        await self.redis.setex("lucky:wheel:config", 3600, config.to_json())
        
        return config
    
    async def update_config(
        self,
        config_id: str,
        segments: Optional[List[dict]] = None,
        min_bet: Optional[Decimal] = None,
        max_bet: Optional[Decimal] = None,
        is_active: Optional[bool] = None,
        user_id: str = None
    ) -> LuckyWheelConfig:
        """Met à jour la configuration de la roue"""
        config = await self.get_or_raise(config_id)
        
        if segments:
            config.segments = segments
            config.calculate_rtp()
        
        if min_bet is not None:
            config.min_bet = min_bet
        
        if max_bet is not None:
            config.max_bet = max_bet
        
        if is_active is not None:
            config.is_active = is_active
        
        config.updated_by = user_id
        config.updated_at = datetime.utcnow()
        
        await self.db.flush()
        
        # Invalider le cache
        await self.redis.delete("lucky:wheel:config")
        
        self.logger.info(f"Lucky wheel config updated: {config_id}")
        
        return config
    
    # ========== Jeu ==========
    
    async def spin(
        self,
        user_id: Optional[str],
        ticket_id: Optional[str],
        stake: Decimal,
        agent_id: Optional[str] = None,
        ip_address: str = None
    ) -> LuckySpinResponse:
        """
        Fait tourner la roue - résultat INSTANTANÉ
        """
        # 1. Valider la mise
        config = await self.get_active_config()
        
        if stake < config.min_bet or stake > config.max_bet:
            raise GameException(f"Mise invalide. Min: {config.min_bet}, Max: {config.max_bet}")
        
        # 2. Vérifier et débiter selon le type
        if user_id:
            # Vérifier les limites
            wallet = await self.wallet_service.get_by_user_id(user_id)
            if not wallet or wallet.balance < stake:
                raise InsufficientBalanceException(float(stake), float(wallet.balance if wallet else 0))
            
            # Débiter
            await self.wallet_service.debit(user_id, stake, "BET")
            
        elif ticket_id:
            # Vérifier le ticket
            ticket = await self.db.get(Ticket, ticket_id)
            if not ticket or ticket.balance < stake:
                raise GameException("Solde ticket insuffisant")
            
            ticket.balance -= stake
            await self.db.flush()
        
        else:
            raise GameException("User ID ou Ticket ID requis")
        
        # 3. Générer le résultat
        result = self._spin_wheel(config.segments)
        multiplier = Decimal(str(result["multiplier"]))
        winnings = stake * multiplier
        
        # 4. Générer les preuves d'équité
        random_seed = secrets.token_hex(32)
        verification_hash = hashlib.sha256(
            f"{random_seed}{stake}{datetime.utcnow().isoformat()}".encode()
        ).hexdigest()
        
        # 5. Créer l'enregistrement du jeu
        lucky_play = LuckyPlay(
            user_id=user_id,
            ticket_id=ticket_id,
            agent_id=agent_id,
            wheel_config_id=config.id,
            game_type=LuckyGameType.WHEEL,
            stake=stake,
            result_segment=result,
            multiplier=multiplier,
            winnings=winnings,
            random_seed=random_seed,
            verification_hash=verification_hash,
            played_at=datetime.utcnow(),
            status="COMPLETED"
        )
        
        self.db.add(lucky_play)
        await self.db.flush()
        
        # 6. Créditer les gains si > 0
        new_balance = None
        if winnings > 0:
            if user_id:
                await self.wallet_service.credit(user_id, winnings, "WIN")
                new_balance = float(await self.wallet_service.get_balance(user_id))
            elif ticket_id:
                ticket.balance += winnings
                await self.db.flush()
                new_balance = float(ticket.balance)
        
        # 7. Audit log
        await self.audit_service.log(
            user_id=user_id,
            agent_id=agent_id,
            action=AuditAction.LUCKY_SPIN,
            resource_type="lucky_play",
            resource_id=lucky_play.id,
            ip_address=ip_address,
            new_values={
                "stake": float(stake),
                "multiplier": float(multiplier),
                "winnings": float(winnings),
                "segment": result["label"]
            }
        )
        
        await self.db.commit()
        
        self.logger.info(f"Lucky spin: user={user_id or ticket_id}, stake={stake}, winnings={winnings}")
        
        return LuckySpinResponse(
            success=True,
            segment=result["label"],
            multiplier=float(multiplier),
            winnings=float(winnings),
            color=result["color"],
            play_id=lucky_play.id,
            verification_hash=verification_hash,
            new_balance=new_balance or 0,
            message=f"Vous avez gagné {winnings} HTG !" if winnings > 0 else "Perdu... Réessayez !"
        )
    
    def _spin_wheel(self, segments: List[dict]) -> dict:
        """
        Génère un résultat aléatoire basé sur les poids des segments.
        Utilise secrets pour un vrai aléatoire cryptographique.
        """
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
                    "color": segment["color"],
                    "weight": segment["weight"]
                }
        
        # Fallback (ne devrait jamais arriver)
        return segments[0]
    
    # ========== Historique ==========
    
    async def get_user_history(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[LuckyPlay]:
        """Récupère l'historique des parties d'un utilisateur"""
        result = await self.db.execute(
            select(LuckyPlay)
            .where(LuckyPlay.user_id == user_id)
            .order_by(LuckyPlay.played_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def verify_play(self, play_id: str) -> Dict[str, Any]:
        """Vérifie l'équité d'une partie"""
        play = await self.db.get(LuckyPlay, play_id)
        
        if not play:
            raise AppException(404, "Partie non trouvée")
        
        # Recalculer le hash
        expected_hash = hashlib.sha256(
            f"{play.random_seed}{play.stake}{play.played_at.isoformat()}".encode()
        ).hexdigest()
        
        return {
            "play_id": play_id,
            "is_valid": play.verification_hash == expected_hash,
            "random_seed": play.random_seed,
            "expected_hash": expected_hash,
            "actual_hash": play.verification_hash,
            "verified_at": datetime.utcnow().isoformat()
        }
    
    async def get_statistics(self, user_id: str = None) -> Dict[str, Any]:
        """Récupère les statistiques globales ou par utilisateur"""
        query = select(
            func.count(LuckyPlay.id).label("total_plays"),
            func.sum(LuckyPlay.stake).label("total_stake"),
            func.sum(LuckyPlay.winnings).label("total_winnings"),
            func.count().filter(LuckyPlay.winnings > 0).label("wins")
        )
        
        if user_id:
            query = query.where(LuckyPlay.user_id == user_id)
        
        result = await self.db.execute(query)
        stats = result.one()
        
        total_plays = stats.total_plays or 0
        total_stake = stats.total_stake or 0
        total_winnings = stats.total_winnings or 0
        wins = stats.wins or 0
        
        # Distribution des segments
        segment_distribution = {}
        if total_plays > 0:
            seg_result = await self.db.execute(
                select(LuckyPlay.result_segment["label"].label("segment"), func.count())
                .group_by("segment")
            )
            for row in seg_result:
                segment_distribution[row.segment] = row[1]
        
        return {
            "total_plays": total_plays,
            "total_stake": float(total_stake),
            "total_winnings": float(total_winnings),
            "win_rate": round((wins / total_plays * 100) if total_plays > 0 else 0, 2),
            "best_win": float(await self._get_best_win(user_id)),
            "segment_distribution": segment_distribution,
            "theoretical_rtp": (await self.get_active_config()).theoretical_rtp,
            "actual_rtp": round(float(total_winnings / total_stake * 100) if total_stake > 0 else 0, 2)
        }
    
    async def _get_best_win(self, user_id: str = None) -> Decimal:
        """Récupère le meilleur gain"""
        query = select(func.max(LuckyPlay.winnings))
        if user_id:
            query = query.where(LuckyPlay.user_id == user_id)
        
        result = await self.db.execute(query)
        return result.scalar() or Decimal("0")