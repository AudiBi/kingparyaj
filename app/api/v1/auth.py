# app/api/v1/auth.py
"""API d'authentification complète avec gestion de sécurité"""

from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from datetime import datetime, timedelta
import secrets

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_user, create_access_token, create_refresh_token, decode_token
from app.schemas.user import (
    UserCreate, UserLogin, TokenResponse, UserResponse, 
    ChangePasswordRequest, ForgotPasswordRequest, ResetPasswordRequest, UserUpdate,
    VerifyEmailRequest, ResendVerificationRequest
)
from app.schemas.common import SuccessResponse, ErrorResponse
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.services.notification_service import NotificationService
from app.models.user import User
from app.models.audit import AuditLog, AuditAction
import redis.asyncio as redis

router = APIRouter(tags=["Authentication"])
security = HTTPBearer()


# ==================== AUTHENTICATION ====================

@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Inscription nouveau joueur",
    description="Crée un nouveau compte joueur avec envoi de SMS de bienvenue"
)
async def register(
    request: Request,
    user_data: UserCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Inscription d'un nouveau joueur.
    
    - **phone**: Numéro de téléphone Haïti (8 chiffres, commence par 3 ou 4)
    - **password**: Mot de passe (min 6 caractères)
    - **first_name**: Prénom (optionnel)
    - **last_name**: Nom (optionnel)
    - **email**: Email (optionnel)
    """
    auth_service = AuthService(db, redis_client)
    
    # Vérifier si le téléphone est déjà utilisé
    existing = await auth_service.user_service.get_user_by_phone(user_data.phone)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ce numéro de téléphone est déjà utilisé"
        )
    
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    
    # Créer l'utilisateur
    user = await auth_service.register(
        phone=user_data.phone,
        password=user_data.password,
        first_name=user_data.first_name,
        last_name=user_data.last_name,
        email=user_data.email,
        ip_address=ip_address,
        user_agent=user_agent
    )
    
    # Envoyer SMS de bienvenue en arrière-plan
    background_tasks.add_task(
        send_welcome_sms,
        user.phone,
        user.first_name or "Cher joueur"
    )
    
    # Générer les tokens
    tokens = await auth_service.login(
        phone=user_data.phone,
        password=user_data.password,
        ip_address=ip_address,
        user_agent=user_agent
    )
    
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": "bearer",
        "expires_in": 3600
    }


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Connexion utilisateur",
    description="Authentifie un utilisateur et retourne les tokens JWT"
)
async def login(
    request: Request,
    credentials: UserLogin,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Connexion d'un utilisateur existant.
    
    - **phone**: Numéro de téléphone
    - **password**: Mot de passe
    """
    auth_service = AuthService(db, redis_client)
    
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    
    # Tentative de connexion
    result = await auth_service.login(
        phone=credentials.phone,
        password=credentials.password,
        ip_address=ip_address,
        user_agent=user_agent
    )
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Téléphone ou mot de passe incorrect"
        )
    
    # Vérifier si le compte est bloqué
    if result.get("is_locked"):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=result.get("lock_reason", "Compte bloqué")
        )
    
    return {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "token_type": "bearer",
        "expires_in": result["expires_in"]
    }


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rafraîchir token",
    description="Génère un nouveau token d'accès à partir du refresh token"
)
async def refresh_token(
    refresh_token: str,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Rafraîchit le token d'accès."""
    auth_service = AuthService(db, redis_client)
    
    result = await auth_service.refresh_token(refresh_token)
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalide ou expiré"
        )
    
    return {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "token_type": "bearer",
        "expires_in": result["expires_in"]
    }


@router.post(
    "/logout",
    response_model=SuccessResponse,
    summary="Déconnexion",
    description="Invalide les tokens et déconnecte l'utilisateur"
)
async def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Déconnexion - blacklist le token."""
    auth_service = AuthService(db, redis_client)
    
    token = credentials.credentials
    await auth_service.logout(current_user.id, token)
    
    return SuccessResponse(message="Déconnecté avec succès")


# ==================== UTILISATEUR CONNECTÉ ====================

@router.get(
    "/me",
    response_model=UserResponse,
    summary="Mon profil",
    description="Récupère les informations de l'utilisateur connecté"
)
async def get_current_user_info(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Récupère le profil de l'utilisateur connecté."""
    user_service = UserService(db, None)
    user_with_wallet = await user_service.get_user_with_wallet(current_user.id)
    
    if not user_with_wallet:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    
    return user_with_wallet


