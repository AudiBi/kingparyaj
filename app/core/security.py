# app/core/security.py
"""Sécurité - JWT, mots de passe et authentification"""

from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import bcrypt
import jwt
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

from app.config import settings
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.models.user import User
# ❌ SUPPRIMER CETTE LIGNE (import circulaire) :
# from app.services.user_service import UserService


# ==================== SECURITY ====================

security = HTTPBearer()


def hash_password(password: str) -> str:
    """Hash un mot de passe avec bcrypt"""
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Vérifie un mot de passe"""
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Crée un token JWT d'accès"""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({
        "exp": expire,
        "type": "access",
        "iat": datetime.utcnow()
    })
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(data: Dict[str, Any]) -> str:
    """Crée un token JWT de rafraîchissement"""
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({
        "exp": expire,
        "type": "refresh",
        "iat": datetime.utcnow()
    })
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """Décode et vérifie un token JWT"""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM]
        )
        return payload
    except jwt.PyJWTError:
        return None


def generate_verification_code(length: int = 6) -> str:
    """Génère un code de vérification à 6 chiffres"""
    import secrets
    return ''.join(str(secrets.randbelow(10)) for _ in range(length))


# ==================== GET CURRENT USER ====================

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Récupère l'utilisateur courant à partir du token JWT.
    Utilisé comme dépendance pour protéger les routes.
    """
    token = credentials.credentials
    
    # Vérifier blacklist
    is_blacklisted = await redis_client.get(f"blacklist:{token}")
    if is_blacklisted:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token révoqué"
        )
    
    # Décoder le token
    payload = decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalide ou expiré"
        )
    
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalide"
        )
    
    # ✅ IMPORT LOCAL pour éviter l'import circulaire
    from app.services.user_service import UserService
    
    user_service = UserService(db, redis_client)
    # ✅ Utiliser get_by_id (pas get_user_by_id qui n'existe pas)
    user = await user_service.get_by_id(user_id)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilisateur inexistant"
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Compte désactivé"
        )
    
    if user.is_locked:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Compte bloqué: {user.lock_reason or 'Raison non spécifiée'}"
        )
    
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
):
    """Vérifie que l'utilisateur est actif"""
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Utilisateur inactif"
        )
    return current_user


async def get_current_admin(
    current_user: User = Depends(get_current_user)
):
    """Vérifie que l'utilisateur est un administrateur"""
    if current_user.role not in ["admin", "super_admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès réservé aux administrateurs"
        )
    return current_user


async def get_current_agent(
    current_user: User = Depends(get_current_user)
):
    """Vérifie que l'utilisateur est un agent"""
    if current_user.role not in ["agent", "manager"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès réservé aux agents"
        )
    return current_user


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Récupère l'utilisateur si token présent, sinon None.
    Utilisé pour les routes optionnelles.
    """
    if not credentials:
        return None
    
    try:
        return await get_current_user(credentials, db, redis_client)
    except HTTPException:
        return None