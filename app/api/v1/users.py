# app/api/v1/users.py
"""API de gestion des utilisateurs (Admin uniquement)"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional, List
from datetime import datetime

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_admin, get_current_user
from app.schemas.user import (
    UserResponse, UserUpdate, UserCreate, UserListResponse,
    KYCUpdate, KYCStatusUpdate
)
from app.schemas.common import SuccessResponse, PaginatedResponse
from app.services.user_service import UserService
from app.services.wallet_service import WalletService
from app.models.user import User, UserRole, KYCStatus
from app.models.audit import AuditLog, AuditAction
import redis.asyncio as redis

router = APIRouter(prefix="/users", tags=["Users"])


# ==================== ADMIN ENDPOINTS ====================

@router.get(
    "/",
    response_model=PaginatedResponse[UserResponse],
    summary="Liste des utilisateurs",
    description="Récupère la liste paginée des utilisateurs (Admin uniquement)"
)
async def get_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    role: Optional[UserRole] = None,
    search: Optional[str] = None,
    kyc_status: Optional[KYCStatus] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste tous les utilisateurs (paginé)."""
    
    query = select(User).where(User.is_deleted == False)
    
    # Filtres
    if role:
        query = query.where(User.role == role)
    if kyc_status:
        query = query.where(User.kyc_status == kyc_status)
    if search:
        query = query.where(
            (User.phone.contains(search)) |
            (User.first_name.contains(search)) |
            (User.last_name.contains(search)) |
            (User.email.contains(search))
        )
    
    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()
    
    query = query.order_by(User.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    users = result.scalars().all()
    
    return {
        "items": users,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "has_next": page * per_page < total,
        "has_prev": page > 1
    }


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Détails utilisateur",
    description="Récupère les détails d'un utilisateur spécifique"
)
async def get_user(
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Récupère un utilisateur par son ID."""
    user_service = UserService(db, None)
    user = await user_service.get_user_by_id(user_id)
    
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    
    return user


@router.put(
    "/{user_id}",
    response_model=UserResponse,
    summary="Modifier utilisateur",
    description="Modifie les informations d'un utilisateur (Admin)"
)
async def update_user(
    user_id: str,
    user_update: UserUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Modifie un utilisateur."""
    user_service = UserService(db, None)
    
    user = await user_service.update_user(
        user_id,
        **user_update.dict(exclude_unset=True)
    )
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_UPDATED,
        resource_type="user",
        resource_id=user_id,
        new_values=user_update.dict(exclude_unset=True)
    )
    db.add(audit)
    await db.commit()
    
    return user


@router.post(
    "/{user_id}/block",
    response_model=SuccessResponse,
    summary="Bloquer utilisateur",
    description="Bloque un utilisateur (empêche connexion et paris)"
)
async def block_user(
    user_id: str,
    reason: str = Query(..., description="Raison du blocage"),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Bloque un utilisateur."""
    user_service = UserService(db, None)
    
    user = await user_service.block_user(user_id, reason, admin.id)
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_BLOCKED,
        resource_type="user",
        resource_id=user_id,
        reason=reason
    )
    db.add(audit)
    await db.commit()
    
    return SuccessResponse(message=f"Utilisateur {user.phone} bloqué avec succès")


@router.post(
    "/{user_id}/unblock",
    response_model=SuccessResponse,
    summary="Débloquer utilisateur",
    description="Débloque un utilisateur précédemment bloqué"
)
async def unblock_user(
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Débloque un utilisateur."""
    user_service = UserService(db, None)
    
    user = await user_service.unblock_user(user_id)
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_UPDATED,
        resource_type="user",
        resource_id=user_id,
        new_values={"is_locked": False, "is_active": True}
    )
    db.add(audit)
    await db.commit()
    
    return SuccessResponse(message=f"Utilisateur {user.phone} débloqué avec succès")


