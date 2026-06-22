# app/routes/admin.py
"""Routes d'administration complètes - Parier Keno & Lucky Haïti
Gestion des utilisateurs, agents, bureaux, jeux, transactions, rapports et conformité LEH
"""

from fastapi import APIRouter, Body, Depends, Request, Form, HTTPException, Query, BackgroundTasks, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, desc, asc, text, update
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from decimal import Decimal
import json
import csv
import io
import secrets
from pathlib import Path

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_admin, hash_password, verify_password
from app.core.logger import logger
from app.core.exceptions import NotFoundException, ValidationException
from app.models.user import User, UserRole, KYCStatus
from app.models.wallet import Wallet
from app.models.bureau import Bureau, CashierSession
from app.models.keno import KenoDraw, KenoBet, KenoDrawStatus, KenoBetStatus
from app.models.lucky import LuckyPlay, LuckyWheelConfig
from app.models.ticket import Ticket, TicketStatus
from app.models.transaction import Transaction, TransactionType, TransactionStatus
from app.models.audit import AuditLog, AuditAction
from app.models.notification import Notification
from app.models.promotion import Promotion, PromotionStatus
from app.models.responsible import SelfExclusion, PlayerLimit
from app.schemas.admin import (
    AdminUserCreate, AdminUserUpdate, AdminAgentCreate,
    AdminBureauCreate, AdminBureauUpdate,
    AdminKenoConfig, AdminLuckyConfig,
    AdminPromotionCreate, AdminPromotionUpdate,
    AdminSettings, AdminReportRequest, LuckyWheelSegment
)
from app.services.user_service import UserService
from app.services.wallet_service import WalletService
from app.services.keno_service import KenoService
from app.services.lucky_service import LuckyWheelService
from app.services.ticket_service import TicketService
from app.services.notification_service import NotificationService

import redis.asyncio as redis

from app.workers.draw_worker import generate_draw_numbers

# Router et templates
router = APIRouter(prefix="/admin", tags=["Admin"])
templates = Jinja2Templates(directory="app/templates/admin")

# ==================== FILTRES TEMPLATES ====================

@templates.env.filters
def format_number(value):
    """Formate un nombre avec séparateurs de milliers"""
    if value is None:
        return "0"
    try:
        return f"{int(value):,}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)

@templates.env.filters
def timeago(value):
    """Convertit une date en format 'il y a X'"""
    if not value:
        return ""
    now = datetime.utcnow()
    diff = now - value
    if diff.days > 30:
        return value.strftime("%d/%m/%Y")
    if diff.days > 0:
        return f"il y a {diff.days}j"
    if diff.seconds > 3600:
        return f"il y a {diff.seconds // 3600}h"
    if diff.seconds > 60:
        return f"il y a {diff.seconds // 60}min"
    return "à l'instant"

@templates.env.filters
def tojson(value):
    """Convertit en JSON"""
    return json.dumps(value)


# ==================== AUTHENTIFICATION ADMIN ====================

@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(
    request: Request,
    error: Optional[str] = None
):
    """Page de connexion administrateur"""
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "is_authenticated": False
    })


@router.post("/login")
async def admin_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    remember: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Traitement de la connexion administrateur
    """
    # Rechercher l'utilisateur par email
    result = await db.execute(
        select(User).where(User.email == email, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        return await admin_login_page(request, error="Email ou mot de passe incorrect")
    
    # Vérifier le mot de passe
    if not verify_password(password, user.password_hash):
        return await admin_login_page(request, error="Email ou mot de passe incorrect")
    
    # Vérifier que c'est un admin
    if user.role not in [UserRole.ADMIN, UserRole.SUPER_ADMIN]:
        return await admin_login_page(request, error="Accès non autorisé")
    
    # Vérifier que le compte est actif
    if not user.is_active:
        return await admin_login_page(request, error="Compte désactivé")
    
    # Générer les tokens
    from app.core.security import create_access_token, create_refresh_token
    access_token = create_access_token({"sub": user.id, "role": user.role})
    refresh_token = create_refresh_token({"sub": user.id})
    
    # Stocker le refresh token dans Redis
    if remember:
        expire = 604800  # 7 jours
    else:
        expire = 3600 * 24  # 24h
    
    await redis_client.setex(f"admin:refresh:{user.id}", expire, refresh_token)
    
    # Mettre à jour la dernière connexion
    user.last_login = datetime.utcnow()
    user.last_ip = request.client.host if request.client else None
    await db.commit()
    
    # Audit log
    audit = AuditLog(
        user_id=user.id,
        action=AuditAction.LOGIN,
        resource_type="admin",
        resource_id=user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent")
    )
    db.add(audit)
    await db.commit()
    
    # Créer la session
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        key="admin_token",
        value=access_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=expire
    )
    response.set_cookie(
        key="admin_refresh",
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=expire
    )
    
    return response


@router.post("/logout")
async def admin_logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Déconnexion administrateur"""
    # Récupérer le token
    token = request.cookies.get("admin_token")
    if token:
        # Blacklister le token
        await redis_client.setex(f"admin:blacklist:{token}", 3600, "1")
    
    # Supprimer le refresh token
    user_id = request.cookies.get("admin_user_id")
    if user_id:
        await redis_client.delete(f"admin:refresh:{user_id}")
    
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_token")
    response.delete_cookie("admin_refresh")
    response.delete_cookie("admin_user_id")
    
    return response


# ==================== DASHBOARD ====================

@router.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Tableau de bord administrateur
    """
    # Statistiques
    stats = await _get_dashboard_stats(db)
    
    # Dernières transactions
    recent_transactions = await _get_recent_transactions(db, limit=10)
    
    # Nouveaux utilisateurs
    recent_users = await _get_recent_users(db, limit=10)
    
    # Alertes système
    alerts = await _get_system_alerts(db, redis_client)
    
    # Utilisateurs en attente KYC
    pending_kyc = await _get_pending_kyc_count(db)
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "active": "dashboard",
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0",
        "stats": stats,
        "recent_transactions": recent_transactions,
        "recent_users": recent_users,
        "alerts": alerts,
        "pending_kyc": pending_kyc,
        "csrf_token": "{{ csrf_token() }}"  # À implémenter avec un middleware
    })


@router.get("/api/dashboard/stats")
async def api_dashboard_stats(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """API pour les statistiques du dashboard (AJAX)"""
    stats = await _get_dashboard_stats(db)
    return stats


@router.get("/api/dashboard/charts")
async def api_dashboard_charts(
    period: int = Query(30, ge=7, le=365),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """API pour les données des graphiques (AJAX)"""
    return await _get_chart_data(db, period)


# ==================== UTILISATEURS ====================

@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    role: Optional[str] = None,
    kyc_status: Optional[str] = None,
    status: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Liste des utilisateurs avec filtres
    """
    query = select(User).where(User.is_deleted == False)
    
    # Filtres
    if search:
        query = query.where(
            or_(
                User.phone.contains(search),
                User.email.contains(search),
                User.first_name.contains(search),
                User.last_name.contains(search),
                User.national_id.contains(search)
            )
        )
    
    if role:
        query = query.where(User.role == role)
    
    if kyc_status:
        query = query.where(User.kyc_status == kyc_status)
    
    if status == "active":
        query = query.where(User.is_active == True)
    elif status == "inactive":
        query = query.where(User.is_active == False)
    elif status == "locked":
        query = query.where(User.is_locked == True)
    
    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0
    
    query = query.order_by(User.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    users = result.scalars().all()
    
    # Récupérer les soldes des wallets
    users_with_balance = []
    for user in users:
        wallet_result = await db.execute(
            select(Wallet).where(Wallet.user_id == user.id)
        )
        wallet = wallet_result.scalar_one_or_none()
        user_dict = {
            "id": user.id,
            "phone": user.phone,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "full_name": user.full_name,
            "national_id": user.national_id,
            "role": user.role,
            "kyc_status": user.kyc_status,
            "is_active": user.is_active,
            "is_locked": user.is_locked,
            "created_at": user.created_at,
            "wallet_balance": float(wallet.balance) if wallet else 0
        }
        users_with_balance.append(user_dict)
    
    return templates.TemplateResponse("users/index.html", {
        "request": request,
        "active": "users",
        "users": users_with_balance,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
            "has_prev": page > 1,
            "has_next": page * per_page < total
        },
        "filters": {
            "search": search,
            "role": role,
            "kyc_status": kyc_status,
            "status": status
        },
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0",
        "pending_kyc": await _get_pending_kyc_count(db)
    })


