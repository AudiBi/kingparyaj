# app/services/responsible_service.py
"""Service pour le jeu responsable (self-exclusion, limites)"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, or_
import redis.asyncio as redis

from app.core.exceptions import AppException, NotFoundException
from app.core.logger import get_logger
from app.models.responsible import SelfExclusion, ExclusionType, ExclusionReason, PlayerLimit
from app.models.user import User
from app.models.wallet import Wallet
from app.models.transaction import Transaction, TransactionType
from app.services.base import BaseService
from app.services.audit_service import AuditService, AuditAction
from app.services.notification_service import NotificationService
from app.schemas.responsible import SelfExclusionRequest, PlayerLimitRequest


class ResponsibleService(BaseService[SelfExclusion, None, None]):
    """
    Service pour le jeu responsable.
    Gère l'auto-exclusion, les limites de jeu, et la détection de comportements problématiques.
    """
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, SelfExclusion)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.notification_service = NotificationService(db, redis_client)
        self.logger = get_logger("ResponsibleService")
    
    # ========== Self-Exclusion ==========
    
    async def create_self_exclusion(
        self,
        user_id: str,
        request: SelfExclusionRequest,
        ip_address: str = None
    ) -> SelfExclusion:
        """Crée une demande d'auto-exclusion"""
        
        # Vérifier si une exclusion active existe déjà
        active = await self.get_active_exclusion(user_id)
        if active:
            raise AppException(400, "Vous avez déjà une exclusion active")
        
        # Calculer les dates
        start_date = datetime.utcnow()
        end_date = None
        
        if request.exclusion_type == ExclusionType.TEMPORARY:
            if not request.duration_days:
                raise AppException(400, "Durée requise pour exclusion temporaire")
            end_date = start_date + timedelta(days=request.duration_days)
        elif request.exclusion_type == ExclusionType.COOLING_OFF:
            # Pause de 24h maximum
            duration = min(request.duration_days or 1, 1)
            end_date = start_date + timedelta(days=duration)
        
        # Créer l'exclusion
        exclusion = SelfExclusion(
            user_id=user_id,
            exclusion_type=request.exclusion_type,
            start_date=start_date,
            end_date=end_date,
            reason=ExclusionReason.SELF_REQUEST,
            reason_details=request.reason,
            is_active=True,
            activated_at=start_date,
            activated_by=user_id
        )
        
        self.db.add(exclusion)
        await self.db.flush()
        
        # Bloquer le compte utilisateur
        user = await self.db.get(User, user_id)
        if user:
            user.is_active = False
            user.is_locked = True
            user.locked_at = start_date
            user.lock_reason = f"Auto-exclusion: {request.exclusion_type.value}"
            await self.db.flush()
        
        # Audit log
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.SELF_EXCLUSION,
            resource_type="self_exclusion",
            resource_id=exclusion.id,
            ip_address=ip_address,
            new_values={
                "exclusion_type": request.exclusion_type.value,
                "duration_days": request.duration_days,
                "end_date": end_date.isoformat() if end_date else None
            }
        )
        
        # Notification
        await self.notification_service.create_notification(
            user_id=user_id,
            notification_type="SELF_EXCLUSION",
            title="🔒 Auto-exclusion activée",
            message=f"Votre compte est maintenant exclu jusqu'au {end_date.strftime('%d/%m/%Y') if end_date else 'indéfini'}. Pour toute question, contactez notre support.",
            channel="IN_APP"
        )
        
        self.logger.info(f"Self-exclusion created for user {user_id}: {request.exclusion_type.value}")
        
        return exclusion
    
    async def get_active_exclusion(self, user_id: str) -> Optional[SelfExclusion]:
        """Récupère l'exclusion active d'un utilisateur"""
        now = datetime.utcnow()
        
        result = await self.db.execute(
            select(SelfExclusion)
            .where(
                and_(
                    SelfExclusion.user_id == user_id,
                    SelfExclusion.is_active == True,
                    or_(
                        SelfExclusion.end_date.is_(None),
                        SelfExclusion.end_date > now
                    )
                )
            )
            .order_by(SelfExclusion.start_date.desc())
        )
        return result.scalar_one_or_none()
    
    async def check_exclusion_status(self, user_id: str) -> Dict[str, Any]:
        """Vérifie le statut d'exclusion d'un utilisateur"""
        
        active = await self.get_active_exclusion(user_id)
        
        if not active:
            return {
                "is_excluded": False,
                "message": "Aucune exclusion active"
            }
        
        now = datetime.utcnow()
        is_expired = active.end_date and active.end_date <= now
        
        if is_expired:
            # Auto-désactiver l'exclusion expirée
            active.is_active = False
            await self.db.flush()
            
            # Réactiver le compte
            user = await self.db.get(User, user_id)
            if user:
                user.is_active = True
                user.is_locked = False
                user.locked_at = None
                user.lock_reason = None
                await self.db.flush()
            
            return {
                "is_excluded": False,
                "message": "L'exclusion est terminée"
            }
        
        return {
            "is_excluded": True,
            "exclusion_type": active.exclusion_type.value,
            "start_date": active.start_date.isoformat(),
            "end_date": active.end_date.isoformat() if active.end_date else None,
            "remaining_days": (active.end_date - now).days if active.end_date else None,
            "message": f"Votre compte est exclu jusqu'au {active.end_date.strftime('%d/%m/%Y') if active.end_date else 'indéfini'}"
        }
    
    async def lift_exclusion(
        self,
        user_id: str,
        admin_id: str,
        reason: str = None
    ) -> SelfExclusion:
        """Lève une exclusion (admin uniquement)"""
        
        active = await self.get_active_exclusion(user_id)
        if not active:
            raise NotFoundException("Exclusion active", user_id)
        
        active.is_active = False
        active.lifted_at = datetime.utcnow()
        active.lifted_by = admin_id
        active.lift_reason = reason
        
        # Réactiver le compte
        user = await self.db.get(User, user_id)
        if user:
            user.is_active = True
            user.is_locked = False
            user.locked_at = None
            user.lock_reason = None
            await self.db.flush()
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.ACCOUNT_FROZEN,  # ou un nouvel action UNBLOCK
            resource_type="self_exclusion",
            resource_id=active.id,
            agent_id=admin_id,
            reason=reason
        )
        
        self.logger.info(f"Exclusion lifted for user {user_id} by admin {admin_id}")
        
        return active
    
    # ========== Limites Joueur ==========
    
    async def set_player_limits(
        self,
        user_id: str,
        request: PlayerLimitRequest,
        set_by: str = None
    ) -> List[PlayerLimit]:
        """Définit les limites de jeu d'un joueur"""
        
        limits = []
        
        if request.daily_deposit_limit is not None:
            limit = await self._set_limit(
                user_id, "DAILY_DEPOSIT", request.daily_deposit_limit, set_by
            )
            limits.append(limit)
        
        if request.daily_loss_limit is not None:
            limit = await self._set_limit(
                user_id, "DAILY_LOSS", request.daily_loss_limit, set_by
            )
            limits.append(limit)
        
        if request.weekly_deposit_limit is not None:
            limit = await self._set_limit(
                user_id, "WEEKLY_DEPOSIT", request.weekly_deposit_limit, set_by
            )
            limits.append(limit)
        
        if request.monthly_deposit_limit is not None:
            limit = await self._set_limit(
                user_id, "MONTHLY_DEPOSIT", request.monthly_deposit_limit, set_by
            )
            limits.append(limit)
        
        if request.single_bet_limit is not None:
            limit = await self._set_limit(
                user_id, "SINGLE_BET", request.single_bet_limit, set_by
            )
            limits.append(limit)
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.LIMIT_CHANGED,
            resource_type="player_limits",
            new_values=request.model_dump(),
            agent_id=set_by
        )
        
        self.logger.info(f"Limits updated for user {user_id}")
        
        return limits
    
    async def _set_limit(
        self,
        user_id: str,
        limit_type: str,
        amount: Decimal,
        set_by: str = None
    ) -> PlayerLimit:
        """Définit ou met à jour une limite spécifique"""
        
        # Vérifier si la limite existe déjà
        result = await self.db.execute(
            select(PlayerLimit).where(
                and_(
                    PlayerLimit.user_id == user_id,
                    PlayerLimit.limit_type == limit_type
                )
            )
        )
        existing = result.scalar_one_or_none()
        
        if existing:
            existing.previous_limit = existing.limit_amount
            existing.limit_amount = amount
            existing.is_active = True
            existing.modified_at = datetime.utcnow()
            existing.modified_by = set_by
            limit = existing
        else:
            limit = PlayerLimit(
                user_id=user_id,
                limit_type=limit_type,
                limit_amount=amount,
                is_active=True,
                set_at=datetime.utcnow(),
                set_by=set_by
            )
            self.db.add(limit)
        
        await self.db.flush()
        
        # Mettre à jour le wallet
        wallet = await self.db.get(Wallet, user_id)
        if wallet:
            if limit_type == "DAILY_DEPOSIT":
                wallet.daily_deposit_limit = amount
            elif limit_type == "DAILY_LOSS":
                wallet.daily_loss_limit = amount
            elif limit_type == "SINGLE_BET":
                wallet.single_bet_limit = amount
            await self.db.flush()
        
        return limit
    
    async def get_player_limits(self, user_id: str) -> List[PlayerLimit]:
        """Récupère les limites actives d'un joueur"""
        result = await self.db.execute(
            select(PlayerLimit).where(
                and_(
                    PlayerLimit.user_id == user_id,
                    PlayerLimit.is_active == True
                )
            )
        )
        return result.scalars().all()
    
    async def remove_limit(self, limit_id: str, user_id: str) -> bool:
        """Supprime une limite (la désactive)"""
        
        result = await self.db.execute(
            select(PlayerLimit).where(
                and_(
                    PlayerLimit.id == limit_id,
                    PlayerLimit.user_id == user_id
                )
            )
        )
        limit = result.scalar_one_or_none()
        
        if not limit:
            raise NotFoundException("Limite", limit_id)
        
        limit.is_active = False
        limit.modified_at = datetime.utcnow()
        
        await self.db.flush()
        
        return True
    
    # ========== Détection comportements problématiques ==========
    
    async def check_problematic_behavior(self, user_id: str) -> Dict[str, Any]:
        """
        Analyse le comportement d'un joueur pour détecter des signes de jeu problématique.
        """
        alerts = []
        risk_level = "low"
        
        # 1. Vérifier les pertes sur 24h
        last_24h = datetime.utcnow() - timedelta(hours=24)
        losses_result = await self.db.execute(
            select(func.sum(Transaction.amount))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.transaction_type == TransactionType.BET,
                    Transaction.status == TransactionStatus.COMPLETED,
                    Transaction.created_at >= last_24h
                )
            )
        )
        total_losses = losses_result.scalar() or Decimal("0")
        
        if total_losses > Decimal("50000"):
            alerts.append({
                "type": "high_losses_24h",
                "message": f"Pertes élevées sur 24h: {total_losses} HTG",
                "severity": "high"
            })
            risk_level = "high"
        elif total_losses > Decimal("20000"):
            alerts.append({
                "type": "moderate_losses_24h",
                "message": f"Pertes modérées sur 24h: {total_losses} HTG",
                "severity": "medium"
            })
            risk_level = "medium"
        
        # 2. Vérifier le nombre de paris sur 24h
        bets_count_result = await self.db.execute(
            select(func.count(Transaction.id))
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.transaction_type == TransactionType.BET,
                    Transaction.created_at >= last_24h
                )
            )
        )
        bets_count = bets_count_result.scalar() or 0
        
        if bets_count > 100:
            alerts.append({
                "type": "high_bet_frequency",
                "message": f"Fréquence de paris élevée: {bets_count} paris en 24h",
                "severity": "medium"
            })
            if risk_level != "high":
                risk_level = "medium"
        
        # 3. Vérifier les sessions longues
        # À implémenter avec les logs de connexion
        
        # 4. Vérifier les dépôts répétés après pertes
        deposits_after_losses = await self._check_chasing_losses(user_id)
        if deposits_after_losses:
            alerts.append({
                "type": "chasing_losses",
                "message": "Dépôts répétés après pertes (comportement à risque)",
                "severity": "high"
            })
            risk_level = "high"
        
        # Générer un rapport
        return {
            "user_id": user_id,
            "risk_level": risk_level,
            "alerts": alerts,
            "total_losses_24h": float(total_losses),
            "total_bets_24h": bets_count,
            "recommendation": self._get_recommendation(risk_level)
        }
    
    async def _check_chasing_losses(self, user_id: str) -> bool:
        """Vérifie si le joueur fait des dépôts après des pertes"""
        last_24h = datetime.utcnow() - timedelta(hours=24)
        
        # Pattern: perte > dépôt dans l'heure suivante
        result = await self.db.execute(
            select(
                Transaction.created_at,
                Transaction.amount,
                Transaction.transaction_type
            )
            .where(
                and_(
                    Transaction.user_id == user_id,
                    Transaction.created_at >= last_24h,
                    Transaction.transaction_type.in_([TransactionType.BET, TransactionType.DEPOSIT])
                )
            )
            .order_by(Transaction.created_at)
        )
        transactions = result.all()
        
        for i, tx in enumerate(transactions[:-1]):
            if (tx.transaction_type == TransactionType.BET and 
                tx.amount > 1000 and
                transactions[i+1].transaction_type == TransactionType.DEPOSIT and
                (transactions[i+1].created_at - tx.created_at).seconds < 3600):
                return True
        
        return False
    
    def _get_recommendation(self, risk_level: str) -> str:
        """Retourne une recommandation basée sur le niveau de risque"""
        recommendations = {
            "low": "Votre comportement de jeu semble sain. Continuez à jouer responsablement.",
            "medium": "Nous vous invitons à consulter vos limites de jeu. Pensez à définir des limites de dépôt.",
            "high": "Nous vous recommandons vivement de faire une pause. Vous pouvez activer l'auto-exclusion temporaire."
        }
        return recommendations.get(risk_level, recommendations["low"])
    
    # ========== Cooling-off ==========
    
    async def activate_cooling_off(self, user_id: str, hours: int = 24) -> SelfExclusion:
        """Active une pause (cooling-off) de courte durée"""
        
        if hours > 48:
            raise AppException(400, "La pause ne peut pas dépasser 48 heures")
        
        request = SelfExclusionRequest(
            exclusion_type=ExclusionType.COOLING_OFF,
            duration_days=hours // 24 + (1 if hours % 24 > 0 else 0),
            reason=f"Pause demandée pour {hours} heures"
        )
        
        return await self.create_self_exclusion(user_id, request)