@router.post(
    "/{user_id}/kyc",
    response_model=SuccessResponse,
    summary="Mettre à jour KYC",
    description="Met à jour le statut KYC d'un utilisateur"
)
async def update_kyc_status(
    user_id: str,
    kyc_data: KYCStatusUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Met à jour le statut KYC."""
    user_service = UserService(db, None)
    
    user = await user_service.update_kyc_status(
        user_id=user_id,
        status=kyc_data.status,
        verified_by=admin.id,
        national_id=kyc_data.national_id,
        documents=kyc_data.documents
    )
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.KYC_VERIFIED if kyc_data.status == KYCStatus.VERIFIED else AuditAction.KYC_SUBMITTED,
        resource_type="user",
        resource_id=user_id,
        new_values={"kyc_status": kyc_data.status}
    )
    db.add(audit)
    await db.commit()
    
    return SuccessResponse(message=f"Statut KYC mis à jour: {kyc_data.status.value}")


@router.post(
    "/{user_id}/credit",
    response_model=SuccessResponse,
    summary="Créditer manuellement",
    description="Crédite manuellement le compte d'un utilisateur (Admin)"
)
async def manual_credit(
    user_id: str,
    amount: float,
    reason: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Crédite manuellement un utilisateur."""
    from decimal import Decimal
    from app.models.transaction import Transaction, TransactionType, PaymentMethod, TransactionStatus
    
    wallet_service = WalletService(db, redis_client)
    wallet = await wallet_service.get_wallet(user_id)
    
    old_balance = wallet.balance
    wallet.balance += Decimal(str(amount))
    
    # Créer la transaction
    transaction = Transaction(
        reference=f"ADJ-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        user_id=user_id,
        wallet_id=wallet.id,
        transaction_type=TransactionType.ADJUSTMENT,
        payment_method=PaymentMethod.CASH,
        amount=Decimal(str(amount)),
        balance_before=old_balance,
        balance_after=wallet.balance,
        status=TransactionStatus.COMPLETED,
        completed_at=datetime.utcnow(),
        metadata={"admin_id": admin.id, "reason": reason}
    )
    
    db.add(transaction)
    await db.commit()
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.DEPOSIT,
        resource_type="wallet",
        resource_id=wallet.id,
        new_values={"balance": float(wallet.balance)},
        reason=reason
    )
    db.add(audit)
    await db.commit()
    
    return SuccessResponse(
        message=f"{amount} HTG crédités sur le compte de {user_id}",
        data={"new_balance": float(wallet.balance)}
    )


# ==================== PUBLIC ENDPOINTS (pour recherche) ====================

@router.get(
    "/by-phone/{phone}",
    response_model=UserResponse,
    summary="Rechercher par téléphone",
    description="Recherche un utilisateur par son numéro de téléphone (Agent/Admin)"
)
async def get_user_by_phone(
    phone: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Recherche un utilisateur par téléphone."""
    # Seuls les agents et admins peuvent rechercher
    if not (current_user.is_agent or current_user.is_admin):
        raise HTTPException(status_code=403, detail="Accès non autorisé")
    
    user_service = UserService(db, None)
    user = await user_service.get_user_by_phone(phone)
    
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur non trouvé")
    
    return user


@router.get(
    "/search/",
    response_model=List[UserResponse],
    summary="Recherche avancée",
    description="Recherche des utilisateurs par nom ou téléphone"
)
async def search_users(
    q: str = Query(..., min_length=2),
    limit: int = Query(10, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Recherche des utilisateurs (nom, téléphone, email)."""
    # Seuls les agents et admins peuvent rechercher
    if not (current_user.is_agent or current_user.is_admin):
        raise HTTPException(status_code=403, detail="Accès non autorisé")
    
    query = select(User).where(
        (User.phone.contains(q)) |
        (User.first_name.contains(q)) |
        (User.last_name.contains(q)) |
        (User.email.contains(q))
    ).where(User.is_deleted == False).limit(limit)
    
    result = await db.execute(query)
    users = result.scalars().all()
    
    return users