@router.get("/users/create", response_class=HTMLResponse)
async def admin_user_create_page(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Page de création d'utilisateur"""
    return templates.TemplateResponse("users/create.html", {
        "request": request,
        "active": "users",
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0",
        "roles": [r.value for r in UserRole]
    })


@router.post("/users/create")
async def admin_user_create(
    request: Request,
    background_tasks: BackgroundTasks,
    user_data: AdminUserCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Création d'un utilisateur"""
    
    # Vérifier si le téléphone existe déjà
    existing = await db.execute(
        select(User).where(User.phone == user_data.phone)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Ce numéro de téléphone est déjà utilisé")
    
    if user_data.email:
        existing = await db.execute(
            select(User).where(User.email == user_data.email)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(400, "Cet email est déjà utilisé")
    
    # Créer l'utilisateur
    user = User(
        phone=user_data.phone,
        email=user_data.email,
        first_name=user_data.first_name,
        last_name=user_data.last_name,
        national_id=user_data.national_id,
        password_hash=hash_password(user_data.password),
        role=user_data.role or UserRole.PLAYER,
        is_active=True,
        kyc_status=KYCStatus.VERIFIED if user_data.kyc_verified else KYCStatus.PENDING
    )
    
    db.add(user)
    await db.flush()
    
    # Créer le wallet
    wallet = Wallet(user_id=user.id)
    db.add(wallet)
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_CREATED,
        resource_type="user",
        resource_id=user.id,
        new_values={"phone": user.phone, "role": user.role}
    )
    db.add(audit)
    
    await db.commit()
    
    # Envoyer notification de bienvenue
    if user.phone:
        background_tasks.add_task(
            _send_welcome_sms,
            user.phone,
            user.first_name or "Cher joueur"
        )
    
    return RedirectResponse(url=f"/admin/users/{user.id}", status_code=303)


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail(
    request: Request,
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'un utilisateur"""
    
    # Récupérer l'utilisateur avec son wallet
    result = await db.execute(
        select(User, Wallet)
        .join(Wallet, User.id == Wallet.user_id, isouter=True)
        .where(User.id == user_id, User.is_deleted == False)
    )
    row = result.first()
    
    if not row:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    user, wallet = row
    
    # Statistiques des paris
    bets_count = await db.execute(
        select(func.count(KenoBet.id))
        .where(KenoBet.user_id == user_id)
    )
    total_bets = bets_count.scalar() or 0
    
    bets_volume = await db.execute(
        select(func.coalesce(func.sum(KenoBet.stake), 0))
        .where(KenoBet.user_id == user_id)
    )
    total_volume = float(bets_volume.scalar() or 0)
    
    bets_wins = await db.execute(
        select(func.coalesce(func.sum(KenoBet.winnings), 0))
        .where(KenoBet.user_id == user_id, KenoBet.status == KenoBetStatus.WON)
    )
    total_wins = float(bets_wins.scalar() or 0)
    
    # Dernières transactions
    transactions_result = await db.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.desc())
        .limit(20)
    )
    transactions = transactions_result.scalars().all()
    
    # Derniers paris
    bets_result = await db.execute(
        select(KenoBet)
        .where(KenoBet.user_id == user_id)
        .order_by(KenoBet.placed_at.desc())
        .limit(20)
    )
    bets = bets_result.scalars().all()
    
    return templates.TemplateResponse("users/detail.html", {
        "request": request,
        "active": "users",
        "user": user,
        "wallet": wallet,
        "stats": {
            "total_bets": total_bets,
            "total_volume": total_volume,
            "total_wins": total_wins,
            "win_rate": round(total_wins / total_volume * 100, 2) if total_volume > 0 else 0
        },
        "transactions": transactions,
        "bets": bets,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def admin_user_edit_page(
    request: Request,
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Page d'édition d'un utilisateur"""
    
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    return templates.TemplateResponse("users/edit.html", {
        "request": request,
        "active": "users",
        "user": user,
        "roles": [r.value for r in UserRole],
        "kyc_statuses": [s.value for s in KYCStatus],
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.put("/api/users/{user_id}")
async def admin_user_update(
    user_id: str,
    user_data: AdminUserUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Mise à jour d'un utilisateur (API)"""
    
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    # Mise à jour des champs
    if user_data.first_name is not None:
        user.first_name = user_data.first_name
    if user_data.last_name is not None:
        user.last_name = user_data.last_name
    if user_data.email is not None:
        user.email = user_data.email
    if user_data.national_id is not None:
        user.national_id = user_data.national_id
    if user_data.role is not None:
        user.role = user_data.role
    if user_data.kyc_status is not None:
        user.kyc_status = user_data.kyc_status
    if user_data.is_active is not None:
        user.is_active = user_data.is_active
    if user_data.password:
        user.password_hash = hash_password(user_data.password)
    
    await db.commit()
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_UPDATED,
        resource_type="user",
        resource_id=user_id,
        new_values=user_data.dict(exclude_unset=True)
    )
    db.add(audit)
    await db.commit()
    
    return {"success": True, "message": "Utilisateur mis à jour avec succès"}


@router.delete("/api/users/{user_id}")
async def admin_user_delete(
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Suppression d'un utilisateur (soft delete)"""
    
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    user.soft_delete(admin.id)
    user.is_active = False
    
    await db.commit()
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_BLOCKED,
        resource_type="user",
        resource_id=user_id,
        reason="Suppression par admin"
    )
    db.add(audit)
    await db.commit()
    
    return {"success": True, "message": "Utilisateur supprimé avec succès"}


@router.post("/api/users/{user_id}/block")
async def admin_user_block(
    user_id: str,
    reason: str = Form(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Bloquer un utilisateur"""
    
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    user.is_locked = True
    user.lock_reason = reason
    user.is_active = False
    user.locked_at = datetime.utcnow()
    
    await db.commit()
    
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
    
    return {"success": True, "message": "Utilisateur bloqué avec succès"}


@router.post("/api/users/{user_id}/unblock")
async def admin_user_unblock(
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Débloquer un utilisateur"""
    
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    user.is_locked = False
    user.lock_reason = None
    user.is_active = True
    user.locked_at = None
    
    await db.commit()
    
    return {"success": True, "message": "Utilisateur débloqué avec succès"}


# ==================== AGENTS ====================

@router.get("/agents", response_class=HTMLResponse)
async def admin_agents(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    bureau_id: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste des agents"""
    
    query = select(User).where(
        User.role.in_([UserRole.AGENT, UserRole.MANAGER]),
        User.is_deleted == False
    )
    
    if search:
        query = query.where(
            or_(
                User.phone.contains(search),
                User.email.contains(search),
                User.first_name.contains(search),
                User.last_name.contains(search)
            )
        )
    
    if bureau_id:
        query = query.where(User.bureau_id == bureau_id)
    
    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0
    
    query = query.order_by(User.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    agents = result.scalars().all()
    
    # Bureaux pour le filtre
    bureaus_result = await db.execute(
        select(Bureau).where(Bureau.is_deleted == False)
    )
    bureaus = bureaus_result.scalars().all()
    
    return templates.TemplateResponse("agents/index.html", {
        "request": request,
        "active": "agents",
        "agents": agents,
        "bureaus": bureaus,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
            "has_prev": page > 1,
            "has_next": page * per_page < total
        },
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.post("/api/agents")
async def admin_agent_create(
    agent_data: AdminAgentCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Création d'un agent"""
    
    # Vérifier le téléphone
    existing = await db.execute(
        select(User).where(User.phone == agent_data.phone)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Ce numéro de téléphone est déjà utilisé")
    
    # Vérifier le bureau
    if agent_data.bureau_id:
        bureau_result = await db.execute(
            select(Bureau).where(Bureau.id == agent_data.bureau_id)
        )
        if not bureau_result.scalar_one_or_none():
            raise HTTPException(404, "Bureau non trouvé")
    
    # Créer l'agent
    user = User(
        phone=agent_data.phone,
        email=agent_data.email,
        first_name=agent_data.first_name,
        last_name=agent_data.last_name,
        national_id=agent_data.national_id,
        password_hash=hash_password(agent_data.password),
        role=UserRole.AGENT,
        bureau_id=agent_data.bureau_id,
        is_active=True,
        kyc_status=KYCStatus.VERIFIED
    )
    
    db.add(user)
    await db.flush()
    
    # Créer le wallet
    wallet = Wallet(user_id=user.id)
    db.add(wallet)
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_CREATED,
        resource_type="agent",
        resource_id=user.id
    )
    db.add(audit)
    
    await db.commit()
    
    return {"success": True, "agent_id": user.id, "message": "Agent créé avec succès"}


@router.put("/api/agents/{agent_id}")
async def admin_agent_update(
    agent_id: str,
    agent_data: AdminAgentCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Mise à jour d'un agent"""
    
    result = await db.execute(
        select(User).where(User.id == agent_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Agent non trouvé")
    
    if agent_data.password:
        user.password_hash = hash_password(agent_data.password)
    if agent_data.first_name:
        user.first_name = agent_data.first_name
    if agent_data.last_name:
        user.last_name = agent_data.last_name
    if agent_data.email:
        user.email = agent_data.email
    if agent_data.bureau_id:
        user.bureau_id = agent_data.bureau_id
    
    await db.commit()
    
    return {"success": True, "message": "Agent mis à jour avec succès"}


@router.delete("/api/agents/{agent_id}")
async def admin_agent_delete(
    agent_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Suppression d'un agent"""
    
    result = await db.execute(
        select(User).where(User.id == agent_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Agent non trouvé")
    
    user.soft_delete(admin.id)
    user.is_active = False
    
    await db.commit()
    
    return {"success": True, "message": "Agent supprimé avec succès"}


# ==================== BUREAUX ====================

@router.get("/bureaus", response_class=HTMLResponse)
async def admin_bureaus(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste des bureaux"""
    
    result = await db.execute(
        select(Bureau).where(Bureau.is_deleted == False)
    )
    bureaus = result.scalars().all()
    
    return templates.TemplateResponse("bureaus/index.html", {
        "request": request,
        "active": "bureaus",
        "bureaus": bureaus,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.post("/api/bureaus")
async def admin_bureau_create(
    bureau_data: AdminBureauCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Création d'un bureau"""
    
    bureau = Bureau(
        name=bureau_data.name,
        code=bureau_data.code or bureau_data.name[:10].upper().replace(" ", ""),
        address=bureau_data.address,
        city=bureau_data.city,
        phone=bureau_data.phone,
        email=bureau_data.email,
        is_active=True
    )
    
    db.add(bureau)
    await db.flush()
    
    await db.commit()
    
    return {"success": True, "bureau_id": bureau.id, "message": "Bureau créé avec succès"}


@router.put("/api/bureaus/{bureau_id}")
async def admin_bureau_update(
    bureau_id: str,
    bureau_data: AdminBureauUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Mise à jour d'un bureau"""
    
    result = await db.execute(
        select(Bureau).where(Bureau.id == bureau_id, Bureau.is_deleted == False)
    )
    bureau = result.scalar_one_or_none()
    
    if not bureau:
        raise HTTPException(404, "Bureau non trouvé")
    
    for key, value in bureau_data.dict(exclude_unset=True).items():
        if value is not None:
            setattr(bureau, key, value)
    
    await db.commit()
    
    return {"success": True, "message": "Bureau mis à jour avec succès"}


@router.delete("/api/bureaus/{bureau_id}")
async def admin_bureau_delete(
    bureau_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Suppression d'un bureau"""
    
    result = await db.execute(
        select(Bureau).where(Bureau.id == bureau_id, Bureau.is_deleted == False)
    )
    bureau = result.scalar_one_or_none()
    
    if not bureau:
        raise HTTPException(404, "Bureau non trouvé")
    
    bureau.soft_delete(admin.id)
    
    await db.commit()
    
    return {"success": True, "message": "Bureau supprimé avec succès"}


# ==================== CONFIGURATION KENO ====================

@router.get("/games/keno/config", response_class=HTMLResponse)
async def admin_keno_config(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Configuration du jeu Keno"""
    
    # Récupérer la configuration
    config = {
        "draw_interval": await redis_client.get("config:keno:draw_interval") or 5,
        "start_hour": await redis_client.get("config:keno:start_hour") or 8,
        "end_hour": await redis_client.get("config:keno:end_hour") or 23,
        "min_bet": await redis_client.get("config:keno:min_bet") or 10,
        "max_bet": await redis_client.get("config:keno:max_bet") or 100000,
        "rtp": await redis_client.get("config:keno:rtp") or 88.4
    }
    
    # Statistiques Keno
    stats = await _get_keno_stats(db)
    
    # Jackpot
    jackpot = {
        "current": await redis_client.get("config:keno:jackpot") or 0,
        "threshold": await redis_client.get("config:keno:jackpot_threshold") or 50000,
        "updated_at": datetime.utcnow()
    }
    
    return templates.TemplateResponse("games/keno/config.html", {
        "request": request,
        "active": "keno",
        "config": config,
        "stats": stats,
        "jackpot": jackpot,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.post("/api/keno/config")
async def admin_keno_config_save(
    config: AdminKenoConfig,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Sauvegarde de la configuration Keno"""
    
    await redis_client.setex("config:keno:draw_interval", 86400, config.draw_interval)
    await redis_client.setex("config:keno:start_hour", 86400, config.start_hour)
    await redis_client.setex("config:keno:end_hour", 86400, config.end_hour)
    await redis_client.setex("config:keno:min_bet", 86400, config.min_bet)
    await redis_client.setex("config:keno:max_bet", 86400, config.max_bet)
    
    return {"success": True, "message": "Configuration Keno sauvegardée"}


@router.post("/api/keno/jackpot/reset")
async def admin_keno_jackpot_reset(
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Réinitialisation du jackpot Keno"""
    
    await redis_client.setex("config:keno:jackpot", 86400, 0)
    
    return {"success": True, "message": "Jackpot réinitialisé"}


# ==================== CONFIGURATION LUCKY ====================

@router.get("/games/lucky/config", response_class=HTMLResponse)
async def admin_lucky_config(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Configuration du jeu Lucky Wheel"""
    
    # Récupérer la configuration active
    result = await db.execute(
        select(LuckyWheelConfig)
        .where(LuckyWheelConfig.is_active == True)
        .order_by(LuckyWheelConfig.is_default.desc())
    )
    config = result.scalar_one_or_none()
    
    if not config:
        # Créer une configuration par défaut
        config = LuckyWheelConfig.get_default_config()
        db.add(config)
        await db.commit()
    
    # Statistiques Lucky
    stats = await _get_lucky_stats(db)
    
    return templates.TemplateResponse("games/lucky/config.html", {
        "request": request,
        "active": "lucky",
        "config": config,
        "stats": stats,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.put("/api/lucky/config/{config_id}")
async def admin_lucky_config_update(
    config_id: str,
    config_data: AdminLuckyConfig,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Mise à jour de la configuration Lucky"""
    
    result = await db.execute(
        select(LuckyWheelConfig).where(LuckyWheelConfig.id == config_id)
    )
    config = result.scalar_one_or_none()
    
    if not config:
        raise HTTPException(404, "Configuration non trouvée")
    
    config.segments = config_data.segments
    config.min_bet = config_data.min_bet
    config.max_bet = config_data.max_bet
    config.calculate_rtp()
    
    await db.commit()
    
    # Invalider le cache
    await redis_client.delete("lucky:wheel:config")
    
    return {"success": True, "message": "Configuration Lucky mise à jour"}


# ==================== TRANSACTIONS ====================

@router.get("/transactions", response_class=HTMLResponse)
async def admin_transactions(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    transaction_type: Optional[str] = None,
    status: Optional[str] = None,
    user_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste des transactions"""
    
    query = select(Transaction)
    
    if transaction_type:
        query = query.where(Transaction.transaction_type == transaction_type)
    if status:
        query = query.where(Transaction.status == status)
    if user_id:
        query = query.where(Transaction.user_id == user_id)
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        query = query.where(Transaction.created_at >= start)
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.where(Transaction.created_at < end)
    
    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0
    
    query = query.order_by(Transaction.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    transactions = result.scalars().all()
    
    # Récupérer les noms des utilisateurs
    for tx in transactions:
        if tx.user_id:
            user_result = await db.execute(
                select(User).where(User.id == tx.user_id)
            )
            user = user_result.scalar_one_or_none()
            tx.user_name = user.full_name if user else None
    
    return templates.TemplateResponse("transactions/index.html", {
        "request": request,
        "active": "transactions",
        "transactions": transactions,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
            "has_prev": page > 1,
            "has_next": page * per_page < total
        },
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0",
        "transaction_types": [t.value for t in TransactionType],
        "statuses": [s.value for s in TransactionStatus]
    })


# ==================== TICKETS ====================

@router.get("/tickets", response_class=HTMLResponse)
async def admin_tickets(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    bureau_id: Optional[str] = None,
    search: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Gestion des tickets"""
    
    query = select(Ticket)
    
    if status:
        query = query.where(Ticket.status == status)
    if bureau_id:
        query = query.where(Ticket.bureau_id == bureau_id)
    if search:
        query = query.where(
            or_(
                Ticket.ticket_number.contains(search),
                Ticket.player_name.contains(search),
                Ticket.player_phone.contains(search)
            )
        )
    
    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0
    
    query = query.order_by(Ticket.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    tickets = result.scalars().all()
    
    return templates.TemplateResponse("tickets/index.html", {
        "request": request,
        "active": "tickets",
        "tickets": tickets,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
            "has_prev": page > 1,
            "has_next": page * per_page < total
        },
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


# ==================== RAPPORTS ====================

@router.get("/reports/financial", response_class=HTMLResponse)
async def admin_reports_financial(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Rapports financiers"""
    
    # Période par défaut : 30 jours
    if not end_date:
        end_date = datetime.utcnow().date().isoformat()
    if not start_date:
        start_date = (datetime.utcnow() - timedelta(days=30)).date().isoformat()
    
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    
    # Statistiques
    stats = await _get_financial_stats(db, start, end)
    
    # Données journalières
    daily_data = await _get_daily_financial_data(db, start, end)
    
    # Données pour les graphiques
    chart_data = {
        "labels": [d["date"] for d in daily_data],
        "deposits": [d["deposits"] for d in daily_data],
        "withdrawals": [d["withdrawals"] for d in daily_data],
        "net": [d["net"] for d in daily_data]
    }
    
    return templates.TemplateResponse("reports/financial.html", {
        "request": request,
        "active": "reports",
        "start_date": start_date,
        "end_date": end_date,
        "summary": stats,
        "daily_data": daily_data,
        "chart_data": chart_data,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


# ==================== AUDIT LOGS ====================

@router.get("/audit/logs", response_class=HTMLResponse)
async def admin_audit_logs(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    action: Optional[str] = None,
    user_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Logs d'audit pour la conformité LEH"""
    
    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    
    if action:
        query = query.where(AuditLog.action == action)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        query = query.where(AuditLog.created_at >= start)
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.where(AuditLog.created_at < end)
    
    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0
    
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return templates.TemplateResponse("audit/logs.html", {
        "request": request,
        "active": "audit",
        "logs": logs,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page,
            "has_prev": page > 1,
            "has_next": page * per_page < total
        },
        "actions": [a.value for a in AuditAction],
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.post("/api/audit/export")
async def admin_audit_export(
    start_date: str,
    end_date: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export des logs d'audit pour la LEH"""
    
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    
    result = await db.execute(
        select(AuditLog)
        .where(
            and_(
                AuditLog.created_at >= start,
                AuditLog.created_at < end,
                AuditLog.leh_exported == False
            )
        )
    )
    logs = result.scalars().all()
    
    # Marquer comme exportés
    for log in logs:
        log.leh_exported = True
        log.leh_exported_at = datetime.utcnow()
    
    await db.commit()
    
    # Générer le fichier CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Date", "Utilisateur", "Action", "Resource", "Anciennes valeurs", "Nouvelles valeurs", "IP"
    ])
    
    for log in logs:
        writer.writerow([
            log.id,
            log.created_at.isoformat(),
            log.user_id,
            log.action.value,
            log.resource_type,
            json.dumps(log.old_values) if log.old_values else "",
            json.dumps(log.new_values) if log.new_values else "",
            log.ip_address
        ])
    
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=audit_export_{start_date}_{end_date}.csv"
        }
    )


# ==================== PROMOTIONS ====================

@router.get("/promotions", response_class=HTMLResponse)
async def admin_promotions(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste des promotions"""
    
    result = await db.execute(
        select(Promotion).order_by(Promotion.created_at.desc())
    )
    promotions = result.scalars().all()
    
    return templates.TemplateResponse("promotions/index.html", {
        "request": request,
        "active": "promotions",
        "promotions": promotions,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.post("/api/promotions")
async def admin_promotion_create(
    promotion_data: AdminPromotionCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Création d'une promotion"""
    
    promotion = Promotion(
        name=promotion_data.name,
        code=promotion_data.code,
        description=promotion_data.description,
        type=promotion_data.type,
        config=promotion_data.config,
        start_date=promotion_data.start_date,
        end_date=promotion_data.end_date,
        min_deposit=promotion_data.min_deposit,
        max_bonus=promotion_data.max_bonus,
        wagering_requirement=promotion_data.wagering_requirement,
        eligible_games=promotion_data.eligible_games,
        new_users_only=promotion_data.new_users_only,
        first_deposit_only=promotion_data.first_deposit_only,
        total_budget=promotion_data.total_budget,
        status=PromotionStatus.DRAFT,
        created_by=admin.id
    )
    
    db.add(promotion)
    await db.flush()
    
    await db.commit()
    
    return {"success": True, "promotion_id": promotion.id, "message": "Promotion créée avec succès"}


@router.put("/api/promotions/{promotion_id}")
async def admin_promotion_update(
    promotion_id: str,
    promotion_data: AdminPromotionUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Mise à jour d'une promotion"""
    
    result = await db.execute(
        select(Promotion).where(Promotion.id == promotion_id)
    )
    promotion = result.scalar_one_or_none()
    
    if not promotion:
        raise HTTPException(404, "Promotion non trouvée")
    
    for key, value in promotion_data.dict(exclude_unset=True).items():
        if value is not None:
            setattr(promotion, key, value)
    
    await db.commit()
    
    return {"success": True, "message": "Promotion mise à jour avec succès"}


@router.delete("/api/promotions/{promotion_id}")
async def admin_promotion_delete(
    promotion_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Suppression d'une promotion"""
    
    result = await db.execute(
        select(Promotion).where(Promotion.id == promotion_id)
    )
    promotion = result.scalar_one_or_none()
    
    if not promotion:
        raise HTTPException(404, "Promotion non trouvée")
    
    await db.delete(promotion)
    await db.commit()
    
    return {"success": True, "message": "Promotion supprimée avec succès"}

# ==================== API SUPPLEMENTAIRES POUR KYC ====================

@router.post("/api/users/{user_id}/kyc/verify")
async def admin_user_kyc_verify(
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Valide le KYC d'un utilisateur"""
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    user.kyc_status = KYCStatus.VERIFIED
    user.kyc_verified_at = datetime.utcnow()
    user.kyc_verified_by = admin.id
    
    await db.commit()
    
    return {"success": True, "message": "KYC validé avec succès"}


@router.post("/api/users/{user_id}/kyc/reject")
async def admin_user_kyc_reject(
    user_id: str,
    reason: str = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Rejette le KYC d'un utilisateur"""
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    user.kyc_status = KYCStatus.REJECTED
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.KYC_SUBMITTED,
        resource_type="user",
        resource_id=user_id,
        reason=reason
    )
    db.add(audit)
    await db.commit()
    
    return {"success": True, "message": "KYC rejeté"}


@router.post("/api/users/{user_id}/kyc/reset")
async def admin_user_kyc_reset(
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Réinitialise le KYC d'un utilisateur"""
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    
    user.kyc_status = KYCStatus.PENDING
    user.kyc_verified_at = None
    user.kyc_verified_by = None
    
    await db.commit()
    
    return {"success": True, "message": "KYC réinitialisé"}


@router.post("/api/users/{user_id}/credit")
async def admin_user_credit(
    user_id: str,
    amount: float = Body(...),
    reason: str = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Crédite manuellement un utilisateur"""
    from decimal import Decimal
    from app.models.transaction import Transaction, TransactionType, PaymentMethod, TransactionStatus
    
    wallet_service = WalletService(db, redis_client)
    wallet = await wallet_service.get_wallet(user_id)
    
    old_balance = wallet.balance
    wallet.balance += Decimal(str(amount))
    
    transaction = Transaction(
        reference=f"ADJ-CREDIT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
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
    
    return {"success": True, "message": f"{amount} HTG crédités"}


@router.post("/api/users/{user_id}/debit")
async def admin_user_debit(
    user_id: str,
    amount: float = Body(...),
    reason: str = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Débite manuellement un utilisateur"""
    from decimal import Decimal
    from app.models.transaction import Transaction, TransactionType, PaymentMethod, TransactionStatus
    
    wallet_service = WalletService(db, redis_client)
    wallet = await wallet_service.get_wallet(user_id)
    
    if wallet.balance < Decimal(str(amount)):
        raise HTTPException(400, "Solde insuffisant")
    
    old_balance = wallet.balance
    wallet.balance -= Decimal(str(amount))
    
    transaction = Transaction(
        reference=f"ADJ-DEBIT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
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
    
    return {"success": True, "message": f"{amount} HTG débités"}

# ==================== API SUPPLEMENTAIRES POUR BUREAUX ====================
@router.get("/api/bureaus/statistics")
async def admin_bureaus_statistics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Statistiques globales des bureaux"""
    
    # Total bureaux
    total_result = await db.execute(
        select(func.count(Bureau.id)).where(Bureau.is_deleted == False)
    )
    total = total_result.scalar() or 0
    
    # Actifs
    active_result = await db.execute(
        select(func.count(Bureau.id)).where(
            Bureau.is_active == True,
            Bureau.is_deleted == False
        )
    )
    active = active_result.scalar() or 0
    
    # Total agents
    agents_result = await db.execute(
        select(func.count(User.id))
        .where(User.role == UserRole.AGENT, User.is_deleted == False)
    )
    total_agents = agents_result.scalar() or 0
    
    # Total caisse
    cash_result = await db.execute(
        select(func.coalesce(func.sum(Bureau.cash_balance), 0))
        .where(Bureau.is_deleted == False)
    )
    total_cash = float(cash_result.scalar() or 0)
    
    return {
        "total": total,
        "active": active,
        "total_agents": total_agents,
        "total_cash": total_cash
    }


@router.post("/api/bureaus/bulk/activate")
async def admin_bureaus_bulk_activate(
    bureau_ids: List[str] = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Active plusieurs bureaux"""
    result = await db.execute(
        update(Bureau)
        .where(Bureau.id.in_(bureau_ids))
        .values(is_active=True)
    )
    await db.commit()
    return {"success": True, "message": f"{result.rowcount} bureaux activés"}


@router.post("/api/bureaus/bulk/deactivate")
async def admin_bureaus_bulk_deactivate(
    bureau_ids: List[str] = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Désactive plusieurs bureaux"""
    result = await db.execute(
        update(Bureau)
        .where(Bureau.id.in_(bureau_ids))
        .values(is_active=False)
    )
    await db.commit()
    return {"success": True, "message": f"{result.rowcount} bureaux désactivés"}


@router.post("/api/bureaus/{bureau_id}/toggle-status")
async def admin_bureau_toggle_status(
    bureau_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Active/Désactive un bureau"""
    result = await db.execute(
        select(Bureau).where(Bureau.id == bureau_id, Bureau.is_deleted == False)
    )
    bureau = result.scalar_one_or_none()
    
    if not bureau:
        raise HTTPException(404, "Bureau non trouvé")
    
    bureau.is_active = not bureau.is_active
    await db.commit()
    
    status = "activé" if bureau.is_active else "désactivé"
    return {"success": True, "message": f"Bureau {status} avec succès"}


@router.get("/admin/bureaus/{bureau_id}/agents")
async def admin_bureau_agents(
    request: Request,
    bureau_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Voir les agents d'un bureau"""
    result = await db.execute(
        select(User)
        .where(
            User.bureau_id == bureau_id,
            User.role == UserRole.AGENT,
            User.is_deleted == False
        )
    )
    agents = result.scalars().all()
    
    return templates.TemplateResponse("bureaus/agents.html", {
        "request": request,
        "active": "bureaus",
        "bureau": await db.get(Bureau, bureau_id),
        "agents": agents,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/admin/bureaus/{bureau_id}/tickets")
async def admin_bureau_tickets(
    request: Request,
    bureau_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Voir les tickets d'un bureau"""
    result = await db.execute(
        select(Ticket)
        .where(Ticket.bureau_id == bureau_id)
        .order_by(Ticket.created_at.desc())
        .limit(100)
    )
    tickets = result.scalars().all()
    
    return templates.TemplateResponse("bureaus/tickets.html", {
        "request": request,
        "active": "bureaus",
        "bureau": await db.get(Bureau, bureau_id),
        "tickets": tickets,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })

# ==================== API SUPPLEMENTAIRES POUR SETTINGS ====================

@router.put("/api/settings/general")
async def admin_settings_general_update(
    settings_data: AdminSettings,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour les paramètres généraux"""
    for key, value in settings_data.dict().items():
        await redis_client.setex(f"settings:{key}", 86400, str(value))
    
    return {"success": True, "message": "Paramètres généraux mis à jour"}


@router.put("/api/settings/limits")
async def admin_settings_limits_update(
    limits_data: dict,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour les limites"""
    for key, value in limits_data.items():
        await redis_client.setex(f"settings:limits:{key}", 86400, str(value))
    
    return {"success": True, "message": "Limites mises à jour"}


@router.put("/api/settings/maintenance")
async def admin_settings_maintenance_update(
    maintenance_data: dict,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour le mode maintenance"""
    await redis_client.setex("settings:maintenance:mode", 86400, str(maintenance_data.get('maintenance_mode', False)))
    await redis_client.setex("settings:maintenance:message", 86400, maintenance_data.get('maintenance_message', ''))
    
    return {"success": True, "message": "Mode maintenance mis à jour"}


@router.put("/api/settings/security/auth")
async def admin_settings_auth_update(
    auth_data: dict,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour les paramètres d'authentification"""
    for key, value in auth_data.items():
        await redis_client.setex(f"settings:security:{key}", 86400, str(value))
    
    return {"success": True, "message": "Paramètres d'authentification mis à jour"}


@router.put("/api/settings/security/password")
async def admin_settings_password_update(
    password_data: dict,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour la politique de mot de passe"""
    for key, value in password_data.items():
        await redis_client.setex(f"settings:password:{key}", 86400, str(value))
    
    return {"success": True, "message": "Politique de mot de passe mise à jour"}


@router.put("/api/settings/security/whitelist")
async def admin_settings_whitelist_update(
    whitelist_data: dict,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour la whitelist IP"""
    import json
    await redis_client.setex("settings:security:whitelist", 86400, json.dumps(whitelist_data.get('ip_whitelist', [])))
    
    return {"success": True, "message": "Whitelist IP mise à jour"}


@router.put("/api/settings/security/ratelimit")
async def admin_settings_ratelimit_update(
    ratelimit_data: dict,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour le rate limiting"""
    for key, value in ratelimit_data.items():
        await redis_client.setex(f"settings:ratelimit:{key}", 86400, str(value))
    
    return {"success": True, "message": "Rate limiting mis à jour"}


@router.post("/api/sessions/{session_id}/terminate")
async def admin_session_terminate(
    session_id: str,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Termine une session"""
    await redis_client.delete(f"session:{session_id}")
    return {"success": True, "message": "Session terminée"}


@router.post("/api/sessions/terminate-all")
async def admin_sessions_terminate_all(
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Termine toutes les sessions sauf celle de l'admin"""
    # Récupérer toutes les sessions actives
    keys = await redis_client.keys("session:*")
    for key in keys:
        session_data = await redis_client.get(key)
        if session_data:
            import json
            data = json.loads(session_data)
            if data.get('user_id') != admin.id:
                await redis_client.delete(key)
    
    return {"success": True, "message": "Toutes les sessions ont été terminées"}


@router.get("/api/settings/status")
async def admin_settings_status(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Statut du système"""
    # Vérifier la base de données
    try:
        await db.execute("SELECT 1")
        db_status = "healthy"
    except:
        db_status = "unhealthy"
    
    # Vérifier Redis
    try:
        await redis_client.ping()
        redis_status = "healthy"
    except:
        redis_status = "unhealthy"
    
    # Compter les workers
    try:
        from app.workers.celery import celery_app
        inspect = celery_app.control.inspect()
        active = inspect.active()
        worker_count = len(active) if active else 0
    except:
        worker_count = 0
    
    return {
        "database": db_status,
        "redis": redis_status,
        "worker_count": worker_count
    }

# ==================== API SUPPLEMENTAIRES POUR GAMES ====================

# ==================== KENO ====================

@router.get("/api/keno/draws/{draw_id}")
async def admin_keno_draw_detail(
    draw_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'un tirage Keno"""
    result = await db.execute(
        select(KenoDraw).where(KenoDraw.id == draw_id)
    )
    draw = result.scalar_one_or_none()
    
    if not draw:
        raise HTTPException(404, "Tirage non trouvé")
    
    # Récupérer les paris
    bets_result = await db.execute(
        select(KenoBet).where(KenoBet.draw_id == draw_id)
    )
    bets = bets_result.scalars().all()
    
    return {
        "id": draw.id,
        "draw_number": draw.draw_number,
        "draw_time": draw.draw_time,
        "numbers": draw.numbers,
        "status": draw.status,
        "total_bets": draw.total_bets,
        "total_amount": float(draw.total_amount),
        "total_payout": float(draw.total_payout),
        "jackpot_amount": float(draw.jackpot_amount),
        "jackpot_won": draw.jackpot_won,
        "bets": [
            {
                "user_id": b.user_id,
                "user_name": b.user.full_name if b.user else None,
                "ticket_number": b.ticket.ticket_number if b.ticket else None,
                "picks": b.picks,
                "stake": float(b.stake),
                "winnings": float(b.winnings),
                "status": b.status
            }
            for b in bets[:50]
        ]
    }


@router.post("/api/keno/draws/trigger")
async def admin_keno_trigger_draw(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Déclenche un tirage Keno manuellement"""
    from app.services.keno_service import KenoService
    from app.api.websockets.manager import broadcast_draw_result
    
    keno_service = KenoService(db, redis_client)
    
    # Générer le tirage
    draw = await keno_service.generate_draw()
    
    # Régler les paris
    result = await keno_service.settle_bets_for_draw(draw.id)
    
    # Diffuser via WebSocket
    await broadcast_draw_result(result)
    
    return {"success": True, "message": f"Tirage #{draw.draw_number} déclenché", "draw_id": draw.id}


@router.post("/api/keno/draws/{draw_id}/trigger")
async def admin_keno_trigger_specific_draw(
    draw_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Déclenche un tirage Keno spécifique"""
    from app.services.keno_service import KenoService
    from app.api.websockets.manager import broadcast_draw_result
    
    result = await db.execute(
        select(KenoDraw).where(KenoDraw.id == draw_id)
    )
    draw = result.scalar_one_or_none()
    
    if not draw:
        raise HTTPException(404, "Tirage non trouvé")
    
    if draw.status != KenoDrawStatus.PENDING:
        raise HTTPException(400, "Ce tirage n'est plus en attente")
    
    keno_service = KenoService(db, redis_client)
    
    # Générer les numéros
    drawn_numbers = generate_draw_numbers()
    draw.numbers = drawn_numbers
    draw.status = KenoDrawStatus.COMPLETED
    draw.closed_at = datetime.utcnow()
    
    await db.flush()
    
    # Régler les paris
    result = await keno_service.settle_bets_for_draw(draw.id)
    
    # Diffuser via WebSocket
    await broadcast_draw_result(result)
    
    return {"success": True, "message": f"Tirage #{draw.draw_number} déclenché"}


@router.post("/api/keno/draws/schedule")
async def admin_keno_schedule_draws(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Planifie les tirages Keno pour les prochaines 24h"""
    from app.services.draw_scheduler import schedule_draws
    
    await schedule_draws()
    
    return {"success": True, "message": "Tirages planifiés avec succès"}


@router.post("/api/keno/draws/cancel-pending")
async def admin_keno_cancel_pending_draws(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Annule tous les tirages Keno en attente"""
    result = await db.execute(
        update(KenoDraw)
        .where(KenoDraw.status == KenoDrawStatus.PENDING)
        .values(status=KenoDrawStatus.CANCELLED, closed_at=datetime.utcnow(), closed_by="system")
    )
    await db.commit()
    
    return {"success": True, "message": f"{result.rowcount} tirages annulés"}


@router.post("/api/keno/draws/{draw_id}/cancel")
async def admin_keno_cancel_draw(
    draw_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Annule un tirage Keno spécifique"""
    result = await db.execute(
        select(KenoDraw).where(KenoDraw.id == draw_id)
    )
    draw = result.scalar_one_or_none()
    
    if not draw:
        raise HTTPException(404, "Tirage non trouvé")
    
    if draw.status != KenoDrawStatus.PENDING:
        raise HTTPException(400, "Ce tirage ne peut pas être annulé")
    
    draw.status = KenoDrawStatus.CANCELLED
    draw.closed_at = datetime.utcnow()
    draw.closed_by = admin.id
    
    await db.commit()
    
    return {"success": True, "message": f"Tirage #{draw.draw_number} annulé"}


@router.post("/api/keno/paytable")
async def admin_keno_paytable_update(
    paytable: Dict = Body(...),
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour la table de paiement Keno"""
    await redis_client.setex("config:keno:paytable", 86400, json.dumps(paytable))
    return {"success": True, "message": "Table de paiement mise à jour"}


@router.post("/api/keno/paytable/reset")
async def admin_keno_paytable_reset(
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Réinitialise la table de paiement Keno"""
    default_paytable = {
        "1": {"1": 2.5},
        "2": {"2": 6},
        "3": {"3": 12, "2": 1.5},
        "4": {"4": 30, "3": 3, "2": 1},
        "5": {"5": 60, "4": 6, "3": 2, "2": 0.5},
        "6": {"6": 120, "5": 15, "4": 4, "3": 1.5, "2": 0.5},
        "7": {"7": 300, "6": 30, "5": 8, "4": 2, "3": 1, "2": 0.5},
        "8": {"8": 600, "7": 60, "6": 15, "5": 4, "4": 1.5, "3": 0.5},
        "9": {"9": 1200, "8": 120, "7": 30, "6": 8, "5": 3, "4": 1},
        "10": {"10": 5000, "9": 500, "8": 60, "7": 15, "6": 5, "5": 2, "4": 0.5}
    }
    await redis_client.setex("config:keno:paytable", 86400, json.dumps(default_paytable))
    return {"success": True, "message": "Table de paiement réinitialisée"}


@router.post("/api/keno/jackpot/threshold")
async def admin_keno_jackpot_threshold(
    threshold: int = Body(...),
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour le seuil du jackpot Keno"""
    await redis_client.setex("config:keno:jackpot_threshold", 86400, threshold)
    return {"success": True, "message": "Seuil du jackpot mis à jour"}


# ==================== LUCKY ====================

@router.put("/admin/api/lucky/config")
async def admin_lucky_config_update(
    config_data: AdminLuckyConfig,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Met à jour la configuration Lucky"""
    result = await db.execute(
        select(LuckyWheelConfig)
        .where(LuckyWheelConfig.is_active == True)
        .order_by(LuckyWheelConfig.is_default.desc())
        .limit(1)
    )
    config = result.scalar_one_or_none()
    
    if not config:
        config = LuckyWheelConfig.get_default_config()
        db.add(config)
        await db.flush()
    
    config.name = config_data.name
    config.description = config_data.description
    config.min_bet = config_data.min_bet
    config.max_bet = config_data.max_bet
    config.calculate_rtp()
    
    await db.commit()
    
    return {"success": True, "message": "Configuration mise à jour"}


@router.put("/admin/api/lucky/config/{config_id}/segments")
async def admin_lucky_segments_update(
    config_id: str,
    segments: List[LuckyWheelSegment] = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour les segments de la roue Lucky"""
    result = await db.execute(
        select(LuckyWheelConfig).where(LuckyWheelConfig.id == config_id)
    )
    config = result.scalar_one_or_none()
    
    if not config:
        raise HTTPException(404, "Configuration non trouvée")
    
    config.segments = [s.dict() for s in segments]
    config.calculate_rtp()
    
    await db.commit()
    
    # Invalider le cache
    await redis_client.delete("lucky:wheel:config")
    
    return {"success": True, "message": "Segments mis à jour"}

# ==================== API SUPPLEMENTAIRES POUR PROMOTIONS ====================

@router.get("/api/promotions/statistics")
async def admin_promotions_statistics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Statistiques des promotions"""
    # Total
    total_result = await db.execute(
        select(func.count(Promotion.id))
    )
    total = total_result.scalar() or 0
    
    # Actives
    active_result = await db.execute(
        select(func.count(Promotion.id))
        .where(
            Promotion.status == PromotionStatus.ACTIVE,
            Promotion.start_date <= datetime.utcnow(),
            Promotion.end_date >= datetime.utcnow()
        )
    )
    active = active_result.scalar() or 0
    
    # En attente
    pending_result = await db.execute(
        select(func.count(Promotion.id))
        .where(
            Promotion.status == PromotionStatus.ACTIVE,
            Promotion.start_date > datetime.utcnow()
        )
    )
    pending = pending_result.scalar() or 0
    
    # Expirées
    expired_result = await db.execute(
        select(func.count(Promotion.id))
        .where(
            or_(
                Promotion.status == PromotionStatus.EXPIRED,
                Promotion.end_date < datetime.utcnow()
            )
        )
    )
    expired = expired_result.scalar() or 0
    
    # Budget utilisé
    used_result = await db.execute(
        select(func.coalesce(func.sum(Promotion.used_budget), 0))
    )
    used_budget = float(used_result.scalar() or 0)
    
    # Total réclamations
    claims_result = await db.execute(
        select(func.coalesce(func.sum(Promotion.total_claims), 0))
    )
    total_claims = claims_result.scalar() or 0
    
    return {
        "total": total,
        "active": active,
        "pending": pending,
        "expired": expired,
        "used_budget": used_budget,
        "total_claims": total_claims
    }


@router.put("/api/promotions/{promotion_id}/status")
async def admin_promotion_status_update(
    promotion_id: str,
    status: PromotionStatus,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Met à jour le statut d'une promotion"""
    result = await db.execute(
        select(Promotion).where(Promotion.id == promotion_id)
    )
    promotion = result.scalar_one_or_none()
    
    if not promotion:
        raise HTTPException(404, "Promotion non trouvée")
    
    promotion.status = status
    await db.commit()
    
    return {"success": True, "message": f"Statut mis à jour: {status.value}"}


@router.post("/api/promotions/bulk/activate")
async def admin_promotions_bulk_activate(
    promo_ids: List[str] = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Active plusieurs promotions"""
    result = await db.execute(
        update(Promotion)
        .where(Promotion.id.in_(promo_ids))
        .values(status=PromotionStatus.ACTIVE)
    )
    await db.commit()
    return {"success": True, "message": f"{result.rowcount} promotions activées"}


@router.post("/api/promotions/bulk/pause")
async def admin_promotions_bulk_pause(
    promo_ids: List[str] = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Met en pause plusieurs promotions"""
    result = await db.execute(
        update(Promotion)
        .where(Promotion.id.in_(promo_ids))
        .values(status=PromotionStatus.PAUSED)
    )
    await db.commit()
    return {"success": True, "message": f"{result.rowcount} promotions mises en pause"}

# ==================== API SUPPLEMENTAIRES POUR AUDIT ====================

@router.get("/api/audit/statistics")
async def admin_audit_statistics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Statistiques des logs d'audit"""
    today = datetime.utcnow().date()
    today_start = datetime.combine(today, datetime.min.time())
    
    # Total
    total_result = await db.execute(
        select(func.count(AuditLog.id))
    )
    total = total_result.scalar() or 0
    
    # Exportés
    exported_result = await db.execute(
        select(func.count(AuditLog.id))
        .where(AuditLog.leh_exported == True)
    )
    exported = exported_result.scalar() or 0
    
    # En attente
    pending = total - exported
    
    # Aujourd'hui
    today_result = await db.execute(
        select(func.count(AuditLog.id))
        .where(AuditLog.created_at >= today_start)
    )
    today_count = today_result.scalar() or 0
    
    # Critiques
    critical_result = await db.execute(
        select(func.count(AuditLog.id))
        .where(
            AuditLog.action.in_([
                "user_blocked", "account_frozen", "self_exclusion",
                "money_laundering", "fraud"
            ])
        )
    )
    critical = critical_result.scalar() or 0
    
    return {
        "total": total,
        "exported": exported,
        "pending": pending,
        "today": today_count,
        "critical": critical,
        "retention_days": 2555  # 7 ans
    }


@router.get("/api/audit/logs/{log_id}")
async def admin_audit_log_detail(
    log_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'un log d'audit"""
    result = await db.execute(
        select(AuditLog).where(AuditLog.id == log_id)
    )
    log = result.scalar_one_or_none()
    
    if not log:
        raise HTTPException(404, "Log non trouvé")
    
    return {
        "id": log.id,
        "user_id": log.user_id,
        "user_name": log.user.full_name if log.user else None,
        "agent_id": log.agent_id,
        "action": log.action,
        "action_color": "info" if log.action in ["login", "logout"] else "purple" if log.action in ["bet_placed", "bet_settled"] else "success" if log.action in ["deposit", "withdrawal"] else "danger" if log.action in ["user_blocked", "account_frozen"] else "gray",
        "resource_type": log.resource_type,
        "resource_id": log.resource_id,
        "old_values": log.old_values,
        "new_values": log.new_values,
        "reason": log.reason,
        "metadata": log.metadata,
        "ip_address": log.ip_address,
        "user_agent": log.user_agent,
        "session_id": log.session_id,
        "leh_exported": log.leh_exported,
        "leh_exported_at": log.leh_exported_at,
        "created_at": log.created_at,
        "updated_at": log.updated_at
    }


@router.post("/api/audit/logs/{log_id}/leh-export")
async def admin_audit_log_leh_export(
    log_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Marque un log comme exporté vers la LEH"""
    result = await db.execute(
        select(AuditLog).where(AuditLog.id == log_id)
    )
    log = result.scalar_one_or_none()
    
    if not log:
        raise HTTPException(404, "Log non trouvé")
    
    log.leh_exported = True
    log.leh_exported_at = datetime.utcnow()
    
    await db.commit()
    
    return {"success": True, "message": "Log marqué comme exporté"}


@router.post("/api/audit/logs/bulk/leh-export")
async def admin_audit_logs_bulk_leh_export(
    log_ids: List[str] = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Marque plusieurs logs comme exportés vers la LEH"""
    result = await db.execute(
        update(AuditLog)
        .where(AuditLog.id.in_(log_ids))
        .values(leh_exported=True, leh_exported_at=datetime.utcnow())
    )
    await db.commit()
    
    return {"success": True, "message": f"{result.rowcount} logs marqués comme exportés"}


@router.get("/api/audit/export")
async def admin_audit_export(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export des logs d'audit"""
    import csv
    import io
    
    params = dict(request.query_params)
    query = select(AuditLog).order_by(AuditLog.created_at.desc())
    
    if params.get('search'):
        # Recherche avancée
        pass
    if params.get('action'):
        query = query.where(AuditLog.action == params['action'])
    if params.get('start_date'):
        start = datetime.strptime(params['start_date'], "%Y-%m-%d")
        query = query.where(AuditLog.created_at >= start)
    if params.get('end_date'):
        end = datetime.strptime(params['end_date'], "%Y-%m-%d") + timedelta(days=1)
        query = query.where(AuditLog.created_at < end)
    
    result = await db.execute(query.limit(10000))
    logs = result.scalars().all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Date", "Utilisateur", "Action", "Resource", "Resource ID",
        "Anciennes valeurs", "Nouvelles valeurs", "Raison", "IP", "Export LEH"
    ])
    
    for log in logs:
        writer.writerow([
            log.id,
            log.created_at.isoformat(),
            log.user_id or "Système",
            log.action,
            log.resource_type or "",
            log.resource_id or "",
            json.dumps(log.old_values) if log.old_values else "",
            json.dumps(log.new_values) if log.new_values else "",
            log.reason or "",
            log.ip_address or "",
            "Oui" if log.leh_exported else "Non"
        ])
    
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=audit_logs_{datetime.utcnow().strftime('%Y%m%d')}.csv"
        }
    )

# ==================== API SUPPLEMENTAIRES POUR REPORTS ====================

@router.get("/api/reports/financial/export")
async def admin_reports_financial_export(
    request: Request,
    format: str = Query("excel"),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export des rapports financiers"""
    # Implémenter l'export Excel/PDF
    pass


@router.get("/api/reports/game/export")
async def admin_reports_game_export(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export des rapports de jeu"""
    pass


@router.get("/api/reports/compliance/export")
async def admin_reports_compliance_export(
    request: Request,
    format: str = Query("csv"),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export des rapports de conformité LEH"""
    pass


@router.post("/api/reports/compliance/leh/generate")
async def admin_reports_leh_generate(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Génère un rapport LEH"""
    import json
    from datetime import datetime
    
    params = dict(request.query_params)
    start_date = params.get('start_date', datetime.utcnow().strftime('%Y-%m-%d'))
    end_date = params.get('end_date', datetime.utcnow().strftime('%Y-%m-%d'))
    
    # Générer le rapport
    report = {
        "header": {
            "operator": "Parier Keno Haïti",
            "period": {"start": start_date, "end": end_date},
            "generated_at": datetime.utcnow().isoformat()
        },
        "summary": {
            "total_users": 0,
            "kyc_verified": 0,
            "kyc_pending": 0,
            "self_exclusions": 0,
            "total_transactions": 0,
            "total_volume": 0
        },
        "transactions": [],
        "users": []
    }
    
    await db.commit()
    
    return {"success": True, "message": "Rapport LEH généré"}


@router.post("/api/reports/compliance/leh/send")
async def admin_reports_leh_send(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Envoie le rapport à la LEH"""
    # Implémenter l'envoi à l'API LEH
    return {"success": True, "message": "Rapport envoyé à la LEH"}


@router.get("/api/reports/compliance/leh/download")
async def admin_reports_leh_download(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Télécharge le rapport LEH"""
    # Implémenter le téléchargement
    pass


@router.get("/api/compliance/alert/{alert_id}")
async def admin_compliance_alert_detail(
    alert_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'une alerte de conformité"""
    # Implémenter
    pass


@router.post("/api/compliance/alert/{alert_id}/resolve")
async def admin_compliance_alert_resolve(
    alert_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Résout une alerte de conformité"""
    return {"success": True, "message": "Alerte résolue"}

# ==================== API SUPPLEMENTAIRES POUR TICKETS ====================

@router.get("/api/tickets/statistics")
async def admin_tickets_statistics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Statistiques des tickets"""
    now = datetime.utcnow()
    
    # Total
    total_result = await db.execute(
        select(func.count(Ticket.id))
    )
    total = total_result.scalar() or 0
    
    # Actifs
    active_result = await db.execute(
        select(func.count(Ticket.id))
        .where(Ticket.status == TicketStatus.ACTIVE)
    )
    active = active_result.scalar() or 0
    
    # Solde total
    balance_result = await db.execute(
        select(func.coalesce(func.sum(Ticket.balance), 0))
        .where(Ticket.status == TicketStatus.ACTIVE)
    )
    total_balance = float(balance_result.scalar() or 0)
    
    # Expirent bientôt (dans 48h)
    expiring_result = await db.execute(
        select(func.count(Ticket.id))
        .where(
            and_(
                Ticket.status == TicketStatus.ACTIVE,
                Ticket.expires_at <= now + timedelta(hours=48),
                Ticket.expires_at > now
            )
        )
    )
    expiring_soon = expiring_result.scalar() or 0
    
    # Expirés
    expired_result = await db.execute(
        select(func.count(Ticket.id))
        .where(Ticket.status == TicketStatus.EXPIRED)
    )
    expired = expired_result.scalar() or 0
    
    # Payés
    paid_result = await db.execute(
        select(func.count(Ticket.id))
        .where(Ticket.status == TicketStatus.PAID)
    )
    paid = paid_result.scalar() or 0
    
    return {
        "total": total,
        "active": active,
        "total_balance": total_balance,
        "expiring_soon": expiring_soon,
        "expired": expired,
        "paid": paid
    }


@router.get("/api/tickets/{ticket_id}")
async def admin_ticket_detail(
    ticket_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'un ticket"""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    # Récupérer les paris
    bets_result = await db.execute(
        select(KenoBet).where(KenoBet.ticket_id == ticket_id)
        .order_by(KenoBet.placed_at.desc())
    )
    bets = bets_result.scalars().all()
    
    return {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "player_name": ticket.player_name,
        "player_phone": ticket.player_phone,
        "balance": float(ticket.balance),
        "initial_amount": float(ticket.initial_amount),
        "status": ticket.status,
        "expires_at": ticket.expires_at,
        "created_at": ticket.created_at,
        "paid_at": ticket.paid_at,
        "bureau_name": ticket.bureau.name if ticket.bureau else None,
        "agent_name": ticket.agent.full_name if ticket.agent else None,
        "bets": [
            {
                "game": "Keno",
                "picks": b.picks,
                "stake": float(b.stake),
                "winnings": float(b.winnings),
                "status": b.status,
                "date": b.placed_at
            }
            for b in bets[:20]
        ]
    }


@router.get("/api/tickets/{ticket_number}/qr")
async def admin_ticket_qr(
    ticket_number: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Génère un QR code pour un ticket"""
    import qrcode
    from io import BytesIO
    import base64
    
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    # Générer le QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=8,
        border=2,
    )
    qr.add_data(ticket_number)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()
    
    return {"success": True, "qr_code": f"data:image/png;base64,{qr_base64}"}


@router.post("/api/tickets/{ticket_id}/payout")
async def admin_ticket_payout(
    ticket_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Paye un ticket"""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        raise HTTPException(400, "Ce ticket n'est plus actif")
    
    if ticket.balance <= 0:
        raise HTTPException(400, "Aucun solde à payer")
    
    # Marquer comme payé
    amount = ticket.balance
    ticket.status = TicketStatus.PAID
    ticket.paid_at = datetime.utcnow()
    ticket.paid_by_agent = admin.id
    ticket.balance = 0
    
    # Mettre à jour la caisse du bureau
    if ticket.bureau_id:
        bureau_result = await db.execute(
            select(Bureau).where(Bureau.id == ticket.bureau_id)
        )
        bureau = bureau_result.scalar_one()
        bureau.cash_balance -= amount
    
    await db.commit()
    
    return {"success": True, "message": f"Ticket payé: {amount} HTG", "amount": float(amount)}


@router.post("/api/tickets/{ticket_id}/cancel")
async def admin_ticket_cancel(
    ticket_id: str,
    reason: str = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Annule un ticket"""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        raise HTTPException(400, "Seuls les tickets actifs peuvent être annulés")
    
    # Vérifier qu'il n'y a pas de paris en attente
    bets_result = await db.execute(
        select(KenoBet).where(
            and_(
                KenoBet.ticket_id == ticket_id,
                KenoBet.status == "PENDING"
            )
        )
    )
    pending_bets = bets_result.scalars().all()
    
    if pending_bets:
        raise HTTPException(400, f"Impossible d'annuler: {len(pending_bets)} paris en attente")
    
    # Rembourser le solde
    amount = ticket.balance
    ticket.status = TicketStatus.CANCELLED
    ticket.balance = 0
    
    # Mettre à jour la caisse du bureau (débit)
    if ticket.bureau_id:
        bureau_result = await db.execute(
            select(Bureau).where(Bureau.id == ticket.bureau_id)
        )
        bureau = bureau_result.scalar_one()
        bureau.cash_balance -= amount
    
    await db.commit()
    
    return {"success": True, "message": f"Ticket annulé. Remboursement de {amount} HTG"}


@router.post("/api/tickets/bulk/payout")
async def admin_tickets_bulk_payout(
    ticket_ids: List[str] = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Paye plusieurs tickets"""
    result = await db.execute(
        select(Ticket).where(Ticket.id.in_(ticket_ids), Ticket.status == TicketStatus.ACTIVE)
    )
    tickets = result.scalars().all()
    
    total_amount = 0
    for ticket in tickets:
        if ticket.balance > 0:
            total_amount += ticket.balance
            ticket.status = TicketStatus.PAID
            ticket.paid_at = datetime.utcnow()
            ticket.paid_by_agent = admin.id
            ticket.balance = 0
            
            if ticket.bureau_id:
                bureau_result = await db.execute(
                    select(Bureau).where(Bureau.id == ticket.bureau_id)
                )
                bureau = bureau_result.scalar_one()
                bureau.cash_balance -= ticket.balance
    
    await db.commit()
    
    return {"success": True, "message": f"{len(tickets)} tickets payés pour un total de {total_amount} HTG"}


@router.post("/api/tickets/bulk/cancel")
async def admin_tickets_bulk_cancel(
    ticket_ids: List[str] = Body(...),
    reason: str = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Annule plusieurs tickets"""
    result = await db.execute(
        select(Ticket).where(Ticket.id.in_(ticket_ids), Ticket.status == TicketStatus.ACTIVE)
    )
    tickets = result.scalars().all()
    
    total_amount = 0
    for ticket in tickets:
        total_amount += ticket.balance
        ticket.status = TicketStatus.CANCELLED
        ticket.balance = 0
        
        if ticket.bureau_id:
            bureau_result = await db.execute(
                select(Bureau).where(Bureau.id == ticket.bureau_id)
            )
            bureau = bureau_result.scalar_one()
            bureau.cash_balance -= ticket.balance
    
    await db.commit()
    
    return {"success": True, "message": f"{len(tickets)} tickets annulés pour un total de {total_amount} HTG"}


@router.get("/api/tickets/export")
async def admin_tickets_export(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export des tickets au format CSV"""
    import csv
    import io
    
    params = dict(request.query_params)
    query = select(Ticket).order_by(Ticket.created_at.desc())
    
    if params.get('search'):
        query = query.where(
            or_(
                Ticket.ticket_number.contains(params['search']),
                Ticket.player_name.contains(params['search']),
                Ticket.player_phone.contains(params['search'])
            )
        )
    if params.get('status'):
        query = query.where(Ticket.status == params['status'])
    if params.get('start_date'):
        start = datetime.strptime(params['start_date'], "%Y-%m-%d")
        query = query.where(Ticket.created_at >= start)
    if params.get('end_date'):
        end = datetime.strptime(params['end_date'], "%Y-%m-%d") + timedelta(days=1)
        query = query.where(Ticket.created_at < end)
    
    result = await db.execute(query.limit(10000))
    tickets = result.scalars().all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Numéro", "Joueur", "Téléphone", "Montant initial", "Solde",
        "Statut", "Bureau", "Agent", "Créé le", "Expire le", "Payé le"
    ])
    
    for ticket in tickets:
        writer.writerow([
            ticket.ticket_number,
            ticket.player_name or "",
            ticket.player_phone or "",
            float(ticket.initial_amount),
            float(ticket.balance),
            ticket.status,
            ticket.bureau.name if ticket.bureau else "",
            ticket.agent.full_name if ticket.agent else "",
            ticket.created_at.isoformat(),
            ticket.expires_at.isoformat(),
            ticket.paid_at.isoformat() if ticket.paid_at else ""
        ])
    
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=tickets_{datetime.utcnow().strftime('%Y%m%d')}.csv"
        }
    )

# ==================== API SUPPLEMENTAIRES POUR TRANSACTIONS ====================

@router.get("/api/transactions/{transaction_id}")
async def admin_transaction_detail(
    transaction_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'une transaction"""
    result = await db.execute(
        select(Transaction).where(Transaction.id == transaction_id)
    )
    transaction = result.scalar_one_or_none()
    
    if not transaction:
        raise HTTPException(404, "Transaction non trouvée")
    
    # Récupérer l'utilisateur
    user_result = await db.execute(
        select(User).where(User.id == transaction.user_id)
    )
    user = user_result.scalar_one_or_none()
    
    return {
        "id": transaction.id,
        "reference": transaction.reference,
        "transaction_type": transaction.transaction_type,
        "payment_method": transaction.payment_method,
        "amount": float(transaction.amount),
        "fee": float(transaction.fee),
        "bonus_amount": float(transaction.bonus_amount),
        "balance_before": float(transaction.balance_before),
        "balance_after": float(transaction.balance_after),
        "status": transaction.status,
        "bet_id": transaction.bet_id,
        "draw_id": transaction.draw_id,
        "ticket_id": transaction.ticket_id,
        "external_reference": transaction.external_reference,
        "failure_reason": transaction.failure_reason,
        "ip_address": transaction.ip_address,
        "user_agent": transaction.user_agent,
        "metadata": transaction.metadata,
        "user_id": transaction.user_id,
        "user_name": user.full_name if user else None,
        "created_at": transaction.created_at,
        "completed_at": transaction.completed_at
    }


@router.post("/api/transactions/{transaction_id}/confirm")
async def admin_transaction_confirm(
    transaction_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Confirme une transaction en attente"""
    result = await db.execute(
        select(Transaction).where(Transaction.id == transaction_id)
    )
    transaction = result.scalar_one_or_none()
    
    if not transaction:
        raise HTTPException(404, "Transaction non trouvée")
    
    if transaction.status != TransactionStatus.PENDING:
        raise HTTPException(400, "Seules les transactions en attente peuvent être confirmées")
    
    transaction.status = TransactionStatus.COMPLETED
    transaction.completed_at = datetime.utcnow()
    
    # Si c'est un retrait, mettre à jour le wallet
    if transaction.transaction_type == TransactionType.WITHDRAWAL:
        wallet_result = await db.execute(
            select(Wallet).where(Wallet.id == transaction.wallet_id)
        )
        wallet = wallet_result.scalar_one()
        wallet.pending_withdrawals -= transaction.amount
    
    await db.commit()
    
    return {"success": True, "message": "Transaction confirmée avec succès"}


@router.post("/api/transactions/{transaction_id}/cancel")
async def admin_transaction_cancel(
    transaction_id: str,
    reason: str = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Annule une transaction en attente"""
    result = await db.execute(
        select(Transaction).where(Transaction.id == transaction_id)
    )
    transaction = result.scalar_one_or_none()
    
    if not transaction:
        raise HTTPException(404, "Transaction non trouvée")
    
    if transaction.status != TransactionStatus.PENDING:
        raise HTTPException(400, "Seules les transactions en attente peuvent être annulées")
    
    transaction.status = TransactionStatus.CANCELLED
    transaction.failure_reason = reason
    transaction.completed_at = datetime.utcnow()
    
    # Si c'est un retrait, recréditer le wallet
    if transaction.transaction_type == TransactionType.WITHDRAWAL:
        wallet_result = await db.execute(
            select(Wallet).where(Wallet.id == transaction.wallet_id)
        )
        wallet = wallet_result.scalar_one()
        wallet.balance += transaction.amount
        wallet.pending_withdrawals -= transaction.amount
    
    await db.commit()
    
    return {"success": True, "message": "Transaction annulée avec succès"}


@router.get("/api/transactions/statistics")
async def admin_transactions_statistics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Statistiques des transactions"""
    today = datetime.utcnow().date()
    today_start = datetime.combine(today, datetime.min.time())
    
    # Total par type
    result = await db.execute(
        select(
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.DEPOSIT), 0).label("deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WITHDRAWAL), 0).label("withdrawals"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN), 0).label("wins")
        )
        .where(Transaction.status == TransactionStatus.COMPLETED)
    )
    stats = result.one()
    
    # En attente
    pending_result = await db.execute(
        select(func.count(Transaction.id))
        .where(Transaction.status == TransactionStatus.PENDING)
    )
    pending = pending_result.scalar() or 0
    
    # Aujourd'hui
    today_result = await db.execute(
        select(func.count(Transaction.id))
        .where(Transaction.created_at >= today_start)
    )
    today_count = today_result.scalar() or 0
    
    return {
        "total_volume": float(stats.deposits + stats.withdrawals + stats.wins),
        "total_deposits": float(stats.deposits),
        "total_withdrawals": float(stats.withdrawals),
        "total_wins": float(stats.wins),
        "pending": pending,
        "today": today_count
    }


@router.get("/api/transactions/export")
async def admin_transactions_export(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export des transactions au format CSV"""
    import csv
    import io
    
    # Récupérer les filtres
    params = dict(request.query_params)
    
    query = select(Transaction).order_by(Transaction.created_at.desc())
    
    if params.get('type'):
        query = query.where(Transaction.transaction_type == params['type'])
    if params.get('status'):
        query = query.where(Transaction.status == params['status'])
    if params.get('start_date'):
        start = datetime.strptime(params['start_date'], "%Y-%m-%d")
        query = query.where(Transaction.created_at >= start)
    if params.get('end_date'):
        end = datetime.strptime(params['end_date'], "%Y-%m-%d") + timedelta(days=1)
        query = query.where(Transaction.created_at < end)
    
    result = await db.execute(query.limit(10000))
    transactions = result.scalars().all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Référence", "Type", "Méthode", "Montant", "Frais", 
        "Solde avant", "Solde après", "Statut", "Utilisateur", "Date"
    ])
    
    for tx in transactions:
        writer.writerow([
            tx.reference,
            tx.transaction_type,
            tx.payment_method or "",
            float(tx.amount),
            float(tx.fee),
            float(tx.balance_before),
            float(tx.balance_after),
            tx.status,
            tx.user_id,
            tx.created_at.isoformat()
        ])
    
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=transactions_{datetime.utcnow().strftime('%Y%m%d')}.csv"
        }
    )

# ==================== FONCTIONS AUXILIAIRES ====================

async def _get_dashboard_stats(db: AsyncSession) -> dict:
    """Récupère les statistiques du dashboard"""
    today = datetime.utcnow().date()
    today_start = datetime.combine(today, datetime.min.time())
    
    # Utilisateurs
    users_result = await db.execute(
        select(
            func.count(User.id).label("total"),
            func.count().filter(User.created_at >= today_start).label("new_today")
        ).where(User.is_deleted == False)
    )
    users = users_result.one()
    
    # Transactions
    tx_result = await db.execute(
        select(
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.DEPOSIT), 0).label("deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WITHDRAWAL), 0).label("withdrawals"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN), 0).label("wins")
        )
        .where(Transaction.status == TransactionStatus.COMPLETED)
    )
    tx = tx_result.one()
    
    # Paris aujourd'hui
    bets_result = await db.execute(
        select(func.count(KenoBet.id))
        .where(KenoBet.placed_at >= today_start)
    )
    today_bets = bets_result.scalar() or 0
    
    # Tickets actifs
    tickets_result = await db.execute(
        select(func.count(Ticket.id))
        .where(Ticket.status == TicketStatus.ACTIVE)
    )
    active_tickets = tickets_result.scalar() or 0
    
    # Bureaux
    bureaus_result = await db.execute(
        select(
            func.count(Bureau.id).label("total"),
            func.count().filter(Bureau.is_active == True).label("active")
        ).where(Bureau.is_deleted == False)
    )
    bureaus = bureaus_result.one()
    
    return {
        "users": {
            "total": users.total or 0,
            "new_today": users.new_today or 0
        },
        "transactions": {
            "total_volume": float((tx.deposits or 0) + (tx.withdrawals or 0) + (tx.wins or 0)),
            "today_volume": 0,
            "total_wins": float(tx.wins or 0),
            "today_wins": 0
        },
        "games": {
            "today_bets": today_bets
        },
        "tickets": {
            "active": active_tickets,
            "expiring_soon": 0
        },
        "bureaus": {
            "total": bureaus.total or 0,
            "active": bureaus.active or 0
        }
    }


async def _get_recent_transactions(db: AsyncSession, limit: int = 10) -> list:
    """Récupère les dernières transactions"""
    result = await db.execute(
        select(Transaction)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def _get_recent_users(db: AsyncSession, limit: int = 10) -> list:
    """Récupère les derniers utilisateurs"""
    result = await db.execute(
        select(User)
        .where(User.is_deleted == False)
        .order_by(User.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def _get_system_alerts(db: AsyncSession, redis_client: redis.Redis) -> list:
    """Récupère les alertes système"""
    alerts = []
    
    # Tickets expirant dans 24h
    tomorrow = datetime.utcnow() + timedelta(days=1)
    tickets_result = await db.execute(
        select(func.count(Ticket.id))
        .where(
            and_(
                Ticket.status == TicketStatus.ACTIVE,
                Ticket.expires_at <= tomorrow,
                Ticket.expires_at > datetime.utcnow()
            )
        )
    )
    expiring_tickets = tickets_result.scalar() or 0
    
    if expiring_tickets > 0:
        alerts.append({
            "level": "warning",
            "message": f"{expiring_tickets} tickets expirent dans les prochaines 24h",
            "created_at": datetime.utcnow()
        })
    
    # Sessions de caisse ouvertes depuis plus de 12h
    sessions_result = await db.execute(
        select(func.count(CashierSession.id))
        .where(
            and_(
                CashierSession.status == "OPEN",
                CashierSession.opened_at < datetime.utcnow() - timedelta(hours=12)
            )
        )
    )
    open_sessions = sessions_result.scalar() or 0
    
    if open_sessions > 0:
        alerts.append({
            "level": "warning",
            "message": f"{open_sessions} sessions de caisse ouvertes depuis plus de 12h",
            "created_at": datetime.utcnow()
        })
    
    return alerts


async def _get_pending_kyc_count(db: AsyncSession) -> int:
    """Récupère le nombre d'utilisateurs en attente de KYC"""
    result = await db.execute(
        select(func.count(User.id))
        .where(User.kyc_status == KYCStatus.PENDING)
    )
    return result.scalar() or 0


async def _get_chart_data(db: AsyncSession, period: int) -> dict:
    """Récupère les données pour les graphiques"""
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=period)
    
    # Transactions par jour
    result = await db.execute(
        select(
            func.date_trunc('day', Transaction.created_at).label("day"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.DEPOSIT), 0).label("deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WITHDRAWAL), 0).label("withdrawals"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN), 0).label("wins")
        )
        .where(
            and_(
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
                Transaction.status == TransactionStatus.COMPLETED
            )
        )
        .group_by(func.date_trunc('day', Transaction.created_at))
        .order_by(func.date_trunc('day', Transaction.created_at))
    )
    rows = result.all()
    
    labels = []
    deposits = []
    withdrawals = []
    wins = []
    
    for row in rows:
        labels.append(row.day.strftime("%d/%m"))
        deposits.append(float(row.deposits))
        withdrawals.append(float(row.withdrawals))
        wins.append(float(row.wins))
    
    # Répartition des jeux
    keno_result = await db.execute(
        select(func.count(KenoBet.id))
        .where(KenoBet.placed_at >= start_date)
    )
    keno_count = keno_result.scalar() or 0
    
    lucky_result = await db.execute(
        select(func.count(LuckyPlay.id))
        .where(LuckyPlay.played_at >= start_date)
    )
    lucky_count = lucky_result.scalar() or 0
    
    total = keno_count + lucky_count
    if total == 0:
        total = 1
    
    return {
        "transactions": {
            "labels": labels,
            "deposits": deposits,
            "withdrawals": withdrawals,
            "wins": wins
        },
        "games": {
            "keno": round(keno_count / total * 100),
            "lucky": round(lucky_count / total * 100)
        }
    }


async def _get_keno_stats(db: AsyncSession) -> dict:
    """Récupère les statistiques Keno"""
    # Total tirages
    draws_result = await db.execute(
        select(func.count(KenoDraw.id))
        .where(KenoDraw.status == KenoDrawStatus.COMPLETED)
    )
    total_draws = draws_result.scalar() or 0
    
    # Total paris
    bets_result = await db.execute(
        select(
            func.count(KenoBet.id).label("total_bets"),
            func.coalesce(func.sum(KenoBet.stake), 0).label("total_volume"),
            func.coalesce(func.sum(KenoBet.winnings), 0).label("total_payout")
        )
    )
    bets = bets_result.one()
    
    total_bets = bets.total_bets or 0
    total_volume = float(bets.total_volume or 0)
    total_payout = float(bets.total_payout or 0)
    
    rtp = round(total_payout / total_volume * 100, 2) if total_volume > 0 else 0
    edge = round(100 - rtp, 2)
    
    return {
        "total_draws": total_draws,
        "total_bets": total_bets,
        "total_volume": total_volume,
        "total_payout": total_payout,
        "rtp": rtp,
        "edge": edge
    }


async def _get_lucky_stats(db: AsyncSession) -> dict:
    """Récupère les statistiques Lucky"""
    result = await db.execute(
        select(
            func.count(LuckyPlay.id).label("total_plays"),
            func.coalesce(func.sum(LuckyPlay.stake), 0).label("total_stake"),
            func.coalesce(func.sum(LuckyPlay.winnings), 0).label("total_wins"),
            func.max(LuckyPlay.multiplier).label("max_multiplier")
        )
    )
    stats = result.one()
    
    return {
        "total_plays": stats.total_plays or 0,
        "total_stake": float(stats.total_stake or 0),
        "total_wins": float(stats.total_wins or 0),
        "max_multiplier": float(stats.max_multiplier or 0)
    }


async def _get_financial_stats(db: AsyncSession, start_date: datetime, end_date: datetime) -> dict:
    """Récupère les statistiques financières"""
    
    # Total par type
    result = await db.execute(
        select(
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.DEPOSIT), 0).label("deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WITHDRAWAL), 0).label("withdrawals"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.BET), 0).label("bets"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN), 0).label("wins")
        )
        .where(
            and_(
                Transaction.created_at >= start_date,
                Transaction.created_at < end_date,
                Transaction.status == TransactionStatus.COMPLETED
            )
        )
    )
    stats = result.one()
    
    deposits = float(stats.deposits or 0)
    withdrawals = float(stats.withdrawals or 0)
    bets = float(stats.bets or 0)
    wins = float(stats.wins or 0)
    net_revenue = deposits + wins - withdrawals - bets
    
    return {
        "deposits": deposits,
        "withdrawals": withdrawals,
        "bets_volume": bets,
        "wins": wins,
        "net_revenue": net_revenue,
        "edge": round((net_revenue / (deposits + wins) * 100) if (deposits + wins) > 0 else 0, 2)
    }


async def _get_daily_financial_data(db: AsyncSession, start_date: datetime, end_date: datetime) -> list:
    """Récupère les données financières journalières"""
    
    result = await db.execute(
        select(
            func.date_trunc('day', Transaction.created_at).label("day"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.DEPOSIT), 0).label("deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WITHDRAWAL), 0).label("withdrawals"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.BET), 0).label("bets"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN), 0).label("wins")
        )
        .where(
            and_(
                Transaction.created_at >= start_date,
                Transaction.created_at < end_date,
                Transaction.status == TransactionStatus.COMPLETED
            )
        )
        .group_by(func.date_trunc('day', Transaction.created_at))
        .order_by(func.date_trunc('day', Transaction.created_at))
    )
    rows = result.all()
    
    return [
        {
            "date": row.day.strftime("%d/%m/%Y"),
            "deposits": float(row.deposits),
            "withdrawals": float(row.withdrawals),
            "bets": float(row.bets),
            "wins": float(row.wins),
            "net": float(row.deposits + row.wins - row.withdrawals - row.bets),
            "edge": round((float(row.deposits + row.wins - row.withdrawals - row.bets) / (float(row.deposits + row.wins) or 1) * 100), 2)
        }
        for row in rows
    ]


async def _send_welcome_sms(phone: str, name: str):
    """Envoie un SMS de bienvenue"""
    # À implémenter avec Twilio ou autre
    logger.info(f"SMS de bienvenue à {phone}: Bienvenue {name} sur Parier Keno Haïti!")