@router.put(
    "/me",
    response_model=UserResponse,
    summary="Modifier mon profil",
    description="Met à jour les informations de l'utilisateur connecté"
)
async def update_current_user(
    user_update: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Met à jour le profil."""
    user_service = UserService(db, None)
    
    # Ne pas permettre la modification du téléphone
    if hasattr(user_update, 'phone') and user_update.phone:
        raise HTTPException(status_code=400, detail="Le numéro de téléphone ne peut pas être modifié")
    
    user = await user_service.update_user(
        current_user.id,
        **user_update.dict(exclude_unset=True, exclude={'phone'})
    )
    
    return user


@router.post(
    "/change-password",
    response_model=SuccessResponse,
    summary="Changer mot de passe",
    description="Change le mot de passe de l'utilisateur connecté"
)
async def change_password(
    request: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Change le mot de passe."""
    auth_service = AuthService(db, redis_client)
    
    success = await auth_service.change_password(
        user_id=current_user.id,
        current_password=request.current_password,
        new_password=request.new_password
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Mot de passe actuel incorrect"
        )
    
    return SuccessResponse(message="Mot de passe modifié avec succès")


@router.get(
    "/me/stats",
    summary="Mes statistiques",
    description="Récupère les statistiques de jeu de l'utilisateur"
)
async def get_my_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Récupère les statistiques personnelles."""
    from sqlalchemy import select, func
    from app.models.keno import KenoBet
    from app.models.lucky import LuckyPlay
    
    # Statistiques Keno
    keno_result = await db.execute(
        select(
            func.count(KenoBet.id).label("total_bets"),
            func.coalesce(func.sum(KenoBet.stake), 0).label("total_stake"),
            func.coalesce(func.sum(KenoBet.winnings), 0).label("total_wins"),
            func.count().filter(KenoBet.winnings > 0).label("wins_count")
        ).where(KenoBet.user_id == current_user.id)
    )
    keno_stats = keno_result.one()
    
    # Statistiques Lucky
    lucky_result = await db.execute(
        select(
            func.count(LuckyPlay.id).label("total_plays"),
            func.coalesce(func.sum(LuckyPlay.stake), 0).label("total_stake"),
            func.coalesce(func.sum(LuckyPlay.winnings), 0).label("total_wins")
        ).where(LuckyPlay.user_id == current_user.id)
    )
    lucky_stats = lucky_result.one()
    
    return {
        "keno": {
            "total_bets": keno_stats.total_bets or 0,
            "total_stake": float(keno_stats.total_stake),
            "total_wins": float(keno_stats.total_wins),
            "wins_count": keno_stats.wins_count or 0,
            "win_rate": round((keno_stats.wins_count or 0) / (keno_stats.total_bets or 1) * 100, 2)
        },
        "lucky": {
            "total_plays": lucky_stats.total_plays or 0,
            "total_stake": float(lucky_stats.total_stake),
            "total_wins": float(lucky_stats.total_wins)
        },
        "total": {
            "total_bets": (keno_stats.total_bets or 0) + (lucky_stats.total_plays or 0),
            "total_stake": float(keno_stats.total_stake + lucky_stats.total_stake),
            "total_wins": float(keno_stats.total_wins + lucky_stats.total_wins),
            "net_result": float((keno_stats.total_wins + lucky_stats.total_wins) - 
                               (keno_stats.total_stake + lucky_stats.total_stake))
        }
    }


# ==================== RÉINITIALISATION MOT DE PASSE ====================

@router.post(
    "/forgot-password",
    response_model=SuccessResponse,
    summary="Mot de passe oublié",
    description="Envoie un code de réinitialisation par SMS"
)
async def forgot_password(
    request: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Envoie un code de réinitialisation."""
    user_service = UserService(db, None)
    
    user = await user_service.get_user_by_phone(request.phone)
    if not user:
        # Ne pas révéler si l'utilisateur existe (sécurité)
        return SuccessResponse(
            message="Si ce numéro existe, un code de réinitialisation a été envoyé"
        )
    
    # Générer un code à 6 chiffres
    reset_code = ''.join(str(secrets.randbelow(10)) for _ in range(6))
    
    # Stocker le code dans Redis (expiration 15 minutes)
    await redis_client.setex(f"reset:{request.phone}", 900, reset_code)
    
    # Envoyer SMS en arrière-plan
    background_tasks.add_task(
        send_reset_code_sms,
        user.phone,
        reset_code
    )
    
    return SuccessResponse(
        message="Un code de réinitialisation a été envoyé par SMS"
    )


@router.post(
    "/reset-password",
    response_model=TokenResponse,
    summary="Réinitialiser mot de passe",
    description="Réinitialise le mot de passe avec le code reçu par SMS"
)
async def reset_password(
    request: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Réinitialise le mot de passe."""
    from app.core.security import hash_password
    
    # Vérifier le code
    stored_code = await redis_client.get(f"reset:{request.phone}")
    if not stored_code or stored_code != request.code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code invalide ou expiré"
        )
    
    # Récupérer l'utilisateur
    user_service = UserService(db, None)
    user = await user_service.get_user_by_phone(request.phone)
    
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    
    # Mettre à jour le mot de passe
    user.password_hash = hash_password(request.new_password)
    
    # Supprimer le code
    await redis_client.delete(f"reset:{request.phone}")
    
    # Invalider tous les refresh tokens
    await redis_client.delete(f"refresh:{user.id}")
    
    await db.commit()
    
    # Générer de nouveaux tokens
    from app.core.security import create_access_token, create_refresh_token
    access_token = create_access_token({"sub": user.id, "role": user.role})
    refresh_token = create_refresh_token({"sub": user.id})
    
    await redis_client.setex(f"refresh:{user.id}", 604800, refresh_token)
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": 3600
    }


# ==================== EMAIL VERIFICATION ====================

@router.post(
    "/verify-email",
    response_model=SuccessResponse,
    summary="Vérifier email",
    description="Vérifie l'adresse email avec le code reçu"
)
async def verify_email(
    request: VerifyEmailRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Vérifie l'email."""
    stored_code = await redis_client.get(f"verify_email:{current_user.id}")
    
    if not stored_code or stored_code != request.code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Code invalide ou expiré"
        )
    
    current_user.email_verified = True
    await db.commit()
    await redis_client.delete(f"verify_email:{current_user.id}")
    
    return SuccessResponse(message="Email vérifié avec succès")


@router.post(
    "/resend-verification",
    response_model=SuccessResponse,
    summary="Renvoyer code vérification",
    description="Renvoie le code de vérification par email"
)
async def resend_verification(
    request: ResendVerificationRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user)
):
    """Renvoie le code de vérification."""
    if not current_user.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Aucun email associé au compte"
        )
    
    if current_user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email déjà vérifié"
        )
    
    # Générer un nouveau code
    verification_code = ''.join(str(secrets.randbelow(10)) for _ in range(6))
    
    # Envoyer email en arrière-plan
    background_tasks.add_task(
        send_verification_email,
        current_user.email,
        verification_code,
        current_user.first_name
    )
    
    return SuccessResponse(message="Code de vérification envoyé par email")


# ==================== FONCTIONS UTILITAIRES ====================

async def send_welcome_sms(phone: str, name: str):
    """Envoie un SMS de bienvenue."""
    # À implémenter avec Twilio ou autre service SMS
    # from app.payments.sms import send_sms
    # await send_sms(phone, f"Bienvenue {name} sur Parier Keno Haïti!")
    pass


async def send_reset_code_sms(phone: str, code: str):
    """Envoie le code de réinitialisation par SMS."""
    # À implémenter
    pass


async def send_verification_email(email: str, code: str, name: str):
    """Envoie l'email de vérification."""
    # À implémenter
    pass