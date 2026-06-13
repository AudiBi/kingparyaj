# app/services/auth_service.py
"""Service d'authentification et gestion des tokens"""

import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

from app.core.security import (
    hash_password, verify_password, create_access_token, create_refresh_token, decode_token
)
from app.core.exceptions import AppException, UnauthorizedException
from app.core.logger import get_logger
from app.models.user import User
from app.services.user_service import UserService
from app.services.audit_service import AuditService, AuditAction
from app.schemas.user import UserLogin, UserCreate, TokenResponse


class AuthService:
    """Service d'authentification"""
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client
        self.user_service = UserService(db, redis_client)
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("AuthService")
    
    async def register(self, data: UserCreate, ip_address: str) -> User:
        """Inscription d'un nouvel utilisateur"""
        # Vérifier si l'utilisateur existe déjà
        existing = await self.user_service.get_by_phone(data.phone)
        if existing:
            raise AppException(400, "Ce numéro de téléphone est déjà utilisé")
        
        if data.email:
            existing_email = await self.user_service.get_by_email(data.email)
            if existing_email:
                raise AppException(400, "Cet email est déjà utilisé")
        
        # Créer l'utilisateur
        user = await self.user_service.create(data)
        
        # Log audit
        await self.audit_service.log(
            user_id=user.id,
            action=AuditAction.USER_CREATED,
            ip_address=ip_address,
            new_values={"phone": user.phone, "role": user.role}
        )
        
        self.logger.info(f"User registered: {user.phone}")
        return user
    
    async def login(
        self,
        credentials: UserLogin,
        ip_address: str,
        user_agent: str
    ) -> Tuple[User, TokenResponse]:
        """Connexion d'un utilisateur"""
        user = await self.user_service.get_by_phone(credentials.phone)
        
        if not user:
            # Log tentative échouée
            await self.audit_service.log(
                action=AuditAction.LOGIN_FAILED,
                ip_address=ip_address,
                user_agent=user_agent,
                extra_data={"phone": credentials.phone, "reason": "user_not_found"}
            )
            raise UnauthorizedException("Numéro ou mot de passe incorrect")
        
        if not user.is_active:
            raise UnauthorizedException("Compte désactivé")
        
        if user.is_locked:
            raise UnauthorizedException("Compte bloqué. Contactez le support")
        
        if not verify_password(credentials.password, user.password_hash):
            # Incrémenter compteur de tentatives
            await self.user_service.increment_failed_attempts(user.id)
            
            await self.audit_service.log(
                user_id=user.id,
                action=AuditAction.LOGIN_FAILED,
                ip_address=ip_address,
                user_agent=user_agent,
                extra_data={"reason": "invalid_password"}
            )
            raise UnauthorizedException("Numéro ou mot de passe incorrect")
        
        # Réinitialiser les tentatives
        await self.user_service.reset_failed_attempts(user.id)
        
        # Mettre à jour dernière connexion
        await self.user_service.update_last_login(user.id, ip_address)
        
        # Générer tokens
        tokens = await self._create_tokens(user.id, user.role)
        
        # Log audit
        await self.audit_service.log(
            user_id=user.id,
            action=AuditAction.LOGIN,
            ip_address=ip_address,
            user_agent=user_agent
        )
        
        self.logger.info(f"User logged in: {user.phone}")
        return user, tokens
    
    async def logout(self, user_id: str, token: str) -> None:
        """Déconnexion - blacklist le token"""
        # Blacklist le token jusqu'à expiration
        payload = decode_token(token)
        if payload and payload.get("exp"):
            ttl = payload["exp"] - datetime.utcnow().timestamp()
            if ttl > 0:
                await self.redis.setex(f"blacklist:{token}", int(ttl), "1")
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.LOGOUT
        )
        
        self.logger.info(f"User logged out: {user_id}")
    
    async def refresh_token(self, refresh_token: str) -> TokenResponse:
        """Rafraîchit le token d'accès"""
        payload = decode_token(refresh_token)
        
        if not payload or payload.get("type") != "refresh":
            raise UnauthorizedException("Token invalide")
        
        user_id = payload.get("sub")
        if not user_id:
            raise UnauthorizedException("Token invalide")
        
        user = await self.user_service.get_by_id(user_id)
        if not user or not user.is_active:
            raise UnauthorizedException("Utilisateur invalide")
        
        # Vérifier que le refresh token est valide
        stored_token = await self.redis.get(f"refresh:{user_id}")
        if not stored_token or stored_token != refresh_token:
            raise UnauthorizedException("Token invalide")
        
        # Créer nouveaux tokens
        return await self._create_tokens(user.id, user.role)
    
    async def _create_tokens(self, user_id: str, role: str) -> TokenResponse:
        """Crée les tokens JWT"""
        access_token = create_access_token({"sub": user_id, "role": role})
        refresh_token = create_refresh_token({"sub": user_id})
        
        # Stocker refresh token
        from app.config import settings
        await self.redis.setex(
            f"refresh:{user_id}",
            settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
            refresh_token
        )
        
        from app.config import settings
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        )
    
    async def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        ip_address: str
    ) -> bool:
        """Change le mot de passe"""
        user = await self.user_service.get_by_id(user_id)
        
        if not verify_password(current_password, user.password_hash):
            raise AppException(400, "Mot de passe actuel incorrect")
        
        # Mettre à jour le mot de passe
        await self.user_service.update_password(user_id, new_password)
        
        # Blacklist tous les tokens existants
        await self.redis.delete(f"refresh:{user_id}")
        
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.PASSWORD_CHANGE,
            ip_address=ip_address
        )
        
        self.logger.info(f"Password changed for user: {user_id}")
        return True
    
    async def verify_token(self, token: str) -> Optional[dict]:
        """Vérifie un token et retourne le payload"""
        # Vérifier blacklist
        if await self.redis.exists(f"blacklist:{token}"):
            return None
        
        return decode_token(token)