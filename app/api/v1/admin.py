# app/api/v1/admin.py
"""API d'administration complète"""

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, and_
from typing import List, Optional
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_admin
from app.schemas.user import UserResponse, UserCreate
from app.schemas.keno import KenoDrawResponse
from app.schemas.common import SuccessResponse, PaginatedResponse
from app.services.user_service import UserService
from app.services.keno_service import KenoService
from app.models.user import User, UserRole
from app.models.keno import KenoDraw, KenoDrawStatus
from app.models.bureau import Bureau
from app.models.audit import AuditLog
import redis.asyncio as redis

router = APIRouter(prefix="/admin", tags=["Admin"])


# ==================== TABLEAU DE BORD ====================

@router.get("/dashboard", name="api_admin_dashboard")  # nom distinct : url_for("admin_dashboard") = page HTML
async def admin_dashboard(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Tableau de bord administrateur."""
    
    now = datetime.utcnow()
    today_start = datetime.combine(now.date(), datetime.min.time())
    
    # Statistiques utilisateurs
    users_result = await db.execute(
        select(
            func.count(User.id).label("total"),
            func.count().filter(User.role == UserRole.PLAYER).label("players"),
            func.count().filter(User.role == UserRole.AGENT).label("agents"),
            func.count().filter(User.created_at >= today_start).label("new_today")
        )
    )
    users_stats = users_result.one()
    
    # Statistiques financières
    from app.models.transaction import Transaction, TransactionType
    finance_result = await db.execute(
        select(
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.DEPOSIT), 0).label("total_deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WITHDRAWAL), 0).label("total_withdrawals"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.BET), 0).label("total_bets"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN), 0).label("total_wins")
        ).where(Transaction.status == "completed")
    )
    finance_stats = finance_result.one()
    
    # Statistiques jeux
    from app.models.keno import KenoDraw
    games_result = await db.execute(
        select(
            func.count(KenoDraw.id).filter(KenoDraw.status == KenoDrawStatus.PENDING).label("pending_draws"),
            func.count(KenoDraw.id).filter(KenoDraw.status == KenoDrawStatus.COMPLETED).label("completed_draws")
        )
    )
    games_stats = games_result.one()
    
    # Tickets actifs
    from app.models.ticket import Ticket, TicketStatus
    tickets_result = await db.execute(
        select(
            func.count(Ticket.id).label("active_tickets"),
            func.coalesce(func.sum(Ticket.balance), 0).label("total_balance")
        ).where(Ticket.status == TicketStatus.ACTIVE)
    )
    tickets_stats = tickets_result.one()
    
    return {
        "users": {
            "total": users_stats.total or 0,
            "players": users_stats.players or 0,
            "agents": users_stats.agents or 0,
            "new_today": users_stats.new_today or 0
        },
        "finance": {
            "total_deposits": float(finance_stats.total_deposits),
            "total_withdrawals": float(finance_stats.total_withdrawals),
            "total_bets": float(finance_stats.total_bets),
            "total_wins": float(finance_stats.total_wins),
            "net_revenue": float(finance_stats.total_deposits - finance_stats.total_withdrawals + finance_stats.total_wins - finance_stats.total_bets)
        },
        "games": {
            "pending_draws": games_stats.pending_draws or 0,
            "completed_draws": games_stats.completed_draws or 0
        },
        "tickets": {
            "active_tickets": tickets_stats.active_tickets or 0,
            "total_balance": float(tickets_stats.total_balance)
        }
    }


# ==================== GESTION DES AGENTS ====================

@router.get("/agents", response_model=List[UserResponse])
async def list_agents(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste tous les agents."""
    
    result = await db.execute(
        select(User).where(
            User.role.in_([UserRole.AGENT, UserRole.MANAGER]),
            User.is_deleted == False
        )
    )
    agents = result.scalars().all()
    
    return agents


@router.post("/agents", response_model=UserResponse)
async def create_agent(
    agent_data: UserCreate,
    bureau_id: str = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Crée un nouvel agent."""
    
    from app.services.auth_service import AuthService
    
    auth_service = AuthService(db, redis_client)
    
    user = await auth_service.register(
        phone=agent_data.phone,
        password=agent_data.password,
        first_name=agent_data.first_name,
        last_name=agent_data.last_name,
        email=agent_data.email
    )
    
    # Mettre à jour le rôle et le bureau
    user.role = UserRole.AGENT
    if bureau_id:
        user.bureau_id = bureau_id
    
    await db.commit()
    
    return user


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Supprime (soft delete) un agent."""
    
    result = await db.execute(
        select(User).where(User.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    
    if not agent:
        raise HTTPException(status_code=404, detail="Agent non trouvé")
    
    agent.is_deleted = True
    agent.is_active = False
    
    await db.commit()
    
    return SuccessResponse(message=f"Agent {agent.phone} supprimé")


# ==================== GESTION DES BUREAUX ====================

@router.get("/bureaus")
async def list_bureaus(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste tous les bureaux."""
    
    result = await db.execute(
        select(Bureau).where(Bureau.is_deleted == False)
    )
    bureaus = result.scalars().all()
    
    return bureaus


@router.post("/bureaus")
async def create_bureau(
    name: str,
    city: str,
    address: str = None,
    phone: str = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Crée un nouveau bureau."""
    
    bureau = Bureau(
        name=name,
        code=name[:10].upper().replace(" ", ""),
        city=city,
        address=address,
        phone=phone,
        is_active=True
    )
    
    db.add(bureau)
    await db.commit()
    
    return bureau


# ==================== CONFIGURATION DES JEUX ====================

@router.get("/config/keno/draws")
async def get_keno_draws_config(
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Récupère la configuration des tirages Keno."""
    
    # Récupérer la config depuis Redis
    interval = await redis_client.get("config:keno:draw_interval")
    start_hour = await redis_client.get("config:keno:start_hour")
    end_hour = await redis_client.get("config:keno:end_hour")
    
    return {
        "draw_interval_minutes": int(interval) if interval else 5,
        "start_hour": int(start_hour) if start_hour else 8,
        "end_hour": int(end_hour) if end_hour else 23
    }


@router.put("/config/keno/draws")
async def update_keno_draws_config(
    draw_interval_minutes: int = Query(5, ge=1, le=60),
    start_hour: int = Query(8, ge=0, le=23),
    end_hour: int = Query(23, ge=0, le=23),
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis),
    background_tasks: BackgroundTasks = None
):
    """Met à jour la configuration des tirages Keno."""
    
    await redis_client.setex("config:keno:draw_interval", 86400, draw_interval_minutes)
    await redis_client.setex("config:keno:start_hour", 86400, start_hour)
    await redis_client.setex("config:keno:end_hour", 86400, end_hour)
    
    # (restart_scheduler n'existe pas : l'appel faisait échouer la requête.
    #  Le worker Celery relit ces réglages à chaque passage.)
    return SuccessResponse(message="Configuration des tirages mise à jour")


# ==================== TIROGES MANUELS ====================

@router.post("/keno/draws/trigger")
async def trigger_keno_draw(
    background_tasks: BackgroundTasks,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Déclenche un tirage Keno manuellement."""
    
    from app.services.keno_service import KenoService
    from app.api.websockets.manager import broadcast_draw_result
    
    keno_service = KenoService(db, redis_client)
    
    # Générer le tirage
    draw = await keno_service.generate_draw()
    
    # Régler les paris
    result = await keno_service.settle_bets_for_draw(draw.id)
    
    # Diffuser via WebSocket
    await broadcast_draw_result(result)
    
    return {
        "message": "Tirage déclenché avec succès",
        "draw": result
    }


@router.post("/keno/draws/{draw_id}/cancel")
async def cancel_keno_draw(
    draw_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Annule un tirage Keno EN ATTENTE et rembourse ses paris.

    Remplace l'ancien GET /keno/draws/{id}/reset, dangereux : sur un tirage
    déjà réglé il remboursait la mise de TOUS les paris, gagnants compris
    (qui gardaient leur gain), créditait les comptes sans transaction, puis
    remettait le tirage en attente ; et étant un GET accepté avec le cookie
    admin, un simple lien suffisait à le déclencher. Un tirage réglé ne peut
    plus être « réinitialisé ».
    """
    result = await KenoService(db, redis_client).cancel_draw(draw_id, by=admin.id, reason="Annulation admin (API)")
    await db.commit()
    return SuccessResponse(
        message=f"Tirage n° {result['draw_number']} annulé : {result['refunded_bets']} pari(s) remboursé(s)",
        data=result,
    )


@router.get("/audit/logs")
async def get_audit_logs(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, le=200),
    action: str = None,
    user_id: str = None,
    start_date: datetime = None,
    end_date: datetime = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Récupère les logs d'audit."""
    
    query = select(AuditLog)
    
    if action:
        query = query.where(AuditLog.action == action)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if start_date:
        query = query.where(AuditLog.created_at >= start_date)
    if end_date:
        query = query.where(AuditLog.created_at <= end_date)
    
    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()
    
    query = query.order_by(AuditLog.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    logs = result.scalars().all()
    
    return {
        "items": logs,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    }


@router.post("/audit/export")
async def export_audit_logs(
    start_date: datetime,
    end_date: datetime,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Exporte les logs d'audit pour la LEH."""
    
    query = select(AuditLog).where(
        and_(
            AuditLog.created_at >= start_date,
            AuditLog.created_at <= end_date,
            AuditLog.leh_exported == False
        )
    )
    
    result = await db.execute(query)
    logs = result.scalars().all()
    
    # Marquer comme exportés
    for log in logs:
        log.mark_exported()
    
    await db.commit()
    
    return {
        "exported_count": len(logs),
        "start_date": start_date,
        "end_date": end_date,
        "message": f"{len(logs)} logs marqués comme exportés"
    }