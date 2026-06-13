# app/services/user_service.py
"""Service pour la gestion des utilisateurs, KYC et parrainage"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, update
import redis.asyncio as redis
import secrets

from app.core.exceptions import AppException, NotFoundException
from app.core.security import hash_password, verify_password, generate_verification_code
from app.core.logger import get_logger
from app.models.user import User, UserRole, KYCStatus
from app.models.wallet import Wallet
from app.models.audit import AuditLog, AuditAction
from app.services.base import BaseService
from app.services.audit_service import AuditService
from app.schemas.user import UserCreate, UserUpdate, UserAdminUpdate, KYCSubmission


class UserService(BaseService[User, UserCreate, UserUpdate]):
    """
    Service complet pour la gestion des utilisateurs.
    Gère les inscriptions, profils, KYC, parrainage, et verrouillage.
    """
    
    MAX_LOGIN_ATTEMPTS = 5
    LOCKOUT_DURATION_MINUTES = 30
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, User)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("UserService")
    
    # ========== CRUD de base ==========
    
    async def get_by_phone(self, phone: str) -> Optional[User]:
        """Récupère un utilisateur par son numéro de téléphone"""
        result = await self.db.execute(
            select(User).where(
                and_(
                    User.phone == phone,
                    User.is_deleted == False
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def get_by_email(self, email: str) -> Optional[User]:
        """Récupère un utilisateur par son email"""
        if not email:
            return None
        result = await self.db.execute(
            select(User).where(
                and_(
                    User.email == email,
                    User.is_deleted == False
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def get_by_national_id(self, national_id: str) -> Optional[User]:
        """Récupère un utilisateur par sa carte d'identité"""
        result = await self.db.execute(
            select(User).where(
                and_(
                    User.national_id == national_id,
                    User.is_deleted == False
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def get_by_referral_code(self, referral_code: str) -> Optional[User]:
        """Récupère un utilisateur par son code de parrainage"""
        result = await self.db.execute(
            select(User).where(
                and_(
                    User.referral_code == referral_code,
                    User.is_deleted == False
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def get_agents(
        self,
        bureau_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ) -> List[User]:
        """Récupère la liste des agents"""
        query = select(User).where(
            and_(
                User.role.in_([UserRole.AGENT, UserRole.MANAGER]),
                User.is_deleted == False
            )
        )
        
        if bureau_id:
            query = query.where(User.bureau_id == bureau_id)
        
        query = query.order_by(User.created_at.desc()).offset(skip).limit(limit)
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    # ========== Création et mise à jour ==========
    
    async def create(self, data: UserCreate, user_id: str = None) -> User:
        """Crée un nouvel utilisateur"""
        
        # Vérifier que le téléphone n'existe pas
        existing = await self.get_by_phone(data.phone)
        if existing:
            raise AppException(400, "Ce numéro de téléphone est déjà utilisé")
        
        if data.email:
            existing_email = await self.get_by_email(data.email)
            if existing_email:
                raise AppException(400, "Cet email est déjà utilisé")
        
        if data.national_id:
            existing_nid = await self.get_by_national_id(data.national_id)
            if existing_nid:
                raise AppException(400, "Cette carte d'identité est déjà utilisée")
        
        # Générer un code de parrainage unique
        referral_code = await self._generate_unique_referral_code()
        
        # Gérer le parrainage
        referrer_id = None
        if data.referral_code:
            referrer = await self.get_by_referral_code(data.referral_code)
            if referrer:
                referrer_id = referrer.id
        
        # Créer l'utilisateur
        user = User(
            email=data.email,
            phone=data.phone,
            first_name=data.first_name,
            last_name=data.last_name,
            national_id=data.national_id,
            password_hash=hash_password(data.password),
            role=UserRole.PLAYER,
            referral_code=referral_code,
            referrer_id=referrer_id,
            created_by=user_id,
            created_at=datetime.utcnow()
        )
        
        self.db.add(user)
        await self.db.flush()
        
        # Créer le portefeuille associé
        wallet = Wallet(user_id=user.id, balance=Decimal("0"))
        self.db.add(wallet)
        
        await self.db.flush()
        
        self.logger.info(f"User created: {user.phone} (ID: {user.id})")
        
        # Bonus de parrainage
        if referrer_id:
            await self._grant_referral_bonus(referrer_id)
        
        return user
    
    async def update_profile(
        self,
        user_id: str,
        data: UserUpdate,
        updater_id: str = None
    ) -> User:
        """Met à jour le profil d'un utilisateur"""
        
        user = await self.get_or_raise(user_id)
        
        if data.email and data.email != user.email:
            existing = await self.get_by_email(data.email)
            if existing and existing.id != user_id:
                raise AppException(400, "Cet email est déjà utilisé")
            user.email = data.email
        
        if data.phone and data.phone != user.phone:
            existing = await self.get_by_phone(data.phone)
            if existing and existing.id != user_id:
                raise AppException(400, "Ce numéro de téléphone est déjà utilisé")
            user.phone = data.phone
        
        if data.first_name is not None:
            user.first_name = data.first_name
        
        if data.last_name is not None:
            user.last_name = data.last_name
        
        user.updated_by = updater_id or user_id
        user.updated_at = datetime.utcnow()
        
        await self.db.flush()
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.USER_UPDATED,
            resource_type="user",
            resource_id=user.id,
            new_values={"email": user.email, "phone": user.phone}
        )
        
        self.logger.info(f"User profile updated: {user_id}")
        
        return user
    
    async def update_password(
        self,
        user_id: str,
        new_password: str,
        updater_id: str = None
    ) -> bool:
        """Met à jour le mot de passe d'un utilisateur"""
        
        user = await self.get_or_raise(user_id)
        user.password_hash = hash_password(new_password)
        user.updated_at = datetime.utcnow()
        user.updated_by = updater_id or user_id
        
        await self.db.flush()
        
        # Invalider tous les tokens de rafraîchissement
        await self.redis.delete(f"refresh:{user_id}")
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.PASSWORD_CHANGE,
            resource_type="user",
            resource_id=user.id
        )
        
        self.logger.info(f"Password updated for user: {user_id}")
        
        return True
    
    # ========== Gestion des tentatives de connexion ==========
    
    async def increment_failed_attempts(self, user_id: str) -> int:
        """Incrémente le compteur de tentatives échouées"""
        user = await self.get_or_raise(user_id)
        
        # Récupérer le compteur actuel depuis Redis
        key = f"login_attempts:{user_id}"
        attempts = await self.redis.incr(key)
        await self.redis.expire(key, self.LOCKOUT_DURATION_MINUTES * 60)
        
        # Bloquer le compte si trop de tentatives
        if attempts >= self.MAX_LOGIN_ATTEMPTS:
            user.is_locked = True
            user.locked_at = datetime.utcnow()
            user.lock_reason = f"Trop de tentatives de connexion ({attempts})"
            await self.db.flush()
            
            await self.audit_service.log(
                user_id=user_id,
                action=AuditAction.ACCOUNT_FROZEN,
                resource_type="user",
                resource_id=user.id,
                reason=user.lock_reason
            )
            
            self.logger.warning(f"User {user_id} locked due to too many failed attempts")
        
        return attempts
    
    async def reset_failed_attempts(self, user_id: str) -> None:
        """Réinitialise le compteur de tentatives échouées"""
        await self.redis.delete(f"login_attempts:{user_id}")
        
        # Débloquer le compte si nécessaire
        user = await self.get_or_raise(user_id)
        if user.is_locked:
            user.is_locked = False
            user.locked_at = None
            user.lock_reason = None
            await self.db.flush()
    
    async def update_last_login(self, user_id: str, ip_address: str) -> None:
        """Met à jour la date de dernière connexion"""
        user = await self.get_or_raise(user_id)
        user.last_login = datetime.utcnow()
        user.last_ip = ip_address
        await self.db.flush()
    
    # ========== Gestion admin ==========
    
    async def admin_update(
        self,
        user_id: str,
        data: UserAdminUpdate,
        admin_id: str
    ) -> User:
        """Mise à jour d'un utilisateur par un administrateur"""
        
        user = await self.get_or_raise(user_id)
        
        if data.role is not None:
            old_role = user.role
            user.role = data.role
            
            await self.audit_service.log(
                user_id=user_id,
                action=AuditAction.USER_UPDATED,
                resource_type="user",
                resource_id=user.id,
                old_values={"role": old_role},
                new_values={"role": data.role},
                agent_id=admin_id
            )
        
        if data.is_active is not None:
            user.is_active = data.is_active
        
        if data.is_locked is not None:
            user.is_locked = data.is_locked
            if data.is_locked:
                user.locked_at = datetime.utcnow()
                user.lock_reason = data.lock_reason
            else:
                user.locked_at = None
                user.lock_reason = None
        
        if data.kyc_status is not None:
            user.kyc_status = data.kyc_status
            if data.kyc_status == KYCStatus.VERIFIED:
                user.kyc_verified_at = datetime.utcnow()
                user.kyc_verified_by = admin_id
        
        if data.bureau_id is not None:
            user.bureau_id = data.bureau_id
        
        user.updated_by = admin_id
        user.updated_at = datetime.utcnow()
        
        await self.db.flush()
        
        self.logger.info(f"User {user_id} updated by admin {admin_id}")
        
        return user
    
    async def block_user(
        self,
        user_id: str,
        reason: str,
        admin_id: str,
        duration_days: Optional[int] = None
    ) -> User:
        """Bloque un utilisateur (temporairement ou définitivement)"""
        
        user = await self.get_or_raise(user_id)
        
        user.is_active = False
        user.is_locked = True
        user.locked_at = datetime.utcnow()
        user.lock_reason = reason
        user.updated_by = admin_id
        user.updated_at = datetime.utcnow()
        
        await self.db.flush()
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.USER_BLOCKED,
            resource_type="user",
            resource_id=user.id,
            reason=reason,
            agent_id=admin_id,
            extra_data={"duration_days": duration_days}
        )
        
        self.logger.warning(f"User {user_id} blocked by admin {admin_id}: {reason}")
        
        return user
    
    async def unblock_user(self, user_id: str, admin_id: str) -> User:
        """Débloque un utilisateur"""
        
        user = await self.get_or_raise(user_id)
        
        user.is_active = True
        user.is_locked = False
        user.locked_at = None
        user.lock_reason = None
        user.updated_by = admin_id
        user.updated_at = datetime.utcnow()
        
        await self.db.flush()
        
        # Réinitialiser les tentatives
        await self.redis.delete(f"login_attempts:{user_id}")
        
        self.logger.info(f"User {user_id} unblocked by admin {admin_id}")
        
        return user
    
    # ========== KYC (Know Your Customer) ==========
    
    async def submit_kyc(
        self,
        user_id: str,
        data: KYCSubmission,
        ip_address: str = None
    ) -> Dict[str, Any]:
        """Soumet des documents KYC"""
        
        user = await self.get_or_raise(user_id)
        
        if user.kyc_status == KYCStatus.VERIFIED:
            raise AppException(400, "KYC déjà vérifié")
        
        # Stocker les URLs des documents
        import json
        documents = {
            "document_type": data.document_type,
            "document_number": data.document_number,
            "front_url": data.document_front_url,
            "back_url": data.document_back_url,
            "selfie_url": data.selfie_url,
            "submitted_at": datetime.utcnow().isoformat()
        }
        
        user.kyc_status = KYCStatus.PENDING
        user.kyc_documents = json.dumps(documents)
        user.updated_at = datetime.utcnow()
        
        await self.db.flush()
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.KYC_SUBMITTED,
            resource_type="user",
            resource_id=user.id,
            ip_address=ip_address,
            extra_data={"document_type": data.document_type}
        )
        
        self.logger.info(f"KYC submitted for user {user_id}")
        
        return {
            "status": "pending",
            "message": "Vos documents ont été soumis. Vérification en cours.",
            "submitted_at": datetime.utcnow().isoformat()
        }
    
    async def verify_kyc(
        self,
        user_id: str,
        admin_id: str,
        approved: bool,
        rejection_reason: str = None
    ) -> User:
        """Vérifie les documents KYC (admin uniquement)"""
        
        user = await self.get_or_raise(user_id)
        
        if approved:
            user.kyc_status = KYCStatus.VERIFIED
            user.kyc_verified_at = datetime.utcnow()
            user.kyc_verified_by = admin_id
        else:
            user.kyc_status = KYCStatus.REJECTED
        
        user.updated_at = datetime.utcnow()
        user.updated_by = admin_id
        
        await self.db.flush()
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.KYC_VERIFIED,
            resource_type="user",
            resource_id=user.id,
            agent_id=admin_id,
            extra_data={"approved": approved, "rejection_reason": rejection_reason}
        )
        
        self.logger.info(f"KYC verified for user {user_id}: approved={approved}")
        
        return user
    
    # ========== Parrainage (Référence) ==========
    
    async def _generate_unique_referral_code(self) -> str:
        """Génère un code de parrainage unique"""
        import secrets
        import string
        
        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
            existing = await self.get_by_referral_code(code)
            if not existing:
                return code
    
    async def _grant_referral_bonus(self, referrer_id: str) -> None:
        """Accorde le bonus de parrainage"""
        from app.services.wallet_service import WalletService
        
        wallet_service = WalletService(self.db, self.redis)
        
        # Bonus de 50 HTG pour le parrain
        await wallet_service.credit(
            user_id=referrer_id,
            amount=Decimal("50"),
            transaction_type="BONUS",
            description="Bonus de parrainage"
        )
        
        # Notification
        from app.services.notification_service import NotificationService
        notification_service = NotificationService(self.db, self.redis)
        await notification_service.create_notification(
            user_id=referrer_id,
            notification_type="PROMOTION",
            title="🎁 Bonus de parrainage",
            message="Un nouveau joueur s'est inscrit avec votre code ! Vous avez reçu 50 HTG de bonus.",
            channel="IN_APP"
        )
    
    async def get_referrals(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[User]:
        """Récupère la liste des utilisateurs parrainés"""
        result = await self.db.execute(
            select(User)
            .where(User.referrer_id == user_id)
            .order_by(User.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def get_referral_stats(self, user_id: str) -> Dict[str, Any]:
        """Récupère les statistiques de parrainage"""
        
        # Nombre de parrainés
        result = await self.db.execute(
            select(func.count(User.id))
            .where(User.referrer_id == user_id)
        )
        total_referrals = result.scalar() or 0
        
        # Parrainés actifs (ceux qui ont joué)
        active_result = await self.db.execute(
            select(func.count(User.id))
            .where(
                and_(
                    User.referrer_id == user_id,
                    User.total_bets_count > 0
                )
            )
        )
        active_referrals = active_result.scalar() or 0
        
        return {
            "total_referrals": total_referrals,
            "active_referrals": active_referrals,
            "bonus_earned": total_referrals * 50,  # 50 HTG par parrainage
            "referral_code": (await self.get_or_raise(user_id)).referral_code
        }
    
    # ========== Statistiques ==========
    
    async def get_statistics(self) -> Dict[str, Any]:
        """Récupère les statistiques globales des utilisateurs"""
        
        # Total utilisateurs
        total_result = await self.db.execute(
            select(func.count(User.id)).where(User.is_deleted == False)
        )
        total_users = total_result.scalar() or 0
        
        # Joueurs actifs (dernière connexion < 30 jours)
        thirty_days_ago = datetime.utcnow() - timedelta(days=30)
        active_result = await self.db.execute(
            select(func.count(User.id))
            .where(
                and_(
                    User.is_deleted == False,
                    User.last_login >= thirty_days_ago
                )
            )
        )
        active_users = active_result.scalar() or 0
        
        # Nouveaux utilisateurs (7 derniers jours)
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        new_result = await self.db.execute(
            select(func.count(User.id))
            .where(User.created_at >= seven_days_ago)
        )
        new_users = new_result.scalar() or 0
        
        # KYC status
        kyc_result = await self.db.execute(
            select(
                User.kyc_status,
                func.count(User.id)
            )
            .group_by(User.kyc_status)
        )
        kyc_distribution = {status.value: count for status, count in kyc_result}
        
        # Rôles
        role_result = await self.db.execute(
            select(
                User.role,
                func.count(User.id)
            )
            .group_by(User.role)
        )
        role_distribution = {role.value: count for role, count in role_result}
        
        return {
            "total_users": total_users,
            "active_users": active_users,
            "active_rate": round((active_users / total_users * 100) if total_users > 0 else 0, 2),
            "new_users_last_7_days": new_users,
            "kyc_distribution": kyc_distribution,
            "role_distribution": role_distribution
        }
    
    async def search_users(
        self,
        query: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[User]:
        """Recherche des utilisateurs par téléphone, email ou nom"""
        
        search_pattern = f"%{query}%"
        
        result = await self.db.execute(
            select(User)
            .where(
                and_(
                    User.is_deleted == False,
                    or_(
                        User.phone.like(search_pattern),
                        User.email.like(search_pattern),
                        User.first_name.like(search_pattern),
                        User.last_name.like(search_pattern)
                    )
                )
            )
            .order_by(User.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()