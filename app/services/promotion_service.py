# app/services/promotion_service.py
"""Service pour la gestion des promotions et bonus"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func
import redis.asyncio as redis

from app.core.exceptions import AppException, NotFoundException
from app.core.logger import get_logger
from app.models.promotion import Promotion, PromotionType, PromotionStatus, UserPromotion
from app.models.user import User
from app.models.wallet import Wallet
from app.models.transaction import Transaction, TransactionType
from app.services.base import BaseService
from app.services.audit_service import AuditService, AuditAction
from app.services.notification_service import NotificationService
from app.schemas.promotion import PromotionCreate, PromotionUpdate, ClaimPromotionRequest


class PromotionService(BaseService[Promotion, PromotionCreate, PromotionUpdate]):
    """
    Service pour la gestion des promotions et bonus.
    Gère les bonus de dépôt, cashback, et autres offres promotionnelles.
    """
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, Promotion)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.notification_service = NotificationService(db, redis_client)
        self.logger = get_logger("PromotionService")
    
    # ========== CRUD Promotions ==========
    
    async def create_promotion(
        self,
        data: PromotionCreate,
        created_by: str
    ) -> Promotion:
        """Crée une nouvelle promotion"""
        
        # Vérifier code unique
        if data.code:
            existing = await self.db.execute(
                select(Promotion).where(Promotion.code == data.code)
            )
            if existing.scalar_one_or_none():
                raise AppException(400, f"Code {data.code} déjà utilisé")
        
        promotion = Promotion(
            name=data.name,
            code=data.code,
            description=data.description,
            type=data.type,
            config=data.config,
            start_date=data.start_date,
            end_date=data.end_date,
            min_deposit=data.min_deposit,
            max_bonus=data.max_bonus,
            wagering_requirement=data.wagering_requirement,
            eligible_games=data.eligible_games,
            total_budget=data.total_budget,
            new_users_only=data.new_users_only,
            first_deposit_only=data.first_deposit_only,
            status=PromotionStatus.DRAFT,
            created_by=created_by
        )
        
        self.db.add(promotion)
        await self.db.flush()
        
        self.logger.info(f"Promotion created: {promotion.name} (ID: {promotion.id})")
        
        return promotion
    
    async def activate_promotion(self, promotion_id: str, activated_by: str) -> Promotion:
        """Active une promotion"""
        
        promotion = await self.get_or_raise(promotion_id)
        
        if promotion.status != PromotionStatus.DRAFT:
            raise AppException(400, f"Impossible d'activer une promotion en status {promotion.status}")
        
        promotion.status = PromotionStatus.ACTIVE
        promotion.updated_by = activated_by
        promotion.updated_at = datetime.utcnow()
        
        await self.db.flush()
        
        # Invalider le cache
        await self.redis.delete("promotions:active")
        
        self.logger.info(f"Promotion activated: {promotion.name}")
        
        return promotion
    
    async def deactivate_promotion(self, promotion_id: str, deactivated_by: str) -> Promotion:
        """Désactive une promotion"""
        
        promotion = await self.get_or_raise(promotion_id)
        
        promotion.status = PromotionStatus.PAUSED
        promotion.updated_by = deactivated_by
        promotion.updated_at = datetime.utcnow()
        
        await self.db.flush()
        
        await self.redis.delete("promotions:active")
        
        self.logger.info(f"Promotion deactivated: {promotion.name}")
        
        return promotion
    
    async def get_active_promotions(self) -> List[Promotion]:
        """Récupère les promotions actives"""
        
        # Vérifier cache
        cached = await self.redis.get("promotions:active")
        if cached:
            import json
            promotion_ids = json.loads(cached)
            if promotion_ids:
                result = await self.db.execute(
                    select(Promotion).where(Promotion.id.in_(promotion_ids))
                )
                return result.scalars().all()
        
        now = datetime.utcnow()
        result = await self.db.execute(
            select(Promotion)
            .where(
                and_(
                    Promotion.status == PromotionStatus.ACTIVE,
                    Promotion.start_date <= now,
                    Promotion.end_date >= now
                )
            )
        )
        promotions = result.scalars().all()
        
        # Mettre en cache
        if promotions:
            await self.redis.setex(
                "promotions:active",
                300,
                [p.id for p in promotions]
            )
        
        return promotions
    
    # ========== Réclamation de bonus ==========
    
    async def claim_bonus(
        self,
        user_id: str,
        request: ClaimPromotionRequest,
        ip_address: str = None
    ) -> Dict[str, Any]:
        """Réclame un bonus promotionnel"""
        
        # Trouver la promotion
        if request.promotion_code:
            promotion = await self._get_promotion_by_code(request.promotion_code)
        else:
            # Promotion par défaut (ex: bonus bienvenue)
            promotions = await self.get_active_promotions()
            if not promotions:
                raise AppException(404, "Aucune promotion active")
            promotion = promotions[0]
        
        if not promotion:
            raise NotFoundException("Promotion", request.promotion_code or "active")
        
        # Vérifier éligibilité
        is_eligible, reason = await self._check_eligibility(user_id, promotion)
        if not is_eligible:
            raise AppException(400, reason)
        
        # Calculer le bonus
        bonus_amount = await self._calculate_bonus(user_id, promotion)
        if bonus_amount <= 0:
            raise AppException(400, "Bonus nul")
        
        # Créer la réclamation
        user_promotion = UserPromotion(
            user_id=user_id,
            promotion_id=promotion.id,
            bonus_amount=bonus_amount,
            wagering_required=promotion.wagering_requirement * bonus_amount,
            expires_at=datetime.utcnow() + timedelta(days=30)
        )
        
        self.db.add(user_promotion)
        await self.db.flush()
        
        # Créditer le bonus
        from app.services.wallet_service import WalletService
        wallet_service = WalletService(self.db, self.redis)
        await wallet_service.credit(
            user_id=user_id,
            amount=bonus_amount,
            transaction_type="BONUS",
            description=f"Bonus {promotion.name}"
        )
        
        # Mettre à jour les statistiques
        promotion.total_claims += 1
        promotion.total_bonus_given += bonus_amount
        if promotion.total_budget:
            promotion.used_budget += bonus_amount
        
        await self.db.flush()
        
        # Audit
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.DEPOSIT,  # Ou un nouvel action CLAIM_BONUS
            resource_type="promotion",
            resource_id=promotion.id,
            ip_address=ip_address,
            new_values={
                "promotion_name": promotion.name,
                "bonus_amount": float(bonus_amount),
                "wagering_required": user_promotion.wagering_required
            }
        )
        
        # Notification
        await self.notification_service.create_notification(
            user_id=user_id,
            notification_type="PROMOTION",
            title=f"🎁 Bonus {promotion.name} reçu !",
            message=f"Vous avez reçu {bonus_amount} HTG de bonus. Mise requise: {user_promotion.wagering_required} HTG.",
            channel="IN_APP",
            payload={
                "promotion_id": promotion.id,
                "bonus_amount": float(bonus_amount),
                "wagering_required": float(user_promotion.wagering_required)
            }
        )
        
        self.logger.info(f"User {user_id} claimed bonus {promotion.name}: {bonus_amount} HTG")
        
        return {
            "success": True,
            "promotion_name": promotion.name,
            "bonus_amount": float(bonus_amount),
            "wagering_required": float(user_promotion.wagering_required),
            "expires_at": user_promotion.expires_at.isoformat(),
            "message": f"Bonus de {bonus_amount} HTG crédité ! Mise requise: {user_promotion.wagering_required} HTG."
        }
    
    async def _check_eligibility(self, user_id: str, promotion: Promotion) -> tuple[bool, str]:
        """Vérifie si un utilisateur est éligible à une promotion"""
        
        # Vérifier période
        now = datetime.utcnow()
        if now < promotion.start_date:
            return False, "Cette promotion n'a pas encore commencé"
        if now > promotion.end_date:
            return False, "Cette promotion est expirée"
        
        # Vérifier budget
        if promotion.total_budget and promotion.used_budget >= promotion.total_budget:
            return False, "Budget de la promotion épuisé"
        
        # Vérifier nouveau joueur
        if promotion.new_users_only:
            user = await self.db.get(User, user_id)
            if user and user.total_bets_count > 0:
                return False, "Cette promotion est réservée aux nouveaux joueurs"
        
        # Vérifier premier dépôt
        if promotion.first_deposit_only:
            deposits_count = await self.db.execute(
                select(func.count(Transaction.id))
                .where(
                    and_(
                        Transaction.user_id == user_id,
                        Transaction.transaction_type == TransactionType.DEPOSIT,
                        Transaction.status == "COMPLETED"
                    )
                )
            )
            if deposits_count.scalar() > 0:
                return False, "Cette promotion est réservée au premier dépôt"
        
        # Vérifier si déjà réclamé
        existing = await self.db.execute(
            select(UserPromotion).where(
                and_(
                    UserPromotion.user_id == user_id,
                    UserPromotion.promotion_id == promotion.id
                )
            )
        )
        if existing.scalar_one_or_none():
            return False, "Vous avez déjà réclamé cette promotion"
        
        return True, ""
    
    async def _calculate_bonus(self, user_id: str, promotion: Promotion) -> Decimal:
        """Calcule le montant du bonus"""
        
        if promotion.type == PromotionType.DEPOSIT_BONUS:
            # Récupérer le dernier dépôt
            last_deposit = await self.db.execute(
                select(Transaction)
                .where(
                    and_(
                        Transaction.user_id == user_id,
                        Transaction.transaction_type == TransactionType.DEPOSIT,
                        Transaction.status == "COMPLETED"
                    )
                )
                .order_by(Transaction.created_at.desc())
                .limit(1)
            )
            deposit = last_deposit.scalar_one_or_none()
            
            if not deposit:
                return Decimal("0")
            
            deposit_amount = deposit.amount
            min_deposit = Decimal(str(promotion.config.get("min_deposit", 0)))
            
            if deposit_amount < min_deposit:
                return Decimal("0")
            
            bonus_percent = Decimal(str(promotion.config.get("bonus_percent", 0))) / 100
            max_bonus = Decimal(str(promotion.config.get("max_bonus", 0))) if promotion.max_bonus else None
            
            bonus = deposit_amount * bonus_percent
            
            if max_bonus and bonus > max_bonus:
                bonus = max_bonus
            
            return bonus
        
        elif promotion.type == PromotionType.CASHBACK:
            # Calculer les pertes sur la période
            period_days = promotion.config.get("period_days", 7)
            start_date = datetime.utcnow() - timedelta(days=period_days)
            
            losses_result = await self.db.execute(
                select(func.sum(Transaction.amount))
                .where(
                    and_(
                        Transaction.user_id == user_id,
                        Transaction.transaction_type == TransactionType.BET,
                        Transaction.status == "COMPLETED",
                        Transaction.created_at >= start_date
                    )
                )
            )
            total_bets = losses_result.scalar() or Decimal("0")
            
            wins_result = await self.db.execute(
                select(func.sum(Transaction.amount))
                .where(
                    and_(
                        Transaction.user_id == user_id,
                        Transaction.transaction_type == TransactionType.WIN,
                        Transaction.status == "COMPLETED",
                        Transaction.created_at >= start_date
                    )
                )
            )
            total_wins = wins_result.scalar() or Decimal("0")
            
            losses = total_bets - total_wins
            if losses <= 0:
                return Decimal("0")
            
            cashback_percent = Decimal(str(promotion.config.get("percentage", 0))) / 100
            max_cashback = Decimal(str(promotion.config.get("max_cashback", 0))) if promotion.max_bonus else None
            
            cashback = losses * cashback_percent
            
            if max_cashback and cashback > max_cashback:
                cashback = max_cashback
            
            return cashback
        
        # Bonus fixe
        return Decimal(str(promotion.config.get("bonus_amount", 0)))
    
    async def _get_promotion_by_code(self, code: str) -> Optional[Promotion]:
        """Récupère une promotion par son code"""
        result = await self.db.execute(
            select(Promotion).where(
                and_(
                    Promotion.code == code,
                    Promotion.status == PromotionStatus.ACTIVE
                )
            )
        )
        return result.scalar_one_or_none()
    
    # ========== Suivi des bonus ==========
    
    async def update_wagering(
        self,
        user_id: str,
        bet_amount: Decimal,
        game_type: str
    ) -> None:
        """Met à jour le wagering des bonus actifs"""
        
        # Récupérer les bonus non complétés
        result = await self.db.execute(
            select(UserPromotion)
            .where(
                and_(
                    UserPromotion.user_id == user_id,
                    UserPromotion.is_completed == False,
                    UserPromotion.is_expired == False,
                    UserPromotion.expires_at > datetime.utcnow()
                )
            )
        )
        active_bonuses = result.scalars().all()
        
        for bonus in active_bonuses:
            # Vérifier si le jeu est éligible
            promotion = await self.db.get(Promotion, bonus.promotion_id)
            if promotion and game_type in promotion.eligible_games:
                bonus.wagered_amount += bet_amount
                
                if bonus.wagered_amount >= bonus.wagering_required:
                    bonus.is_completed = True
                    bonus.completed_at = datetime.utcnow()
                    
                    # Notification
                    await self.notification_service.create_notification(
                        user_id=user_id,
                        notification_type="PROMOTION",
                        title="✅ Bonus débloqué !",
                        message=f"Votre bonus de {bonus.bonus_amount} HTG est maintenant disponible.",
                        channel="IN_APP"
                    )
        
        await self.db.flush()
    
    async def get_user_bonuses(self, user_id: str) -> Dict[str, Any]:
        """Récupère les bonus d'un utilisateur"""
        
        result = await self.db.execute(
            select(UserPromotion)
            .where(UserPromotion.user_id == user_id)
            .order_by(UserPromotion.claimed_at.desc())
        )
        bonuses = result.scalars().all()
        
        active = []
        completed = []
        expired = []
        
        for bonus in bonuses:
            bonus_data = {
                "id": bonus.id,
                "promotion_id": bonus.promotion_id,
                "bonus_amount": float(bonus.bonus_amount),
                "wagered_amount": float(bonus.wagered_amount),
                "wagering_required": float(bonus.wagering_required),
                "claimed_at": bonus.claimed_at.isoformat(),
                "expires_at": bonus.expires_at.isoformat(),
                "progress": round(float(bonus.wagered_amount / bonus.wagering_required * 100), 2) if bonus.wagering_required > 0 else 100
            }
            
            if bonus.is_expired or bonus.expires_at < datetime.utcnow():
                expired.append(bonus_data)
            elif bonus.is_completed:
                completed.append(bonus_data)
            else:
                active.append(bonus_data)
        
        return {
            "active": active,
            "completed": completed,
            "expired": expired,
            "total_active_bonus": sum(b["bonus_amount"] for b in active)
        }