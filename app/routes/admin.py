# app/routes/admin.py
"""Routes d'administration complètes - King Paryaj (Keno, Lucky6, Horse Races).

(Lucky Wheel retirée : table lucky_plays conservée pour l'historique.)"""

from fastapi import APIRouter, Body, Depends, Request, Form, HTTPException, Query, BackgroundTasks, Response, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, desc, asc, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from decimal import Decimal
import json
import csv
import io
import secrets
from pathlib import Path
from urllib.parse import urlencode

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import ensure_can_manage, ensure_can_assign_role, is_super_admin
from app.core.security import get_current_admin, hash_password, verify_password
from app.core.logger import logger
from app.core.exceptions import GameException, NotFoundException, ValidationException
from app.config import settings
from app.core.csrf import register_csrf_globals  # enregistre aussi la config CsrfProtect à l'import
from app.core.timezone import (
    now_utc, now_haiti, today_haiti, today_bounds_utc, to_haiti,
    local_date_start_utc, local_date_end_utc, local_day,
)
from app.models.user import User, UserRole, KYCStatus
from app.models.wallet import Wallet
from app.models.bureau import Bureau, CashierSession
from app.models.keno import KenoDraw, KenoBet, KenoDrawStatus, KenoBetStatus
from app.models.game import GameBet
from app.models.ticket import Ticket, TicketStatus
from app.models.transaction import Transaction, TransactionType, TransactionStatus
from app.models.audit import AuditLog, AuditAction
from app.models.notification import Notification
from app.models.promotion import Promotion, PromotionStatus, UserPromotion
from app.models.enums import PaymentMethod, PromotionType
from app.models.responsible import SelfExclusion, PlayerLimit
from app.schemas.admin import (
    AdminUserCreate, AdminUserUpdate, AdminAgentCreate, AdminAgentUpdate,
    AdminBureauCreate, AdminBureauUpdate,
    AdminKenoConfig,
    AdminPromotionCreate, AdminPromotionUpdate,
    AdminSettings, AdminReportRequest,
)
from app.services.user_service import UserService
from app.services.wallet_service import WalletService
from app.services.keno_service import KenoService
from app.services.ticket_service import TicketService
from app.services.notification_service import NotificationService

import redis.asyncio as redis

# Table de paiement Keno par défaut (utilisée tant qu'aucune valeur n'a été
# poussée dans Redis via /api/keno/paytable) : {nombre de picks: {nombre de
# hits: multiplicateur}}.

# Router et templates
router = APIRouter(prefix="/admin", tags=["Admin"])
# Racine = app/templates (pas app/templates/admin) : tous les templates admin
# utilisent {% extends "admin/base.html" %} (préfixe "admin/" inclus), donc le
# loader Jinja doit chercher depuis le parent commun pour que ce chemin
# résolve. Avec l'ancienne racine "app/templates/admin", Jinja cherchait
# "app/templates/admin/admin/base.html" (inexistant) : TemplateNotFound sur
# CHAQUE page admin sauf login.html (qui n'extends rien) - jamais détecté car
# aucun test ne rendait de page HTML au-delà du login.
_raw_day_start, _raw_day_end = local_date_start_utc, local_date_end_utc


def local_date_start_utc(value):  # noqa: F811 - version tolérante pour les dates saisies dans les filtres
    try:
        return _raw_day_start(value)
    except (ValueError, TypeError):
        raise HTTPException(400, "Date invalide : utilisez le format AAAA-MM-JJ")


def local_date_end_utc(value):  # noqa: F811
    try:
        return _raw_day_end(value)
    except (ValueError, TypeError):
        raise HTTPException(400, "Date invalide : utilisez le format AAAA-MM-JJ")


templates = Jinja2Templates(directory="app/templates")
# Rend `{{ csrf_token() }}` utilisable dans les templates, déjà appelé ainsi
# partout (formulaires cachés et headers X-CSRFToken des appels fetch()).
register_csrf_globals(templates)

# ==================== FILTRES TEMPLATES ====================
def format_number(value):
    """Formate un nombre avec séparateurs de milliers"""
    if value is None:
        return "0"
    try:
        return f"{int(value):,}".replace(",", " ")
    except (ValueError, TypeError):
        return str(value)


def timeago(value):
    """Convertit une date (UTC naive en base) en format relatif, passé ou futur."""
    if not value:
        return ""
    if not isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    diff = now_utc() - value
    future = diff.total_seconds() < 0
    seconds = int(abs(diff.total_seconds()))
    days = seconds // 86400
    if days > 30:
        return to_haiti(value).strftime("%d/%m/%Y")
    if days > 0:
        amount = f"{days}j"
    elif seconds >= 3600:
        amount = f"{seconds // 3600}h"
    elif seconds >= 60:
        amount = f"{seconds // 60}min"
    else:
        return "à l'instant"
    return f"dans {amount}" if future else f"il y a {amount}"


def local_dt(value):
    """Filtre Jinja : convertit un datetime UTC stocké en heure d'Haïti."""
    return to_haiti(value)


def tojson(value, indent=None):
    """Convertit en JSON (accepte `indent`, comme le filtre `tojson`
    intégré de Jinja, utilisé par plusieurs templates pour un affichage
    formaté dans un <pre>).

    Renvoie du Markup « sûr » (comme le filtre d'origine) : sinon l'auto-échappement
    transforme les guillemets en &#34; et casse le JavaScript (`const x = {{ y|tojson }}`).
    Les caractères < > & ' sont encodés en \\uXXXX : sûr dans <script> et dans un attribut."""
    from jinja2.utils import htmlsafe_json_dumps
    return htmlsafe_json_dumps(value, dumps=lambda v, **kw: json.dumps(v, indent=indent, default=str, **kw))


def qs(request: Request, **overrides) -> str:
    """
    Construit une query string à partir des paramètres actuels de la requête,
    en écrasant les clés fournies dans `overrides` (ex: changement de page en
    conservant les filtres actifs). À utiliser dans les templates comme
    `{{ url_for('admin_x') }}{{ qs(request, page=2) }}` : `request.args`
    n'existe pas sur l'objet Request de Starlette (c'est l'API Flask), et
    passer `**request.query_params` directement à `url_for` lève une
    TypeError dès que la query string contient déjà la clé qu'on surcharge
    (ex: `page`) - d'où ce helper qui fusionne proprement les deux.
    """
    params = dict(request.query_params)
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    if not params:
        return ""
    return "?" + urlencode(params, doseq=True)


# ✅ Ajouter les filtres à l'environnement Jinja2
templates.env.filters["format_number"] = format_number
templates.env.filters["timeago"] = timeago
templates.env.filters["local"] = local_dt
templates.env.filters["tojson"] = tojson
templates.env.globals["qs"] = qs


# ==================== AUTHENTIFICATION ADMIN ====================
# Le jeton CSRF (génération pour ce GET, validation sur le POST plus bas) est
# géré pour tout /admin par AdminCsrfMiddleware (app/core/csrf.py) : les
# templates y accèdent via le global Jinja `{{ csrf_token() }}` (cf.
# register_csrf_globals ci-dessus), pas besoin de le passer explicitement.
@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(
    request: Request,
    error: Optional[str] = None,
    csrf_error: Optional[str] = None,
):
    """Page de connexion administrateur"""
    if not error and csrf_error:
        error = "Session expirée, veuillez réessayer"

    return templates.TemplateResponse(request, "admin/login.html", {
        "error": error,
        "is_authenticated": False,
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
    Traitement de la connexion administrateur.
    Le jeton CSRF a déjà été validé par AdminCsrfMiddleware avant que cette
    route ne soit atteinte (sinon la requête est redirigée en amont).
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
    user.last_login = now_utc()
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
        secure=not settings.DEBUG,
        samesite="lax",
        max_age=expire
    )
    response.set_cookie(
        key="admin_refresh",
        value=refresh_token,
        httponly=True,
        secure=not settings.DEBUG,
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
    # Récupérer le token et blacklister sous la même clé que get_current_user
    # (auparavant "admin:blacklist:*", jamais relue par la dépendance d'auth :
    # le logout ne révoquait donc jamais réellement le token).
    token = request.cookies.get("admin_token")
    if token:
        from app.core.security import decode_token

        payload = decode_token(token)
        if payload and payload.get("exp"):
            ttl = payload["exp"] - datetime.now(timezone.utc).timestamp()
            if ttl > 0:
                await redis_client.setex(f"blacklist:{token}", int(ttl), "1")

        # Supprimer le refresh token (admin_user_id n'était jamais posé en cookie,
        # donc jamais lu ici : on récupère l'id depuis le token lui-même)
        user_id = payload.get("sub") if payload else None
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
    """Tableau de bord administrateur (page HTML).

    Cette route avait été remplacée par la version JSON ci-dessous, qui
    s'affichait brute après la connexion. La version JSON reste disponible
    sur /admin/api/dashboard/summary.
    """
    stats = await _get_dashboard_stats(db, redis_client)
    recent_transactions = await _get_recent_transactions(db, limit=10)
    recent_users = await _get_recent_users(db, limit=10)
    alerts = await _get_system_alerts(db, redis_client)
    pending_kyc = await _get_pending_kyc_count(db)

    return templates.TemplateResponse(request, "admin/dashboard.html", {
        "active": "dashboard",
        "admin_name": admin.full_name or admin.email,
        "admin_role": getattr(admin.role, "value", admin.role),
        "version": "1.0.0",
        "stats": stats,
        "recent_transactions": recent_transactions,
        "recent_users": recent_users,
        "alerts": alerts,
        "pending_kyc": pending_kyc,
    })


@router.get("/api/dashboard/summary")
async def admin_dashboard_summary_api(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Résumé JSON du tableau de bord (utilisateurs, finances, jeux, tickets)."""

    # ============================================================
    # PÉRIODE DU JOUR
    # ============================================================

    today_start, tomorrow_start = today_bounds_utc()

    # ============================================================
    # UTILISATEURS
    # ============================================================

    users_result = await db.execute(
        select(
            func.count(User.id).label("total"),

            func.count().filter(
                User.role == UserRole.PLAYER
            ).label("players"),

            func.count().filter(
                User.role == UserRole.AGENT
            ).label("agents"),

            func.count().filter(
                and_(
                    User.created_at >= today_start,
                    User.created_at < tomorrow_start,
                )
            ).label("new_today"),
        ).where(
            User.is_deleted == False
        )
    )

    users_stats = users_result.one()

    # ============================================================
    # FINANCES
    # ============================================================

    from app.models.transaction import (
        Transaction,
        TransactionType,
        TransactionStatus,
    )

    finance_result = await db.execute(
        select(
            func.coalesce(
                func.sum(Transaction.amount).filter(
                    Transaction.transaction_type
                    == TransactionType.DEPOSIT
                ),
                0,
            ).label("total_deposits"),

            func.coalesce(
                func.sum(Transaction.amount).filter(
                    Transaction.transaction_type
                    == TransactionType.WITHDRAWAL
                ),
                0,
            ).label("total_withdrawals"),

            func.coalesce(
                func.sum(Transaction.amount).filter(
                    Transaction.transaction_type
                    == TransactionType.BET
                ),
                0,
            ).label("total_bets"),

            func.coalesce(
                func.sum(Transaction.amount).filter(
                    Transaction.transaction_type
                    == TransactionType.WIN
                ),
                0,
            ).label("total_wins"),
        ).where(
            Transaction.status == TransactionStatus.COMPLETED
        )
    )

    finance_stats = finance_result.one()

    total_deposits = Decimal(
        str(finance_stats.total_deposits or 0)
    )

    total_withdrawals = Decimal(
        str(finance_stats.total_withdrawals or 0)
    )

    # Mises / gains : paris réglés de tous les jeux, tickets ET comptes
    # (les transactions BET/WIN ne couvrent que les comptes joueurs)
    from app.services.finance_report_service import FinanceReportService

    (games_all,) = await FinanceReportService(db).game_totals([datetime(2000, 1, 1), now_utc() + timedelta(days=1)])
    total_bets = Decimal(str(games_all["stakes"]))
    total_wins = Decimal(str(games_all["wins"]))

    # Revenu brut des jeux
    game_revenue = total_bets - total_wins

    # Flux financier net
    net_cash_flow = total_deposits - total_withdrawals

    # ============================================================
    # JEUX KENO
    # ============================================================

    games_result = await db.execute(
        select(
            func.count(KenoDraw.id).filter(
                KenoDraw.status == KenoDrawStatus.PENDING
            ).label("pending_draws"),

            func.count(KenoDraw.id).filter(
                KenoDraw.status == KenoDrawStatus.COMPLETED
            ).label("completed_draws"),
        )
    )

    games_stats = games_result.one()

    # ============================================================
    # TICKETS
    # ============================================================

    from app.models.ticket import Ticket, TicketStatus

    tickets_result = await db.execute(
        select(
            func.count(Ticket.id).label("active_tickets"),

            func.coalesce(
                func.sum(Ticket.balance),
                0,
            ).label("total_balance"),
        ).where(
            and_(
                Ticket.status == TicketStatus.ACTIVE,
                Ticket.is_deleted == False,
            )
        )
    )

    tickets_stats = tickets_result.one()

    # ============================================================
    # RESPONSE
    # ============================================================

    return {
        "users": {
            "total": users_stats.total or 0,
            "players": users_stats.players or 0,
            "agents": users_stats.agents or 0,
            "new_today": users_stats.new_today or 0,
        },

        "finance": {
            "total_deposits": float(total_deposits),
            "total_withdrawals": float(total_withdrawals),
            "total_bets": float(total_bets),
            "total_wins": float(total_wins),

            # Revenu brut du jeu
            "net_revenue": float(game_revenue),

            # Flux financier
            "net_cash_flow": float(net_cash_flow),
        },

        "games": {
            "pending_draws": games_stats.pending_draws or 0,
            "completed_draws": games_stats.completed_draws or 0,
        },

        "tickets": {
            "active_tickets": tickets_stats.active_tickets or 0,
            "total_balance": float(
                tickets_stats.total_balance or 0
            ),
        },
    }


@router.get("/api/dashboard/stats")
async def admin_dashboard_stats_api(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """
    Statistiques pour l'auto-refresh du tableau de bord (appelée par
    dashboard.html toutes les 30s). Réponse à plat avec des clés à points
    ("users.total") : c'est le format que lit le JS (`data['users.total']`),
    pas des objets imbriqués.
    """
    stats = await _get_dashboard_stats(db, redis_client)
    return {
        "users.total": stats["users"]["total"],
        "transactions.total_volume": stats["transactions"]["total_volume"],
        "transactions.total_wins": stats["transactions"]["total_wins"],
        "games.today_bets": stats["games"]["today_bets"],
    }


@router.get("/api/dashboard/charts")
async def admin_dashboard_charts_api(
    period: int = Query(30, ge=1, le=365, description="Nombre de jours"),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Données pour les graphiques du tableau de bord : volume de transactions
    par jour sur la période choisie, et répartition Keno / Lucky6 / Horse Races
    sur la même période.
    """
    today = today_haiti()
    start_date = today - timedelta(days=period - 1)
    start_datetime = local_date_start_utc(start_date)

    tx_result = await db.execute(
        select(
            local_day(Transaction.created_at).label("day"),
            Transaction.transaction_type,
            func.coalesce(func.sum(Transaction.amount), 0).label("total")
        )
        .where(
            and_(
                Transaction.created_at >= start_datetime,
                Transaction.status == TransactionStatus.COMPLETED,
                Transaction.transaction_type.in_(
                    [TransactionType.DEPOSIT, TransactionType.WITHDRAWAL, TransactionType.WIN]
                )
            )
        )
        .group_by(local_day(Transaction.created_at), Transaction.transaction_type)
    )

    by_day = {}
    for i in range(period):
        day_key = (start_date + timedelta(days=i)).isoformat()
        by_day[day_key] = {"deposits": 0.0, "withdrawals": 0.0, "wins": 0.0}

    for row in tx_result.all():
        day_key = row.day if isinstance(row.day, str) else row.day.isoformat()
        if day_key not in by_day:
            continue
        if row.transaction_type == TransactionType.DEPOSIT:
            by_day[day_key]["deposits"] = float(row.total)
        elif row.transaction_type == TransactionType.WITHDRAWAL:
            by_day[day_key]["withdrawals"] = float(row.total)

    # Gains des joueurs par jour : paris réglés de tous les jeux, tickets ET
    # comptes (les transactions WIN ne couvrent que les comptes joueurs)
    from app.services.finance_report_service import FinanceReportService

    days = [start_date + timedelta(days=i) for i in range(period)]
    bounds = [local_date_start_utc(d) for d in days] + [local_date_end_utc(days[-1])]
    for day, games in zip(days, await FinanceReportService(db).game_totals(bounds)):
        by_day[day.isoformat()]["wins"] = games["wins_paid"]

    labels = list(by_day.keys())

    keno_result = await db.execute(
        select(func.count(KenoBet.id)).where(KenoBet.placed_at >= start_datetime)
    )
    games_result = await db.execute(
        select(GameBet.game_type, func.count(GameBet.id))
        .where(GameBet.placed_at >= start_datetime)
        .group_by(GameBet.game_type)
    )
    by_game = {game: count for game, count in games_result.all()}

    return {
        "transactions": {
            "labels": labels,
            "deposits": [by_day[d]["deposits"] for d in labels],
            "withdrawals": [by_day[d]["withdrawals"] for d in labels],
            "wins": [by_day[d]["wins"] for d in labels],
        },
        "games": {
            "keno": keno_result.scalar() or 0,
            "lucky6": by_game.get("lucky6", 0),
            "horse_races": by_game.get("horse_races", 0),
        },
    }


# ==================== UTILISATEURS ====================

def _users_query(search=None, role=None, kyc_status=None, status=None):
    """Utilisateurs non supprimés, selon les filtres de la liste (la liste et l'export les partagent).
    Une valeur de filtre inconnue (rôle, KYC) est ignorée : sous PostgreSQL, comparer une énumération à un
    texte inconnu provoquerait une erreur 500."""
    query = select(User).where(User.is_deleted == False)
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
        try:
            query = query.where(User.role == UserRole(role.lower()))
        except ValueError:
            pass
    if kyc_status:
        try:
            query = query.where(User.kyc_status == KYCStatus(kyc_status.lower()))
        except ValueError:
            pass
    if status == "active":
        query = query.where(User.is_active == True)
    elif status == "inactive":
        query = query.where(User.is_active == False)
    elif status == "locked":
        query = query.where(User.is_locked == True)
    return query


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
    query = _users_query(search, role, kyc_status, status)

    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0
    
    query = query.order_by(User.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    users = result.scalars().all()

    # Statistiques rapides (affichées dans les cartes en haut de la page,
    # indépendantes de la pagination/des filtres ci-dessus)
    stats_result = await db.execute(
        select(
            func.count(User.id).filter(User.is_active == True).label("active"),
            func.count(User.id).filter(User.role == UserRole.AGENT).label("agents"),
        ).where(User.is_deleted == False)
    )
    stats_row = stats_result.one()
    pending_kyc = await _get_pending_kyc_count(db)

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
    
    return templates.TemplateResponse(request, "admin/users/index.html", {
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
        "pending_kyc": pending_kyc,
        "stats": {
            "active": stats_row.active or 0,
            "agents": stats_row.agents or 0,
            "pending_kyc": pending_kyc,
        }
    })


@router.get("/users/create", response_class=HTMLResponse)
async def admin_user_create_page(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Page de création d'utilisateur"""
    return templates.TemplateResponse(request, "admin/users/create.html", {
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
    ensure_can_assign_role(admin, user_data.role)
    
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
        ip_address=request.client.host if request.client else "0.0.0.0",
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
    if wallet is None:
        # Les comptes admin (et anciens comptes) n'ont pas de portefeuille :
        # on affiche des montants à zéro au lieu de planter la page.
        from types import SimpleNamespace
        wallet = SimpleNamespace(
            balance=0, total_deposited=0, total_withdrawn=0, total_won=0,
            total_bonus_received=0, pending_withdrawals=0, bonus_balance=0,
            total_bet=0, is_frozen=False, currency="HTG", id=None,
        )
    
    # Statistiques des paris
    # Tous les jeux (Keno, Lucky6, Horse Races) ; paris remboursés/annulés exclus
    keno = (await db.execute(
        select(func.count(KenoBet.id), func.coalesce(func.sum(KenoBet.stake), 0),
               func.coalesce(func.sum(KenoBet.winnings).filter(KenoBet.status == KenoBetStatus.WON), 0))
        .where(KenoBet.user_id == user_id, KenoBet.status != KenoBetStatus.REFUNDED)
    )).one()
    other = (await db.execute(
        select(func.count(GameBet.id), func.coalesce(func.sum(GameBet.stake), 0),
               func.coalesce(func.sum(GameBet.winnings).filter(GameBet.status == "WON"), 0))
        .where(GameBet.user_id == user_id, GameBet.status.notin_(["REFUNDED", "VOID"]))
    )).one()
    total_bets = int(keno[0] or 0) + int(other[0] or 0)
    total_volume = float(keno[1] or 0) + float(other[1] or 0)
    total_wins = float(keno[2] or 0) + float(other[2] or 0)
    
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
    
    return templates.TemplateResponse(request, "admin/users/detail.html", {
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
    
    return templates.TemplateResponse(request, "admin/users/edit.html", {
        "active": "users",
        "user": user,
        "roles": [r.value for r in UserRole if r != UserRole.SUPER_ADMIN or is_super_admin(admin)],
        "kyc_statuses": [s.value for s in KYCStatus],
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


def _csv_cell(value) -> str:
    """Cellule de CSV sans « injection de formule » (=, +, -, @ en tête sont neutralisés dans Excel)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


@router.get("/api/users/export")
async def admin_users_export(
    search: Optional[str] = None,
    role: Optional[str] = None,
    kyc_status: Optional[str] = None,
    status: Optional[str] = None,
    ids: Optional[str] = Query(None, description="Identifiants séparés par des virgules (utilisateurs cochés)"),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Export CSV de la liste (mêmes filtres que la page) ou des utilisateurs cochés."""
    import csv
    import io

    query = _users_query(search, role, kyc_status, status)
    if ids:
        wanted = [i for i in ids.split(",") if i.strip()][:1000]
        query = query.where(User.id.in_(wanted))
    users = (await db.execute(query.order_by(User.created_at.desc()).limit(10000))).scalars().all()
    balances = {}
    if users:
        rows = await db.execute(select(Wallet.user_id, Wallet.balance).where(Wallet.user_id.in_([u.id for u in users])))
        balances = {uid: bal for uid, bal in rows.all()}

    def state(u):
        if u.is_locked:
            return "Bloqué"
        return "Actif" if u.is_active else "Inactif"

    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Téléphone", "Prénom", "Nom", "E-mail", "Rôle", "KYC", "Statut", "Solde (HTG)", "Créé le"])
    for u in users:
        writer.writerow([
            _csv_cell(u.phone), _csv_cell(u.first_name), _csv_cell(u.last_name), _csv_cell(u.email),
            _csv_cell(getattr(u.role, "value", u.role)), _csv_cell(getattr(u.kyc_status, "value", u.kyc_status)),
            state(u), f"{Decimal(str(balances.get(u.id, 0))):.2f}",
            to_haiti(u.created_at).strftime("%d/%m/%Y %H:%M") if u.created_at else "",
        ])
    filename = f"utilisateurs_{today_haiti().strftime('%Y%m%d')}.csv"
    return Response(
        content="\ufeff" + out.getvalue(),  # BOM : Excel lit correctement les accents
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/users/bulk/{action}")
async def admin_users_bulk(
    action: str,
    user_ids: List[str] = Body(..., embed=True),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Actions groupées de la liste : activer, désactiver, supprimer.
    Chaque utilisateur est contrôlé séparément ; ceux qui ne peuvent pas l'être sont ignorés avec leur motif
    (soi-même, super administrateur, compte bloqué à débloquer d'abord, introuvable)."""
    if action not in ("activate", "deactivate", "delete"):
        raise HTTPException(404, "Action inconnue")
    ids = list(dict.fromkeys(i for i in user_ids if i))[:200]
    if not ids:
        raise HTTPException(400, "Aucun utilisateur sélectionné")

    users = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_(ids), User.is_deleted == False).with_for_update()
    )).scalars().all()}
    done, skipped = 0, []
    labels = {"activate": "activé", "deactivate": "désactivé", "delete": "supprimé"}
    for uid in ids:
        user = users.get(uid)
        who = (user.full_name or user.phone) if user else uid
        if user is None:
            skipped.append({"id": uid, "reason": "introuvable"})
            continue
        if user.id == admin.id and action in ("deactivate", "delete"):
            skipped.append({"id": uid, "reason": f"{who} : vous-même"})
            continue
        try:
            ensure_can_manage(admin, user)
        except HTTPException:
            skipped.append({"id": uid, "reason": f"{who} : super administrateur"})
            continue
        if action == "activate":
            if user.is_locked:
                skipped.append({"id": uid, "reason": f"{who} : bloqué (utilisez Débloquer)"})
                continue
            user.is_active = True
        elif action == "deactivate":
            user.is_active = False
        else:
            user.soft_delete(admin.id)
            user.is_active = False
        db.add(AuditLog(
            user_id=admin.id, action=AuditAction.USER_UPDATED, resource_type="user", resource_id=user.id,
            ip_address="0.0.0.0", new_values={"bulk": action, "is_active": user.is_active},
            reason=f"Action groupée : {labels[action]}",
        ))
        done += 1

    if not done:
        raise HTTPException(400, "Aucun utilisateur " + labels[action] + " : " + "; ".join(s["reason"] for s in skipped[:5]))
    await db.commit()
    plural = "s" if done > 1 else ""
    message = f"{done} utilisateur{plural} {labels[action]}{plural}"
    if skipped:
        message += f" ; {len(skipped)} ignoré(s) ({'; '.join(s['reason'] for s in skipped[:3])})"
    return {"success": True, "message": message, "done": done, "skipped": skipped}


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
    ensure_can_manage(admin, user)
    
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
        ensure_can_assign_role(admin, user_data.role)
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
        ip_address="0.0.0.0",
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
    ensure_can_manage(admin, user)
    if user.id == admin.id:
        raise HTTPException(400, "Vous ne pouvez pas supprimer votre propre compte")
    
    user.soft_delete(admin.id)
    user.is_active = False
    
    await db.commit()
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_BLOCKED,
        resource_type="user",
        resource_id=user_id,
        ip_address="0.0.0.0",
        reason="Suppression par admin"
    )
    db.add(audit)
    await db.commit()
    
    return {"success": True, "message": "Utilisateur supprimé avec succès"}


@router.post("/api/users/{user_id}/block")
async def admin_user_block(
    user_id: str,
    reason: str = Body(..., embed=True),
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
    ensure_can_manage(admin, user)
    
    user.is_locked = True
    user.lock_reason = reason
    user.is_active = False
    user.locked_at = now_utc()
    
    await db.commit()
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.USER_BLOCKED,
        resource_type="user",
        resource_id=user_id,
        ip_address="0.0.0.0",
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
    ensure_can_manage(admin, user)
    
    user.is_locked = False
    user.lock_reason = None
    user.is_active = True
    user.locked_at = None
    
    await db.commit()
    
    return {"success": True, "message": "Utilisateur débloqué avec succès"}


# ==================== AGENTS ====================

AGENT_ROLES = (UserRole.AGENT, UserRole.MANAGER)


def _agents_query(search=None, bureau_id=None, status=None):
    """Agents (et responsables) non supprimés, selon les filtres de la liste (partagés avec l'export)."""
    query = select(User).options(selectinload(User.bureau)).where(User.role.in_(AGENT_ROLES), User.is_deleted == False)
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
    if status == "active":
        query = query.where(User.is_active == True)
    elif status == "inactive":
        query = query.where(User.is_active == False)
    return query


async def _get_agent(db: AsyncSession, agent_id: str) -> User:
    """Un agent (ou responsable) existant : une route « agent » ne doit jamais agir sur un joueur ou un admin."""
    user = (await db.execute(
        select(User).where(User.id == agent_id, User.is_deleted == False, User.role.in_(AGENT_ROLES))
    )).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "Agent non trouvé")
    return user


async def _open_cash_session(db: AsyncSession, agent_id: str):
    return (await db.execute(
        select(CashierSession).where(CashierSession.agent_id == agent_id, CashierSession.status == "OPEN")
    )).scalars().first()


@router.get("/agents", response_class=HTMLResponse)
async def admin_agents(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    bureau_id: Optional[str] = None,
    status: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Liste des agents"""

    query = _agents_query(search, bureau_id, status)

    # ============================================================
    # Pagination
    # ============================================================

    total_result = await db.execute(
        select(func.count()).select_from(query.subquery())
    )
    total = total_result.scalar() or 0

    query = query.order_by(User.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    agents = list(result.scalars().all())

    # ============================================================
    # Total encaissé aujourd'hui par agent
    # ============================================================

    today = today_haiti()
    today_start = local_date_start_utc(today)
    today_end = local_date_end_utc(today)

    agent_ids = [agent.id for agent in agents]

    cash_in_by_agent = {}

    if agent_ids:
        cash_result = await db.execute(
            select(
                CashierSession.agent_id,
                func.coalesce(
                    func.sum(CashierSession.cash_in_amount),
                    0
                ).label("total_cash_in_today")
            )
            .where(
                CashierSession.agent_id.in_(agent_ids),
                CashierSession.opened_at >= today_start,
                CashierSession.opened_at < today_end
            )
            .group_by(CashierSession.agent_id)
        )

        cash_in_by_agent = {
            row.agent_id: row.total_cash_in_today or Decimal("0")
            for row in cash_result.all()
        }

    # Ajouter l'attribut attendu par le template
    for agent in agents:
        agent.total_cash_in_today = cash_in_by_agent.get(
            agent.id,
            Decimal("0")
        )

    # Commission du mois (taux × ventes) par agent
    from app.services.commission_service import CommissionService, effective_rate, get_default_rate

    default_commission = await get_default_rate(db, redis_client)
    commissions = CommissionService(db, redis_client)
    month_sales = await commissions.sales(
        [local_date_start_utc(today.replace(day=1)), today_end], agent_ids
    ) if agent_ids else {}
    empty = {"sales": Decimal("0"), "frozen": Decimal("0"), "legacy": Decimal("0")}
    for agent in agents:
        rate = effective_rate(agent, default_commission)
        agent.commission_effective = float(rate)
        agent.commission_month = float(commissions._commission(month_sales.get((0, agent.id), empty), rate))

    # ============================================================
    # Bureaux pour le filtre
    # ============================================================

    bureaus_result = await db.execute(
        select(Bureau).where(
            Bureau.is_deleted == False
        )
    )
    bureaus = bureaus_result.scalars().all()

    # ============================================================
    # Statistiques rapides
    # ============================================================

    stats_result = await db.execute(
        select(
            func.count(User.id).label("total"),
            func.count(User.id).filter(
                User.is_active == True
            ).label("active"),
            func.count(User.id).filter(
                User.is_active == False
            ).label("pending"),
        ).where(
            User.role.in_([
                UserRole.AGENT,
                UserRole.MANAGER
            ]),
            User.is_deleted == False
        )
    )

    stats_row = stats_result.one()

    # ============================================================
    # Template
    # ============================================================

    return templates.TemplateResponse(
        request,
        "admin/agents/index.html",
        {
            "active": "agents",

            "stats": {
                "total": stats_row.total or 0,
                "active": stats_row.active or 0,
                "pending": stats_row.pending or 0,
                "bureaus": len(bureaus),
            },
            "default_commission": float(default_commission),

            "filters": {
                "search": search,
                "bureau_id": bureau_id,
                "status": status,
            },

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
        }
    )


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
    
    if agent_data.email:
        if (await db.execute(select(User.id).where(User.email == agent_data.email))).first():
            raise HTTPException(400, "Cet email est déjà utilisé")

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
        resource_id=user.id,
        ip_address="0.0.0.0"
    )
    db.add(audit)
    
    await db.commit()
    
    return {"success": True, "agent_id": user.id, "message": "Agent créé avec succès"}


@router.get("/api/agents/export")
async def admin_agents_export(
    search: Optional[str] = None,
    bureau_id: Optional[str] = None,
    status: Optional[str] = None,
    ids: Optional[str] = Query(None, description="Identifiants séparés par des virgules (agents cochés)"),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Export CSV de la liste des agents (mêmes filtres que la page) ou des agents cochés."""
    import csv
    import io

    query = _agents_query(search, bureau_id, status)
    if ids:
        query = query.where(User.id.in_([i for i in ids.split(",") if i.strip()][:1000]))
    agents = (await db.execute(query.order_by(User.created_at.desc()).limit(10000))).scalars().all()

    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Téléphone", "Prénom", "Nom", "E-mail", "Rôle", "Bureau", "Statut", "Commission (%)", "Créé le"])
    for a in agents:
        writer.writerow([
            _csv_cell(a.phone), _csv_cell(a.first_name), _csv_cell(a.last_name), _csv_cell(a.email),
            _csv_cell(getattr(a.role, "value", a.role)), _csv_cell(a.bureau.name if a.bureau else ""),
            "Actif" if a.is_active else "Inactif",
            "" if a.commission_rate is None else f"{Decimal(str(a.commission_rate)):.2f}",
            to_haiti(a.created_at).strftime("%d/%m/%Y %H:%M") if a.created_at else "",
        ])
    filename = f"agents_{today_haiti().strftime('%Y%m%d')}.csv"
    return Response(
        content="\ufeff" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/agents/bulk/{action}")
async def admin_agents_bulk(
    action: str,
    agent_ids: List[str] = Body(..., embed=True),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Actions groupées de la liste des agents : activer, désactiver, supprimer. Chaque agent est contrôlé
    séparément ; les autres sont ignorés avec leur motif (introuvable, caisse ouverte pour une suppression)."""
    if action not in ("activate", "deactivate", "delete"):
        raise HTTPException(404, "Action inconnue")
    ids = list(dict.fromkeys(i for i in agent_ids if i))[:200]
    if not ids:
        raise HTTPException(400, "Aucun agent sélectionné")

    agents = {u.id: u for u in (await db.execute(
        select(User).where(User.id.in_(ids), User.is_deleted == False, User.role.in_(AGENT_ROLES)).with_for_update()
    )).scalars().all()}
    labels = {"activate": "activé", "deactivate": "désactivé", "delete": "supprimé"}
    done, skipped = 0, []
    for aid in ids:
        agent = agents.get(aid)
        if agent is None:
            skipped.append({"id": aid, "reason": "agent introuvable"})
            continue
        who = agent.full_name or agent.phone
        try:
            ensure_can_manage(admin, agent)
        except HTTPException:
            skipped.append({"id": aid, "reason": f"{who} : super administrateur"})
            continue
        if action == "delete" and await _open_cash_session(db, agent.id):
            skipped.append({"id": aid, "reason": f"{who} : session de caisse ouverte"})
            continue
        if action == "activate":
            agent.is_active = True
        elif action == "deactivate":
            agent.is_active = False
        else:
            agent.soft_delete(admin.id)
            agent.is_active = False
        db.add(AuditLog(
            user_id=admin.id, action=AuditAction.USER_UPDATED, resource_type="agent", resource_id=agent.id,
            ip_address="0.0.0.0", new_values={"bulk": action, "is_active": agent.is_active},
            reason=f"Action groupée : {labels[action]}",
        ))
        done += 1

    if not done:
        raise HTTPException(400, f"Aucun agent {labels[action]} : " + "; ".join(x["reason"] for x in skipped[:5]))
    await db.commit()
    plural = "s" if done > 1 else ""
    message = f"{done} agent{plural} {labels[action]}{plural}"
    if skipped:
        message += f" ; {len(skipped)} ignoré(s) ({'; '.join(x['reason'] for x in skipped[:3])})"
    return {"success": True, "message": message, "done": done, "skipped": skipped}


@router.put("/api/agents/{agent_id}")
async def admin_agent_update(
    agent_id: str,
    agent_data: AdminAgentUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Mise à jour d'un agent"""

    user = await _get_agent(db, agent_id)
    ensure_can_manage(admin, user)

    if agent_data.password:
        user.password_hash = hash_password(agent_data.password)
    if agent_data.first_name:
        user.first_name = agent_data.first_name
    if agent_data.last_name:
        user.last_name = agent_data.last_name
    if agent_data.email and agent_data.email != user.email:
        if (await db.execute(select(User.id).where(User.email == agent_data.email, User.id != user.id))).first():
            raise HTTPException(400, "Cet email est déjà utilisé")
        user.email = agent_data.email
    if agent_data.national_id:
        user.national_id = agent_data.national_id
    if agent_data.bureau_id:
        if (await db.get(Bureau, agent_data.bureau_id)) is None:
            raise HTTPException(404, "Bureau non trouvé")
        user.bureau_id = agent_data.bureau_id
    if agent_data.is_active is not None:
        user.is_active = agent_data.is_active
    if "commission_rate" in agent_data.model_fields_set:
        from app.services.commission_service import parse_rate

        raw = (agent_data.commission_rate or "").strip()
        user.commission_rate = parse_rate(raw) if raw else None  # vide = taux par défaut

    await db.commit()

    return {"success": True, "message": "Agent mis à jour avec succès"}


@router.post("/api/agents/commission-default")
async def admin_agents_commission_default(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Taux de commission par défaut des agents (% des ventes), stocké en base.
    S'applique aux ventes À VENIR : les commissions déjà figées ne changent pas."""
    from app.services.commission_service import set_default_rate

    try:
        payload = await request.json()
    except Exception:
        raise ValidationException("Requête invalide")
    rate = await set_default_rate(db, (payload or {}).get("rate"), by=admin.id)
    await db.commit()
    logger.info(f"Commission agents par défaut : {rate} % (admin {admin.id})")
    return {"success": True, "message": f"Commission par défaut : {rate} % des ventes", "rate": float(rate)}


@router.delete("/api/agents/{agent_id}")
async def admin_agent_delete(
    agent_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Suppression d'un agent"""
    
    user = await _get_agent(db, agent_id)
    ensure_can_manage(admin, user)
    if await _open_cash_session(db, user.id):
        raise HTTPException(400, "Cet agent a une session de caisse ouverte : fermez-la avant de le supprimer")
    
    user.soft_delete(admin.id)
    user.is_active = False

    await db.commit()

    return {"success": True, "message": "Agent supprimé avec succès"}


@router.post("/api/agents/{agent_id}/toggle-status")
async def admin_agent_toggle_status(
    agent_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Active/désactive un agent"""
    user = await _get_agent(db, agent_id)
    ensure_can_manage(admin, user)

    user.is_active = not user.is_active
    await db.commit()

    status = "activé" if user.is_active else "désactivé"
    return {"success": True, "message": f"Agent {status} avec succès"}


@router.post("/api/agents/{agent_id}/reset-password")
async def admin_agent_reset_password(
    agent_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Réinitialise le mot de passe d'un agent et lui envoie par SMS"""
    user = await _get_agent(db, agent_id)
    ensure_can_manage(admin, user)

    new_password = secrets.token_urlsafe(9)
    user.password_hash = hash_password(new_password)

    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.PASSWORD_CHANGE,
        resource_type="agent",
        resource_id=agent_id,
        ip_address="0.0.0.0",
        reason="Réinitialisation par un administrateur"
    )
    db.add(audit)
    await db.commit()

    # Pas de SMS branché : le mot de passe temporaire est affiché UNE fois à l'administrateur, qui le
    # communique à l'agent. Il n'est ni conservé ni écrit dans les journaux.
    return {"success": True, "message": "Mot de passe réinitialisé : communiquez le mot de passe temporaire à l'agent",
            "temporary_password": new_password, "phone": user.phone}


@router.get("/agents/create", response_class=HTMLResponse)
async def admin_agent_create_page(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Page de création d'un agent"""
    bureaus_result = await db.execute(
        select(Bureau).where(Bureau.is_deleted == False).order_by(Bureau.name)
    )
    bureaus = bureaus_result.scalars().all()

    return templates.TemplateResponse(request, "admin/agents/create.html", {
        "active": "agents",
        "bureaus": bureaus,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


async def _get_agent_today_stats(db: AsyncSession, agent_id: str) -> dict:
    """Statistiques du jour pour un agent (paris facilités, tickets créés,
    encaissements/paiements via ses sessions de caisse ouvertes aujourd'hui)."""
    today_start = today_bounds_utc()[0]

    bets_result = await db.execute(
        select(func.count(KenoBet.id)).where(
            KenoBet.agent_id == agent_id, KenoBet.placed_at >= today_start
        )
    )
    tickets_result = await db.execute(
        select(func.count(Ticket.id)).where(
            Ticket.agent_id == agent_id, Ticket.created_at >= today_start
        )
    )
    sessions_result = await db.execute(
        select(
            func.coalesce(func.sum(CashierSession.cash_in_amount), 0),
            func.coalesce(func.sum(CashierSession.cash_out_amount), 0),
        ).where(
            CashierSession.agent_id == agent_id, CashierSession.opened_at >= today_start
        )
    )
    cash_in, cash_out = sessions_result.one()

    return {
        "today_bets": bets_result.scalar() or 0,
        "today_tickets": tickets_result.scalar() or 0,
        "today_cash_in": float(cash_in or 0),
        "today_cash_out": float(cash_out or 0),
    }


_AUDIT_ACTIVITY_ICONS = {
    AuditAction.LOGIN: ("sign-in-alt", "3b82f6"),
    AuditAction.LOGOUT: ("sign-out-alt", "94a3b8"),
    AuditAction.USER_CREATED: ("user-plus", "22c55e"),
    AuditAction.USER_UPDATED: ("user-edit", "3b82f6"),
    AuditAction.USER_BLOCKED: ("user-lock", "ef4444"),
    AuditAction.DEPOSIT: ("arrow-down", "22c55e"),
    AuditAction.WITHDRAWAL: ("arrow-up", "ef4444"),
    AuditAction.BET_PLACED: ("dice", "8b5cf6"),
    AuditAction.PASSWORD_CHANGE: ("key", "f59e0b"),
}


async def _get_recent_activities(db: AsyncSession, user_id: str, limit: int = 10) -> list:
    """Dernière activité d'audit pour un utilisateur (agent/bureau), adaptée
    au format attendu par les templates (icon/color/description/type)."""
    result = await db.execute(
        select(AuditLog)
        .where(or_(AuditLog.user_id == user_id, AuditLog.resource_id == user_id))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()

    activities = []
    for log in logs:
        icon, color = _AUDIT_ACTIVITY_ICONS.get(log.action, ("circle", "94a3b8"))
        activities.append({
            "icon": icon,
            "color": color,
            "description": (log.action.value if hasattr(log.action, "value") else log.action).replace("_", " ").capitalize(),
            "created_at": log.created_at,
            "type": log.resource_type or "système",
        })
    return activities


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
async def admin_agent_detail(
    request: Request,
    agent_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'un agent"""
    result = await db.execute(
        select(User)
        .options(selectinload(User.bureau))  # le template lit agent.bureau (pas de lazy-load en async)
        .where(User.id == agent_id, User.is_deleted == False)
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(404, "Agent non trouvé")

    stats = await _get_agent_today_stats(db, agent_id)

    sessions_result = await db.execute(
        select(CashierSession)
        .where(CashierSession.agent_id == agent_id)
        .order_by(CashierSession.opened_at.desc())
        .limit(10)
    )
    sessions = sessions_result.scalars().all()

    activities = await _get_recent_activities(db, agent_id)

    return templates.TemplateResponse(request, "admin/agents/detail.html", {
        "active": "agents",
        "agent": agent,
        "stats": stats,
        "sessions": sessions,
        "activities": activities,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/agents/{agent_id}/edit", response_class=HTMLResponse)
async def admin_agent_edit_page(
    request: Request,
    agent_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Page d'édition d'un agent"""
    result = await db.execute(
        select(User).where(User.id == agent_id, User.is_deleted == False)
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(404, "Agent non trouvé")

    bureaus_result = await db.execute(
        select(Bureau).where(Bureau.is_deleted == False).order_by(Bureau.name)
    )
    bureaus = bureaus_result.scalars().all()

    agent_stats = await _get_agent_today_stats(db, agent_id)

    from app.services.commission_service import get_default_rate

    return templates.TemplateResponse(request, "admin/agents/edit.html", {
        "active": "agents",
        "agent": agent,
        "default_commission": float(await get_default_rate(db, redis_client)),
        "bureaus": bureaus,
        "agent_stats": agent_stats,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/agents/{agent_id}/sessions", response_class=HTMLResponse)
async def admin_agent_sessions(
    request: Request,
    agent_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Historique des sessions de caisse d'un agent"""
    result = await db.execute(
        select(User).where(User.id == agent_id, User.is_deleted == False)
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(404, "Agent non trouvé")

    sessions_result = await db.execute(
        select(CashierSession)
        .where(CashierSession.agent_id == agent_id)
        .order_by(CashierSession.opened_at.desc())
        .limit(100)
    )
    sessions = sessions_result.scalars().all()

    return templates.TemplateResponse(request, "admin/agents/sessions.html", {
        "active": "agents",
        "agent": agent,
        "sessions": sessions,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


# ==================== BUREAUX ====================

@router.get("/bureaus", response_class=HTMLResponse)
async def admin_bureaus(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste des bureaux"""

    query = select(Bureau).where(Bureau.is_deleted == False)

    if search:
        query = query.where(
            or_(
                Bureau.name.contains(search),
                Bureau.code.contains(search),
                Bureau.city.contains(search)
            )
        )
    if city:
        query = query.where(Bureau.city == city)
    if status == "active":
        query = query.where(Bureau.is_active == True)
    elif status == "inactive":
        query = query.where(Bureau.is_active == False)

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0

    query = query.order_by(Bureau.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    bureaus = result.scalars().all()

    # Nombre d'agents par bureau affiché (attribut ad-hoc, pas une colonne)
    for bureau in bureaus:
        agents_count_result = await db.execute(
            select(func.count(User.id)).where(
                User.bureau_id == bureau.id,
                User.role == UserRole.AGENT,
                User.is_deleted == False
            )
        )
        bureau.agents_count = agents_count_result.scalar() or 0

    # Villes disponibles pour le filtre
    cities_result = await db.execute(
        select(Bureau.city).where(Bureau.is_deleted == False, Bureau.city.isnot(None)).distinct()
    )
    cities = sorted({c for c in cities_result.scalars().all() if c})

    # Statistiques rapides (cartes en haut de la page)
    stats_result = await db.execute(
        select(
            func.count(Bureau.id).label("total"),
            func.count(Bureau.id).filter(Bureau.is_active == True).label("active"),
            func.coalesce(func.sum(Bureau.cash_balance), 0).label("total_cash"),
        ).where(Bureau.is_deleted == False)
    )
    stats_row = stats_result.one()
    total_agents_result = await db.execute(
        select(func.count(User.id)).where(
            User.role == UserRole.AGENT,
            User.is_deleted == False,
            User.bureau_id.isnot(None)
        )
    )

    return templates.TemplateResponse(request, "admin/bureaus/index.html", {
        "active": "bureaus",
        "bureaus": bureaus,
        "cities": cities,
        "filters": {
            "search": search,
            "city": city,
            "status": status,
        },
        "stats": {
            "total": stats_row.total or 0,
            "active": stats_row.active or 0,
            "total_agents": total_agents_result.scalar() or 0,
            "total_cash": float(stats_row.total_cash or 0),
        },
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


async def _bureau_delete_blocker(db: AsyncSession, bureau: Bureau):
    """Motif empêchant la suppression d'un bureau (None si la suppression est possible)."""
    agents = (await db.execute(select(func.count(User.id)).where(
        User.bureau_id == bureau.id, User.role.in_(AGENT_ROLES), User.is_deleted == False))).scalar() or 0
    if agents:
        return f"{bureau.name} : {agents} agent(s) rattaché(s)"
    open_sessions = (await db.execute(select(func.count(CashierSession.id)).where(
        CashierSession.bureau_id == bureau.id, CashierSession.status == "OPEN"))).scalar() or 0
    if open_sessions:
        return f"{bureau.name} : session de caisse ouverte"
    active_tickets = (await db.execute(select(func.count(Ticket.id)).where(
        Ticket.bureau_id == bureau.id, Ticket.status == TicketStatus.ACTIVE))).scalar() or 0
    if active_tickets:
        return f"{bureau.name} : {active_tickets} ticket(s) actif(s)"
    if (bureau.cash_balance or 0) != 0:
        return f"{bureau.name} : la caisse n'est pas vide ({bureau.cash_balance} HTG)"
    return None


@router.delete("/api/bureaus/{bureau_id}")
async def admin_bureau_delete(
    bureau_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Suppression d'un bureau (refusée s'il a encore des agents, une caisse ouverte, des tickets actifs ou de l'argent)."""
    bureau = (await db.execute(
        select(Bureau).where(Bureau.id == bureau_id, Bureau.is_deleted == False).with_for_update()
    )).scalar_one_or_none()
    if not bureau:
        raise HTTPException(404, "Bureau non trouvé")
    blocker = await _bureau_delete_blocker(db, bureau)
    if blocker:
        raise HTTPException(400, f"Suppression impossible — {blocker}")

    bureau.soft_delete(admin.id)
    bureau.is_active = False
    db.add(AuditLog(user_id=admin.id, action=AuditAction.USER_UPDATED, resource_type="bureau", resource_id=bureau.id,
                    ip_address="0.0.0.0", new_values={"deleted": True}, reason="Suppression du bureau"))
    await db.commit()
    return {"success": True, "message": "Bureau supprimé avec succès"}


@router.get("/bureaus/create", response_class=HTMLResponse)
async def admin_bureau_create_page(
    request: Request,
    admin: User = Depends(get_current_admin)
):
    """Page de création d'un bureau"""
    return templates.TemplateResponse(request, "admin/bureaus/create.html", {
        "active": "bureaus",
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


async def _get_bureau_stats(db: AsyncSession, bureau_id: str) -> dict:
    """Statistiques d'un bureau : agents rattachés, tickets actifs, paris du jour."""
    today_start = today_bounds_utc()[0]

    agents_result = await db.execute(
        select(func.count(User.id)).where(
            User.bureau_id == bureau_id, User.role == UserRole.AGENT, User.is_deleted == False
        )
    )
    active_tickets_result = await db.execute(
        select(func.count(Ticket.id)).where(
            Ticket.bureau_id == bureau_id, Ticket.status == TicketStatus.ACTIVE
        )
    )
    today_bets_result = await db.execute(
        select(func.count(KenoBet.id))
        .join(Ticket, KenoBet.ticket_id == Ticket.id)
        .where(Ticket.bureau_id == bureau_id, KenoBet.placed_at >= today_start)
    )

    return {
        "agents": agents_result.scalar() or 0,
        "active_tickets": active_tickets_result.scalar() or 0,
        "today_bets": today_bets_result.scalar() or 0,
    }


@router.get("/bureaus/{bureau_id}", response_class=HTMLResponse)
async def admin_bureau_detail(
    request: Request,
    bureau_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'un bureau"""
    result = await db.execute(
        select(Bureau).where(Bureau.id == bureau_id, Bureau.is_deleted == False)
    )
    bureau = result.scalar_one_or_none()

    if not bureau:
        raise HTTPException(404, "Bureau non trouvé")

    stats = await _get_bureau_stats(db, bureau_id)

    agents_result = await db.execute(
        select(User)
        .where(User.bureau_id == bureau_id, User.role == UserRole.AGENT, User.is_deleted == False)
        .order_by(User.created_at.desc())
        .limit(10)
    )
    agents = agents_result.scalars().all()

    activities = await _get_recent_activities(db, bureau_id)

    return templates.TemplateResponse(request, "admin/bureaus/detail.html", {
        "active": "bureaus",
        "bureau": bureau,
        "stats": stats,
        "opening_hours": bureau.opening_hours or {},
        "agents": agents,
        "activities": activities,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/bureaus/{bureau_id}/edit", response_class=HTMLResponse)
async def admin_bureau_edit_page(
    request: Request,
    bureau_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Page d'édition d'un bureau"""
    result = await db.execute(
        select(Bureau).where(Bureau.id == bureau_id, Bureau.is_deleted == False)
    )
    bureau = result.scalar_one_or_none()

    if not bureau:
        raise HTTPException(404, "Bureau non trouvé")

    managers_result = await db.execute(
        select(User).where(
            User.role.in_([UserRole.MANAGER, UserRole.ADMIN]), User.is_deleted == False
        ).order_by(User.first_name)
    )
    managers = managers_result.scalars().all()

    bureau_stats = await _get_bureau_stats(db, bureau_id)

    return templates.TemplateResponse(request, "admin/bureaus/edit.html", {
        "active": "bureaus",
        "bureau": bureau,
        "managers": managers,
        "bureau_stats": bureau_stats,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


# ==================== CONFIGURATION KENO ====================

@router.get("/games/keno/config", response_class=HTMLResponse)
async def admin_keno_config(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Configuration du Keno (modifiable à tout moment, appliquée au ticket suivant)."""
    from app.services import keno_engine

    config = await KenoService(db, redis_client).get_config()
    return templates.TemplateResponse(request, "admin/games/keno/config.html", {
        "active": "keno",
        "config": config,
        "public": keno_engine.public_config(config),
        "default_paytable": keno_engine.DEFAULT_CONFIG["paytable"],
        "stats": await _get_keno_stats(db),
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/api/keno/config")
async def admin_keno_config_get(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    from app.services import keno_engine

    config = await KenoService(db, redis_client).get_config()
    return {"config": config, "public": keno_engine.public_config(config)}


@router.put("/api/keno/config")
async def admin_keno_config_save(
    payload: Dict = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Enregistre la configuration (validée par le serveur : limites, mises,
    table de paiement, taux de redistribution maximum). S'applique au ticket suivant."""
    from app.services import keno_engine

    config = await KenoService(db, redis_client).save_config(payload, admin_id=admin.id)
    await db.commit()
    return {"success": True, "message": "Configuration Keno enregistrée", "config": config,
            "public": keno_engine.public_config(config)}


@router.post("/api/keno/paytable/preview")
async def admin_keno_paytable_preview(
    payload: Dict = Body(...),
    admin: User = Depends(get_current_admin),
):
    """Calcule le taux de redistribution d'une table (sans l'enregistrer) ou
    l'ajuste à un taux cible (`target_rtp`, en %)."""
    from app.services import keno_engine

    try:
        table = keno_engine.parse_paytable(payload.get("paytable"))
        if payload.get("target_rtp") not in (None, ""):
            table = keno_engine.scale_paytable(table, float(payload["target_rtp"]))
    except (keno_engine.KenoConfigError, ValueError, TypeError) as e:
        raise ValidationException(str(e) or "Table de paiement invalide")
    return {
        "paytable": {str(sp): {str(h): str(m) for h, m in sorted(row.items())} for sp, row in sorted(table.items())},
        "rtp": {str(k): v for k, v in keno_engine.rtp_table(table).items()},
    }


@router.get("/games/keno/draws", response_class=HTMLResponse)
async def admin_keno_draws_page(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    period: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Liste des tirages Keno"""
    query = select(KenoDraw)

    if status:
        query = query.where(KenoDraw.status == status)
    if period:
        now = now_utc()
        if period == "today":
            start = today_bounds_utc()[0]
        elif period == "week":
            start = now - timedelta(days=7)
        elif period == "month":
            start = now - timedelta(days=30)
        else:
            start = None
        if start:
            query = query.where(KenoDraw.draw_time >= start)

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0

    query = query.order_by(KenoDraw.draw_time.desc()).offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    draws = result.scalars().all()

    # Tirages en attente : les totaux ne sont écrits qu'au règlement -> valeurs en direct
    pending_ids = [d.id for d in draws if d.status == KenoDrawStatus.PENDING]
    if pending_ids:
        live = await KenoService(db, redis_client).live_totals(pending_ids)
        for d in draws:
            if d.id in live:
                set_committed_value(d, "total_bets", live[d.id]["total_bets"])
                set_committed_value(d, "total_amount", live[d.id]["total_amount"])

    today_start = today_bounds_utc()[0]
    stats_result = await db.execute(
        select(
            func.count(KenoDraw.id).filter(KenoDraw.status == KenoDrawStatus.PENDING).label("pending"),
            func.count(KenoDraw.id).filter(KenoDraw.status == KenoDrawStatus.COMPLETED).label("completed"),
            func.count(KenoDraw.id).filter(KenoDraw.draw_time >= today_start).label("today"),
        )
    )
    stats_row = stats_result.one()

    next_draw_result = await db.execute(
        select(KenoDraw.draw_time)
        .where(KenoDraw.status == KenoDrawStatus.PENDING, KenoDraw.mode == "scheduled", KenoDraw.draw_time >= now_utc())
        .order_by(KenoDraw.draw_time.asc())
        .limit(1)
    )
    next_draw_time = next_draw_result.scalar_one_or_none()
    waiting_for_agent = False
    if next_draw_time is None:  # tirage ouvert sans heure : il attend qu'un agent le lance
        waiting_for_agent = bool((await db.execute(
            select(func.count(KenoDraw.id)).where(KenoDraw.status == KenoDrawStatus.PENDING, KenoDraw.mode == "scheduled")
        )).scalar())

    return templates.TemplateResponse(request, "admin/games/keno/draws.html", {
        "active": "keno",
        "draws": draws,
        "filters": {"status": status, "period": period},
        "stats": {
            "pending": stats_row.pending or 0,
            "completed": stats_row.completed or 0,
            "today": stats_row.today or 0,
            "next_draw": next_draw_time.strftime("%d/%m %H:%M") if next_draw_time else ("Lancé par un agent" if waiting_for_agent else None),
        },
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


@router.get("/games/keno/statistics", response_class=HTMLResponse)
async def admin_keno_statistics_page(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Statistiques détaillées du jeu Keno"""
    if not end_date:
        end_date = today_haiti().isoformat()
    if not start_date:
        start_date = (today_haiti() - timedelta(days=30)).isoformat()

    start = local_date_start_utc(start_date)
    end = local_date_end_utc(end_date)

    stats_result = await db.execute(
        select(
            func.count(func.distinct(KenoDraw.id)).label("total_draws"),
        )
        .select_from(KenoDraw)
        .where(KenoDraw.draw_time >= start, KenoDraw.draw_time < end, KenoDraw.status == KenoDrawStatus.COMPLETED)
    )
    total_draws = stats_result.scalar() or 0

    bets_result = await db.execute(
        select(
            func.count(KenoBet.id).label("total_bets"),
            func.coalesce(func.sum(KenoBet.stake), 0).label("total_volume"),
            func.coalesce(func.sum(KenoBet.winnings), 0).label("total_payout"),
        ).where(KenoBet.placed_at >= start, KenoBet.placed_at < end,
                KenoBet.status.in_([KenoBetStatus.WON, KenoBetStatus.LOST]))  # paris réglés seulement
    )
    bets_row = bets_result.one()
    total_volume = float(bets_row.total_volume or 0)
    total_payout = float(bets_row.total_payout or 0)
    rtp = round(total_payout / total_volume * 100, 2) if total_volume > 0 else 0

    stats = {
        "total_draws": total_draws,
        "total_bets": bets_row.total_bets or 0,
        "total_volume": total_volume,
        "total_payout": total_payout,
        "rtp": rtp,
        "edge": round(100 - rtp, 2),
    }

    # Fréquence des numéros tirés (calculée en Python : ARRAY Postgres non
    # agrégeable simplement en SQL portable)
    numbers_result = await db.execute(
        select(KenoDraw.numbers).where(
            KenoDraw.draw_time >= start, KenoDraw.draw_time < end,
            KenoDraw.status == KenoDrawStatus.COMPLETED, KenoDraw.numbers.isnot(None)
        )
    )
    from collections import Counter
    counter = Counter()
    for (numbers,) in numbers_result.all():
        counter.update(numbers or [])
    most_common = counter.most_common(10)
    least_common = sorted(counter.items(), key=lambda kv: kv[1])[:10]
    popular_numbers = [{"number": n, "count": c} for n, c in most_common]
    least_popular_numbers = [{"number": n, "count": c} for n, c in least_common]

    # Évolution quotidienne
    daily_result = await db.execute(
        select(
            local_day(KenoBet.placed_at).label("day"),
            func.count(func.distinct(KenoBet.draw_id)).label("draws"),
            func.count(KenoBet.id).label("bets"),
            func.coalesce(func.sum(KenoBet.stake), 0).label("volume"),
            func.coalesce(func.sum(KenoBet.winnings), 0).label("payout"),
        )
        .where(KenoBet.placed_at >= start, KenoBet.placed_at < end,
               KenoBet.status.in_([KenoBetStatus.WON, KenoBetStatus.LOST]))
        .group_by(local_day(KenoBet.placed_at))
        .order_by(local_day(KenoBet.placed_at))
    )
    daily_rows = daily_result.all()
    daily_data = []
    for row in daily_rows:
        volume = float(row.volume)
        payout = float(row.payout)
        day_rtp = round(payout / volume * 100, 2) if volume > 0 else 0
        daily_data.append({
            "date": row.day.strftime("%d/%m/%Y"),
            "draws": row.draws,
            "bets": row.bets,
            "volume": volume,
            "payout": payout,
            "rtp": day_rtp,
            "edge": round(100 - day_rtp, 2),
        })

    chart_data = {
        "labels": [d["date"] for d in daily_data],
        "bets": [d["bets"] for d in daily_data],
        "volume": [d["volume"] for d in daily_data],
        "payout": [d["payout"] for d in daily_data],
    }

    return templates.TemplateResponse(request, "admin/games/keno/statistics.html", {
        "active": "keno",
        "start_date": start_date,
        "end_date": end_date,
        "stats": stats,
        "popular_numbers": popular_numbers,
        "least_popular_numbers": least_popular_numbers,
        "daily_data": daily_data,
        "chart_data": chart_data,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


# ==================== TRANSACTIONS ====================

@router.get("/transactions", response_class=HTMLResponse)
async def admin_transactions(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    transaction_type: Optional[str] = None,
    status: Optional[str] = None,
    method: Optional[str] = None,
    user_id: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste des transactions"""

    query = select(Transaction)

    if search:
        query = query.where(Transaction.reference.contains(search))
    if transaction_type and _parse_enum(TransactionType, transaction_type):
        query = query.where(Transaction.transaction_type == _parse_enum(TransactionType, transaction_type))
    if status and _parse_enum(TransactionStatus, status):
        query = query.where(Transaction.status == _parse_enum(TransactionStatus, status))
    if method and _parse_enum(PaymentMethod, method):
        query = query.where(Transaction.payment_method == _parse_enum(PaymentMethod, method))
    if user_id:
        query = query.where(Transaction.user_id == user_id)
    if min_amount is not None:
        query = query.where(Transaction.amount >= min_amount)
    if max_amount is not None:
        query = query.where(Transaction.amount <= max_amount)
    if start_date:
        start = local_date_start_utc(start_date)
        query = query.where(Transaction.created_at >= start)
    if end_date:
        end = local_date_end_utc(end_date)
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

    # Statistiques rapides
    today_start = today_bounds_utc()[0]
    stats_result = await db.execute(
        select(
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.status == TransactionStatus.COMPLETED), 0).label("total_volume"),
            func.coalesce(func.sum(Transaction.amount).filter(
                Transaction.transaction_type == TransactionType.DEPOSIT, Transaction.status == TransactionStatus.COMPLETED
            ), 0).label("total_deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(
                Transaction.transaction_type == TransactionType.WITHDRAWAL, Transaction.status == TransactionStatus.COMPLETED
            ), 0).label("total_withdrawals"),
            func.coalesce(func.sum(Transaction.amount).filter(
                Transaction.transaction_type == TransactionType.WIN, Transaction.status == TransactionStatus.COMPLETED
            ), 0).label("total_wins"),
            func.count(Transaction.id).filter(Transaction.status == TransactionStatus.PENDING).label("pending"),
            func.count(Transaction.id).filter(Transaction.created_at >= today_start).label("today"),
        )
    )
    stats_row = stats_result.one()

    return templates.TemplateResponse(request, "admin/transactions/index.html", {
        "active": "transactions",
        "transactions": transactions,
        "stats": {
            "total_volume": float(stats_row.total_volume or 0),
            "total_deposits": float(stats_row.total_deposits or 0),
            "total_withdrawals": float(stats_row.total_withdrawals or 0),
            "total_wins": float(stats_row.total_wins or 0),
            "pending": stats_row.pending or 0,
            "today": stats_row.today or 0,
        },
        "filters": {
            "search": search,
            "type": transaction_type,
            "status": status,
            "method": method,
            "user_id": user_id,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "start_date": start_date,
            "end_date": end_date,
        },
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


@router.get("/transactions/{transaction_id}", response_class=HTMLResponse)
async def admin_transaction_detail_page(
    request: Request,
    transaction_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'une transaction"""
    result = await db.execute(
        select(Transaction, User)
        .join(User, Transaction.user_id == User.id, isouter=True)
        .where(Transaction.id == transaction_id)
    )
    row = result.first()

    if not row:
        raise HTTPException(404, "Transaction non trouvée")

    transaction, user = row
    transaction.user = user

    return templates.TemplateResponse(request, "admin/transactions/detail.html", {
        "active": "transactions",
        "transaction": transaction,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


# ==================== TICKETS ====================

@router.get("/tickets", response_class=HTMLResponse)
async def admin_tickets(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    bureau_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    search: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Gestion des tickets"""

    query = select(Ticket).options(selectinload(Ticket.bureau), selectinload(Ticket.agent))

    if status and status.lower() in {x.value for x in TicketStatus}:
        query = query.where(Ticket.status == TicketStatus(status.lower()))
    if bureau_id:
        query = query.where(Ticket.bureau_id == bureau_id)
    if agent_id:
        query = query.where(Ticket.agent_id == agent_id)
    if search:
        query = query.where(
            or_(
                Ticket.ticket_number.contains(search),
                Ticket.player_name.contains(search),
                Ticket.player_phone.contains(search)
            )
        )
    if min_amount is not None:
        query = query.where(Ticket.initial_amount >= min_amount)
    if max_amount is not None:
        query = query.where(Ticket.initial_amount <= max_amount)
    if start_date:
        start = local_date_start_utc(start_date)
        query = query.where(Ticket.created_at >= start)
    if end_date:
        end = local_date_end_utc(end_date)
        query = query.where(Ticket.created_at < end)

    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0

    query = query.order_by(Ticket.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    tickets = result.scalars().all()

    # Bureaux et agents pour les filtres
    bureaus_result = await db.execute(select(Bureau).where(Bureau.is_deleted == False))
    bureaus = bureaus_result.scalars().all()
    agents_result = await db.execute(
        select(User).where(User.role == UserRole.AGENT, User.is_deleted == False)
    )
    agents = agents_result.scalars().all()

    # Statistiques rapides
    soon = now_utc() + timedelta(days=2)
    stats_result = await db.execute(
        select(
            func.count(Ticket.id).label("total"),
            func.count(Ticket.id).filter(Ticket.status == TicketStatus.ACTIVE).label("active"),
            func.count(Ticket.id).filter(Ticket.status == TicketStatus.EXPIRED).label("expired"),
            func.count(Ticket.id).filter(Ticket.status == TicketStatus.PAID).label("paid"),
            func.coalesce(func.sum(Ticket.balance).filter(Ticket.status == TicketStatus.ACTIVE), 0).label("total_balance"),
            func.count(Ticket.id).filter(
                Ticket.status == TicketStatus.ACTIVE, Ticket.expires_at <= soon
            ).label("expiring_soon"),
        )
    )
    stats_row = stats_result.one()

    return templates.TemplateResponse(request, "admin/tickets/index.html", {
        "active": "tickets",
        "tickets": tickets,
        "bureaus": bureaus,
        "agents": agents,
        "stats": {
            "total": stats_row.total or 0,
            "active": stats_row.active or 0,
            "expired": stats_row.expired or 0,
            "paid": stats_row.paid or 0,
            "total_balance": float(stats_row.total_balance or 0),
            "expiring_soon": stats_row.expiring_soon or 0,
        },
        "filters": {
            "search": search,
            "status": status,
            "bureau_id": bureau_id,
            "agent_id": agent_id,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "start_date": start_date,
            "end_date": end_date,
        },
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
        "now": now_utc(),
    })


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def admin_ticket_detail_page(
    request: Request,
    ticket_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'un ticket"""
    result = await db.execute(
        select(Ticket).options(selectinload(Ticket.bureau), selectinload(Ticket.agent)).where(Ticket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")

    tx_result = await db.execute(
        select(Transaction)
        .where(Transaction.ticket_id == ticket_id)
        .order_by(Transaction.created_at.desc())
    )
    transactions = tx_result.scalars().all()
    for tx in transactions:
        tx.type = tx.transaction_type.value if hasattr(tx.transaction_type, "value") else tx.transaction_type
        tx.balance = tx.balance_after
        tx.description = tx.failure_reason or tx.external_reference

    bets_result = await db.execute(
        select(KenoBet)
        .where(KenoBet.ticket_id == ticket_id)
        .order_by(KenoBet.placed_at.desc())
    )
    bets = bets_result.scalars().all()
    for bet in bets:
        bet.game = "Keno"

    return templates.TemplateResponse(request, "admin/tickets/detail.html", {
        "active": "tickets",
        "ticket": ticket,
        "transactions": transactions,
        "bets": bets,
        "now": now_utc(),
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
    year: Optional[int] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Finances : tout l'argent qui entre et qui sort (guichets + comptes
    joueurs) et le revenu du système, par période et par mois."""
    data = await _financial_report(db, start_date, end_date, year, redis_client)
    return templates.TemplateResponse(request, "admin/reports/financial.html", {
        "active": "reports",
        **data,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


async def _financial_report(db: AsyncSession, start_date: Optional[str], end_date: Optional[str], year: Optional[int],
                            redis_client=None) -> dict:
    from app.services.commission_service import CommissionService
    from app.services.finance_report_service import FinanceReportService

    today = today_haiti()
    # Période par défaut : le mois en cours
    try:
        start_day = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else today.replace(day=1)
        end_day = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else today
    except ValueError:
        raise ValidationException("Date invalide (format AAAA-MM-JJ)")
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    year = year if year and 2000 <= year <= today.year else today.year

    service = FinanceReportService(db, redis_client)
    start, end = local_date_start_utc(start_day), local_date_end_utc(end_day)
    period = await service.period(start, end)
    months = await service.monthly(year)

    # Évolution jour par jour (jusqu'à 3 mois affichés)
    daily = []
    days = (end_day - start_day).days + 1
    if days <= 93:
        day_list = [start_day + timedelta(days=i) for i in range(days)]
        bounds = [local_date_start_utc(d) for d in day_list] + [end]
        for d, row in zip(day_list, await service._compute(bounds)):
            daily.append({"date": d.strftime("%d/%m"), "in": row["in"]["total"], "out": row["out"]["total"],
                          "revenue": row["revenue"]})

    first_year = (await db.execute(select(func.min(Ticket.created_at)))).scalar()
    return {
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "year": year,
        "years": list(range(today.year, min(first_year.year if first_year else today.year, today.year) - 1, -1)),
        "period": period,
        "months": months,
        "year_totals": service.totals(months),
        "bureaus": await service.by_bureau(start, end),
        "agent_commissions": await CommissionService(db, redis_client).by_agent(start, end),
        "snapshot": await service.snapshot(),
        "daily": daily,
        "presets": {
            "month": (today.replace(day=1).isoformat(), today.isoformat()),
            "last_month": ((today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat(),
                           (today.replace(day=1) - timedelta(days=1)).isoformat()),
            "days30": ((today - timedelta(days=29)).isoformat(), today.isoformat()),
            "year": (today.replace(month=1, day=1).isoformat(), today.isoformat()),
        },
    }


# ==================== RECHERCHE GLOBALE ====================

@router.get("/search", response_class=HTMLResponse)
async def admin_global_search(
    request: Request,
    q: str = Query("", max_length=100),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Recherche transversale : utilisateurs/agents, tickets, bureaux et transactions."""
    term = q.strip()
    users, tickets, bureaus, transactions = [], [], [], []
    if len(term) >= 2:
        users = (await db.execute(
            select(User).where(User.is_deleted == False, or_(
                User.phone.contains(term), User.email.contains(term),
                User.first_name.contains(term), User.last_name.contains(term)))
            .order_by(User.created_at.desc()).limit(10))).scalars().all()
        tickets = (await db.execute(
            select(Ticket).where(or_(Ticket.ticket_number.contains(term), Ticket.player_name.contains(term),
                                     Ticket.player_phone.contains(term)))
            .order_by(Ticket.created_at.desc()).limit(10))).scalars().all()
        bureaus = (await db.execute(
            select(Bureau).where(Bureau.is_deleted == False,
                                 or_(Bureau.name.contains(term), Bureau.code.contains(term), Bureau.city.contains(term)))
            .order_by(Bureau.name).limit(10))).scalars().all()
        transactions = (await db.execute(
            select(Transaction).where(Transaction.reference.contains(term))
            .order_by(Transaction.created_at.desc()).limit(10))).scalars().all()

    return templates.TemplateResponse(request, "admin/search.html", {
        "active": "",
        "q": term,
        "too_short": 0 < len(term) < 2,
        "users": users, "tickets": tickets, "bureaus": bureaus, "transactions": transactions,
        "total": len(users) + len(tickets) + len(bureaus) + len(transactions),
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
    search: Optional[str] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    user_id: Optional[str] = None,
    ip_address: Optional[str] = None,
    leh_exported: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Logs d'audit pour la conformité LEH"""

    query = select(AuditLog).options(selectinload(AuditLog.user)).order_by(AuditLog.created_at.desc())

    if search:
        query = query.where(
            or_(
                AuditLog.resource_id.contains(search),
                AuditLog.reason.contains(search)
            )
        )
    if action and action in {a.value for a in AuditAction}:
        query = query.where(AuditLog.action == AuditAction(action))
    if resource_type:
        query = query.where(AuditLog.resource_type == resource_type)
    if user_id:
        query = query.where(AuditLog.user_id == user_id)
    if ip_address:
        query = query.where(AuditLog.ip_address == ip_address)
    if leh_exported in ("true", "false"):
        query = query.where(AuditLog.leh_exported == (leh_exported == "true"))
    if start_date:
        start = local_date_start_utc(start_date)
        query = query.where(AuditLog.created_at >= start)
    if end_date:
        end = local_date_end_utc(end_date)
        query = query.where(AuditLog.created_at < end)

    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0

    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    logs = result.scalars().all()

    # Statistiques rapides
    today_start = today_bounds_utc()[0]
    stats_result = await db.execute(
        select(
            func.count(AuditLog.id).label("total"),
            func.count(AuditLog.id).filter(AuditLog.leh_exported == True).label("exported"),
            func.count(AuditLog.id).filter(AuditLog.leh_exported == False).label("pending"),
            func.count(AuditLog.id).filter(AuditLog.created_at >= today_start).label("today"),
            func.count(AuditLog.id).filter(
                AuditLog.action.in_([AuditAction.LOGIN_FAILED, AuditAction.USER_BLOCKED])
            ).label("critical"),
        )
    )
    stats_row = stats_result.one()

    return templates.TemplateResponse(request, "admin/audit/logs.html", {
        "active": "audit",
        "logs": logs,
        "stats": {
            "total": stats_row.total or 0,
            "exported": stats_row.exported or 0,
            "pending": stats_row.pending or 0,
            "today": stats_row.today or 0,
            "critical": stats_row.critical or 0,
            # Pas encore configurable : durée de conservation réglementaire LEH par défaut.
            "retention_days": 365,
        },
        "filters": {
            "search": search,
            "action": action,
            "resource_type": resource_type,
            "user_id": user_id,
            "ip_address": ip_address,
            "leh_exported": leh_exported,
            "start_date": start_date,
            "end_date": end_date,
        },
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
    
    start = local_date_start_utc(start_date)
    end = local_date_end_utc(end_date)
    
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
        log.leh_exported_at = now_utc()
    
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
            to_haiti(log.created_at).strftime("%d/%m/%Y %H:%M:%S"),
            log.user_id,
            log.action.value,
            log.resource_type,
            _csv_cell(json.dumps(log.old_values, ensure_ascii=False)) if log.old_values else "",
            _csv_cell(json.dumps(log.new_values, ensure_ascii=False)) if log.new_values else "",
            log.ip_address
        ])
    
    return Response(
        content="﻿" + output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=audit_export_{start_date}_{end_date}.csv"
        }
    )


# ==================== PROMOTIONS ====================

@router.get("/promotions", response_class=HTMLResponse)
async def admin_promotions(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Liste des promotions"""

    query = select(Promotion)

    if search:
        query = query.where(
            or_(
                Promotion.name.contains(search),
                Promotion.code.contains(search)
            )
        )
    if status and _parse_enum(PromotionStatus, status):
        query = query.where(Promotion.status == _parse_enum(PromotionStatus, status))
    if type and _parse_enum(PromotionType, type):
        query = query.where(Promotion.type == _parse_enum(PromotionType, type))
    if start_date:
        query = query.where(Promotion.start_date >= _parse_day(start_date))
    if end_date:
        query = query.where(Promotion.end_date < _parse_day(end_date) + timedelta(days=1))

    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar() or 0

    query = query.order_by(Promotion.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    promotions = result.scalars().all()

    stats_result = await db.execute(
        select(
            func.count(Promotion.id).label("total"),
            func.count(Promotion.id).filter(Promotion.status == PromotionStatus.ACTIVE).label("active"),
            func.count(Promotion.id).filter(Promotion.status == PromotionStatus.DRAFT).label("pending"),
            func.count(Promotion.id).filter(Promotion.status == PromotionStatus.EXPIRED).label("expired"),
            func.coalesce(func.sum(Promotion.used_budget), 0).label("used_budget"),
            func.coalesce(func.sum(Promotion.total_claims), 0).label("total_claims"),
        )
    )
    stats_row = stats_result.one()

    return templates.TemplateResponse(request, "admin/promotions/index.html", {
        "active": "promotions",
        "promotions": promotions,
        "stats": {
            "total": stats_row.total or 0,
            "active": stats_row.active or 0,
            "pending": stats_row.pending or 0,
            "expired": stats_row.expired or 0,
            "used_budget": float(stats_row.used_budget or 0),
            "total_claims": stats_row.total_claims or 0,
        },
        "filters": {
            "search": search,
            "status": status,
            "type": type,
            "start_date": start_date,
            "end_date": end_date,
        },
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


async def _promotion_delete_blocker(db: AsyncSession, promotion: Promotion):
    claims = (await db.execute(select(func.count(UserPromotion.id)).where(
        UserPromotion.promotion_id == promotion.id))).scalar() or 0
    if claims or (promotion.total_claims or 0):
        return f"{promotion.name} : déjà utilisée par des joueurs ({max(claims, promotion.total_claims or 0)})"
    return None


@router.delete("/api/promotions/{promotion_id}")
async def admin_promotion_delete(
    promotion_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Suppression d'une promotion jamais utilisée (sinon : la mettre en pause, l'historique des bonus est conservé)."""
    promotion = (await db.execute(select(Promotion).where(Promotion.id == promotion_id))).scalar_one_or_none()
    if not promotion:
        raise HTTPException(404, "Promotion non trouvée")
    blocker = await _promotion_delete_blocker(db, promotion)
    if blocker:
        raise HTTPException(400, f"Suppression impossible — {blocker}. Mettez-la en pause à la place.")
    db.add(AuditLog(user_id=admin.id, action=AuditAction.USER_UPDATED, resource_type="promotion", resource_id=promotion.id,
                    ip_address="0.0.0.0", new_values={"deleted": True, "name": promotion.name}, reason="Suppression de la promotion"))
    await db.delete(promotion)
    await db.commit()
    return {"success": True, "message": "Promotion supprimée avec succès"}


@router.get("/promotions/create", response_class=HTMLResponse)
async def admin_promotion_create_page(
    request: Request,
    admin: User = Depends(get_current_admin)
):
    """Page de création d'une promotion"""
    return templates.TemplateResponse(request, "admin/promotions/create.html", {
        "active": "promotions",
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/promotions/{promotion_id}", response_class=HTMLResponse)
async def admin_promotion_detail(
    request: Request,
    promotion_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Détails d'une promotion"""
    result = await db.execute(
        select(Promotion).where(Promotion.id == promotion_id)
    )
    promotion = result.scalar_one_or_none()

    if not promotion:
        raise HTTPException(404, "Promotion non trouvée")

    claims_result = await db.execute(
        select(UserPromotion, User)
        .join(User, UserPromotion.user_id == User.id, isouter=True)
        .where(UserPromotion.promotion_id == promotion_id)
        .order_by(UserPromotion.claimed_at.desc())
        .limit(20)
    )
    recent_claims = [
        {
            "user_name": (user.full_name or user.phone) if user else "Utilisateur supprimé",
            "bonus_amount": float(claim.bonus_amount),
            "created_at": claim.claimed_at,
        }
        for claim, user in claims_result.all()
    ]

    return templates.TemplateResponse(request, "admin/promotions/detail.html", {
        "active": "promotions",
        "promotion": promotion,
        "recent_claims": recent_claims,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/promotions/{promotion_id}/edit", response_class=HTMLResponse)
async def admin_promotion_edit_page(
    request: Request,
    promotion_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Page d'édition d'une promotion"""
    result = await db.execute(
        select(Promotion).where(Promotion.id == promotion_id)
    )
    promotion = result.scalar_one_or_none()

    if not promotion:
        raise HTTPException(404, "Promotion non trouvée")

    return templates.TemplateResponse(request, "admin/promotions/edit.html", {
        "active": "promotions",
        "promotion": promotion,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


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
    ensure_can_manage(admin, user)
    
    user.kyc_status = KYCStatus.VERIFIED
    user.kyc_verified_at = now_utc()
    user.kyc_verified_by = admin.id

    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.KYC_VERIFIED,
        resource_type="user",
        resource_id=user_id,
        ip_address="0.0.0.0"
    )
    db.add(audit)
    await db.commit()

    return {"success": True, "message": "KYC validé avec succès"}


@router.post("/api/users/{user_id}/kyc/reject")
async def admin_user_kyc_reject(
    user_id: str,
    reason: str = Body(..., embed=True),
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
    ensure_can_manage(admin, user)
    
    user.kyc_status = KYCStatus.REJECTED
    
    # Audit log
    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.KYC_SUBMITTED,
        resource_type="user",
        resource_id=user_id,
        ip_address="0.0.0.0",
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
    ensure_can_manage(admin, user)
    
    user.kyc_status = KYCStatus.PENDING
    user.kyc_verified_at = None
    user.kyc_verified_by = None

    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.KYC_SUBMITTED,
        resource_type="user",
        resource_id=user_id,
        ip_address="0.0.0.0",
        reason="Réinitialisation par un administrateur"
    )
    db.add(audit)
    await db.commit()

    return {"success": True, "message": "KYC réinitialisé"}


def _parse_day(raw: str) -> datetime:
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise HTTPException(400, "Date invalide : utilisez le format AAAA-MM-JJ")


def _parse_enum(enum_cls, raw):
    """Valeur de filtre -> membre de l'enum, ou None si inconnue (un filtre invalide est ignoré, jamais une erreur)."""
    if raw is None or isinstance(raw, enum_cls):
        return raw
    text = str(raw).strip()
    for candidate in (text, text.lower()):
        try:
            return enum_cls(candidate)
        except ValueError:
            pass
    return enum_cls.__members__.get(text.upper())


KYC_MAX_BYTES = 5 * 1024 * 1024
KYC_MAX_DOCS = 10
KYC_DOC_TYPES = {"id", "passport", "proof", "selfie", "other"}
_KYC_SIGNATURES = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg", "file-image"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png", "file-image"),
    (b"%PDF", "pdf", "application/pdf", "file-pdf"),
)


def _kyc_dir(user_id: str):
    import os
    from pathlib import Path
    return Path(os.getenv("KYC_UPLOAD_DIR", "uploads/kyc")) / user_id


def _kyc_docs(user: User) -> list:
    try:
        docs = json.loads(user.kyc_documents) if user.kyc_documents else []
    except (TypeError, ValueError):
        return []
    return docs if isinstance(docs, list) else []


def _kyc_documents_for_page(user: User) -> list:
    pages = []
    for d in _kyc_docs(user):
        try:
            uploaded = datetime.fromisoformat(d.get("uploaded_at"))
        except (TypeError, ValueError):
            uploaded = now_utc()
        pages.append({
            "id": d.get("id"), "name": d.get("name") or "document", "size": d.get("size") or 0,
            "type": d.get("icon") or "file", "uploaded_at": uploaded,
            "url": f"/admin/users/{user.id}/kyc/document/{d.get('id')}",
        })
    return pages


async def _kyc_target(db: AsyncSession, admin: User, user_id: str) -> User:
    user = (await db.execute(select(User).where(User.id == user_id, User.is_deleted == False))).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")
    ensure_can_manage(admin, user)
    return user


@router.post("/api/users/{user_id}/kyc/upload")
async def admin_user_kyc_upload(
    user_id: str,
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    comment: Optional[str] = Form(None),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Ajoute un document KYC (JPG, PNG ou PDF, 5 Mo maximum) au dossier d'un utilisateur."""
    import uuid
    user = await _kyc_target(db, admin, user_id)
    if doc_type not in KYC_DOC_TYPES:
        raise HTTPException(400, "Type de document invalide")
    docs = _kyc_docs(user)
    if len(docs) >= KYC_MAX_DOCS:
        raise HTTPException(400, f"Maximum {KYC_MAX_DOCS} documents par utilisateur")
    content = await file.read(KYC_MAX_BYTES + 1)
    if not content:
        raise HTTPException(400, "Fichier vide")
    if len(content) > KYC_MAX_BYTES:
        raise HTTPException(400, "Fichier trop volumineux (5 Mo maximum)")
    kind = next((sig for sig in _KYC_SIGNATURES if content.startswith(sig[0])), None)
    if kind is None:
        raise HTTPException(400, "Format non accepté : utilisez JPG, PNG ou PDF")
    _, ext, mime, icon = kind

    doc_id = uuid.uuid4().hex
    folder = _kyc_dir(user.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{doc_id}.{ext}").write_bytes(content)
    docs.append({
        "id": doc_id, "doc_type": doc_type, "name": (file.filename or f"document.{ext}")[:120],
        "ext": ext, "mime": mime, "icon": icon, "size": len(content),
        "comment": (comment or "")[:200], "uploaded_at": now_utc().isoformat(), "uploaded_by": admin.id,
    })
    user.kyc_documents = json.dumps(docs)
    db.add(AuditLog(user_id=admin.id, action=AuditAction.KYC_SUBMITTED, resource_type="user", resource_id=user.id,
                    ip_address="0.0.0.0", reason=f"Document ajouté ({doc_type})"))
    await db.commit()
    return {"success": True, "message": "Document ajouté avec succès"}


@router.get("/users/{user_id}/kyc/document/{doc_id}")
async def admin_user_kyc_document(
    user_id: str,
    doc_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Affiche un document KYC (réservé aux administrateurs)."""
    user = await _kyc_target(db, admin, user_id)
    doc = next((d for d in _kyc_docs(user) if d.get("id") == doc_id), None)
    if not doc:
        raise HTTPException(404, "Document non trouvé")
    path = _kyc_dir(user.id) / f"{doc['id']}.{doc['ext']}"
    if not path.is_file():
        raise HTTPException(404, "Fichier introuvable sur le serveur")
    return FileResponse(path, media_type=doc.get("mime") or "application/octet-stream",
                        headers={"Content-Disposition": "inline", "X-Content-Type-Options": "nosniff"})


@router.delete("/api/users/{user_id}/kyc/document/{doc_id}")
async def admin_user_kyc_document_delete(
    user_id: str,
    doc_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Supprime un document KYC."""
    user = await _kyc_target(db, admin, user_id)
    docs = _kyc_docs(user)
    doc = next((d for d in docs if d.get("id") == doc_id), None)
    if not doc:
        raise HTTPException(404, "Document non trouvé")
    user.kyc_documents = json.dumps([d for d in docs if d.get("id") != doc_id])
    (_kyc_dir(user.id) / f"{doc['id']}.{doc['ext']}").unlink(missing_ok=True)
    db.add(AuditLog(user_id=admin.id, action=AuditAction.KYC_SUBMITTED, resource_type="user", resource_id=user.id,
                    ip_address="0.0.0.0", reason=f"Document supprimé ({doc.get('doc_type')})"))
    await db.commit()
    return {"success": True, "message": "Document supprimé"}


@router.post("/api/users/{user_id}/kyc/remind")
async def admin_user_kyc_remind(
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Envoie au joueur un rappel (notification) pour compléter son KYC."""
    from app.models.enums import NotificationType, NotificationChannel
    user = await _kyc_target(db, admin, user_id)
    if user.kyc_status == KYCStatus.VERIFIED:
        raise HTTPException(400, "Le KYC de cet utilisateur est déjà validé")
    await NotificationService(db, redis_client).create_notification(
        user_id=user.id, notification_type=NotificationType.ACCOUNT_ALERT,
        title="Vérification d'identité requise",
        message="Merci de fournir vos documents d'identité (KYC) pour continuer à utiliser votre compte.",
        channel=NotificationChannel.IN_APP,
    )
    db.add(AuditLog(user_id=admin.id, action=AuditAction.KYC_SUBMITTED, resource_type="user", resource_id=user.id,
                    ip_address="0.0.0.0", reason="Rappel KYC envoyé au joueur"))
    await db.commit()
    return {"success": True, "message": "Rappel envoyé au joueur"}


@router.get("/users/{user_id}/kyc", response_class=HTMLResponse)
async def admin_user_kyc_page(
    request: Request,
    user_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Page de gestion KYC d'un utilisateur"""
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(404, "Utilisateur non trouvé")

    history_result = await db.execute(
        select(AuditLog)
        .where(
            AuditLog.resource_type == "user",
            AuditLog.resource_id == user_id,
            AuditLog.action.in_([AuditAction.KYC_SUBMITTED, AuditAction.KYC_VERIFIED])
        )
        .order_by(AuditLog.created_at.desc())
        .limit(20)
    )
    logs = history_result.scalars().all()

    action_labels = {
        AuditAction.KYC_SUBMITTED: "Soumission",
        AuditAction.KYC_VERIFIED: "Validation",
    }
    kyc_history = [
        {
            "created_at": log.created_at,
            "action": action_labels.get(log.action, log.action),
            "status": "verified" if log.action == AuditAction.KYC_VERIFIED else "pending",
            "by": log.user_id,
            "comment": log.reason,
        }
        for log in logs
    ]

    return templates.TemplateResponse(request, "admin/users/kyc.html", {
        "active": "users",
        "user": user,
        "documents": _kyc_documents_for_page(user),
        "kyc_history": kyc_history,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


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

    ensure_can_manage(admin, await db.get(User, user_id))
    wallet_service = WalletService(db, redis_client)
    transaction = await wallet_service.credit(
        user_id=user_id,
        amount=Decimal(str(amount)),
        transaction_type="ADJUSTMENT",
        payment_method="cash",
        reference=f"ADJ-CREDIT-{now_utc().strftime('%Y%m%d%H%M%S')}",
    )
    transaction.created_by = admin.id

    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.DEPOSIT,
        resource_type="wallet",
        resource_id=transaction.wallet_id,
        ip_address="0.0.0.0",
        new_values={"credited": float(amount), "balance_after": float(transaction.balance_after)},
        reason=reason,
    )
    db.add(audit)
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
    from app.core.exceptions import InsufficientBalanceException

    ensure_can_manage(admin, await db.get(User, user_id))
    wallet_service = WalletService(db, redis_client)
    try:
        transaction = await wallet_service.debit(
            user_id=user_id,
            amount=Decimal(str(amount)),
            transaction_type="ADJUSTMENT",
            payment_method="cash",
            reference=f"ADJ-DEBIT-{now_utc().strftime('%Y%m%d%H%M%S')}",
        )
    except InsufficientBalanceException:
        raise HTTPException(400, "Solde insuffisant")
    transaction.created_by = admin.id

    audit = AuditLog(
        user_id=admin.id,
        action=AuditAction.WITHDRAWAL,
        resource_type="wallet",
        resource_id=transaction.wallet_id,
        ip_address="0.0.0.0",
        new_values={"debited": float(amount), "balance_after": float(transaction.balance_after)},
        reason=reason,
    )
    db.add(audit)
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


@router.get("/api/bureaus/export")
async def admin_bureaus_export(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export CSV des bureaux (mêmes filtres que la liste)."""
    import csv
    import io
    params = dict(request.query_params)
    query = select(Bureau).where(Bureau.is_deleted == False).order_by(Bureau.created_at.desc())
    if params.get("search"):
        like = params["search"]
        query = query.where(or_(Bureau.name.contains(like), Bureau.code.contains(like), Bureau.city.contains(like)))
    if params.get("city"):
        query = query.where(Bureau.city == params["city"])
    if params.get("status") == "active":
        query = query.where(Bureau.is_active == True)
    elif params.get("status") == "inactive":
        query = query.where(Bureau.is_active == False)
    if params.get("ids"):
        query = query.where(Bureau.id.in_([i for i in params["ids"].split(",") if i.strip()][:1000]))
    bureaus = (await db.execute(query.limit(10000))).scalars().all()
    counts = dict((await db.execute(
        select(User.bureau_id, func.count(User.id)).where(User.role.in_(AGENT_ROLES), User.is_deleted == False)
        .group_by(User.bureau_id))).all())

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Code", "Nom", "Ville", "Adresse", "Téléphone", "Statut", "Agents", "Caisse (HTG)", "Créé le"])
    for b in bureaus:
        writer.writerow([
            _csv_cell(b.code), _csv_cell(b.name), _csv_cell(b.city or ""), _csv_cell(getattr(b, "address", "") or ""),
            _csv_cell(getattr(b, "phone", "") or ""), "Actif" if b.is_active else "Inactif",
            counts.get(b.id, 0), float(b.cash_balance or 0), to_haiti(b.created_at).strftime("%d/%m/%Y %H:%M"),
        ])
    return Response(
        content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=bureaux_{now_haiti().strftime('%Y%m%d')}.csv"},
    )


@router.post("/api/bureaus/bulk/{action}")
async def admin_bureaus_bulk(
    action: str,
    bureau_ids: List[str] = Body(..., embed=True),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Active, désactive ou supprime plusieurs bureaux ; les bureaux non éligibles sont ignorés avec leur motif."""
    if action not in ("activate", "deactivate", "delete"):
        raise HTTPException(400, "Action inconnue")
    ids = list(dict.fromkeys(bureau_ids))[:200]
    if not ids:
        raise HTTPException(400, "Aucun bureau sélectionné")
    bureaus = {b.id: b for b in (await db.execute(
        select(Bureau).where(Bureau.id.in_(ids), Bureau.is_deleted == False).with_for_update())).scalars().all()}
    labels = {"activate": "activé", "deactivate": "désactivé", "delete": "supprimé"}
    done, skipped = 0, []
    for bid in ids:
        bureau = bureaus.get(bid)
        if bureau is None:
            skipped.append({"id": bid, "reason": "bureau introuvable"})
            continue
        if action == "delete":
            blocker = await _bureau_delete_blocker(db, bureau)
            if blocker:
                skipped.append({"id": bid, "reason": blocker})
                continue
            bureau.soft_delete(admin.id)
            bureau.is_active = False
        else:
            bureau.is_active = action == "activate"
        db.add(AuditLog(user_id=admin.id, action=AuditAction.USER_UPDATED, resource_type="bureau", resource_id=bureau.id,
                        ip_address="0.0.0.0", new_values={"bulk": action}, reason=f"Action groupée : {labels[action]}"))
        done += 1
    if not done:
        raise HTTPException(400, f"Aucun bureau {labels[action]} : " + "; ".join(x["reason"] for x in skipped[:5]))
    await db.commit()
    plural = "x" if done > 1 else ""
    message = f"{done} bureau{plural} {labels[action]}{'s' if done > 1 else ''}"
    if skipped:
        message += f" ; {len(skipped)} ignoré(s) ({'; '.join(x['reason'] for x in skipped[:3])})"
    return {"success": True, "message": message, "done": done, "skipped": skipped}


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


@router.get("/bureaus/{bureau_id}/agents", response_class=HTMLResponse)
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
    
    return templates.TemplateResponse(request, "admin/bureaus/agents.html", {
        "active": "bureaus",
        "bureau": await db.get(Bureau, bureau_id),
        "agents": agents,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/bureaus/{bureau_id}/tickets", response_class=HTMLResponse)
async def admin_bureau_tickets(
    request: Request,
    bureau_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Voir les tickets d'un bureau"""
    result = await db.execute(
        select(Ticket)
        .options(selectinload(Ticket.agent))  # lu par le template
        .where(Ticket.bureau_id == bureau_id)
        .order_by(Ticket.created_at.desc())
        .limit(100)
    )
    tickets = result.scalars().all()
    
    return templates.TemplateResponse(request, "admin/bureaus/tickets.html", {
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


@router.put("/api/settings/integrations")
async def admin_settings_integrations_update(
    integrations_data: dict,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour la configuration des intégrations de paiement (MonCash, NatCash, LEH)"""
    for key, value in integrations_data.items():
        await redis_client.setex(f"settings:{key}", 86400, str(value))

    return {"success": True, "message": "Intégrations mises à jour"}


@router.put("/api/settings/logging")
async def admin_settings_logging_update(
    logging_data: dict,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Met à jour la configuration de logging"""
    for key, value in logging_data.items():
        await redis_client.setex(f"settings:{key}", 86400, str(value))

    return {"success": True, "message": "Configuration de logging mise à jour"}


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


async def _redis_str(redis_client: redis.Redis, key: str, default):
    value = await redis_client.get(key)
    return value if value is not None else default


async def _redis_bool(redis_client: redis.Redis, key: str, default: bool) -> bool:
    value = await redis_client.get(key)
    if value is None:
        return default
    return str(value).lower() in ("true", "1", "on")


async def _redis_num(redis_client: redis.Redis, key: str, default):
    value = await redis_client.get(key)
    if value is None:
        return default
    try:
        return type(default)(value)
    except (TypeError, ValueError):
        return default


async def _get_general_settings(redis_client: redis.Redis) -> dict:
    """Reconstruit les paramètres généraux à partir de Redis (écrits par
    /api/settings/general, /api/settings/limits, /api/settings/maintenance,
    aucune valeur persistée en base - ce n'est qu'un cache 24h), avec repli
    sur app.config.settings."""
    return {
        "app_name": await _redis_str(redis_client, "settings:app_name", settings.APP_NAME),
        "app_version": await _redis_str(redis_client, "settings:app_version", settings.APP_VERSION),
        "timezone": await _redis_str(redis_client, "settings:timezone", "America/Port-au-Prince"),
        "currency": await _redis_str(redis_client, "settings:currency", "HTG"),
        "base_url": await _redis_str(redis_client, "settings:base_url", settings.BASE_URL),
        "frontend_url": await _redis_str(redis_client, "settings:frontend_url", settings.FRONTEND_URL),
        "max_daily_deposit": await _redis_num(redis_client, "settings:limits:max_daily_deposit", settings.MAX_DAILY_DEPOSIT),
        "max_daily_withdrawal": await _redis_num(redis_client, "settings:limits:max_daily_withdrawal", 200000),
        "max_single_bet": await _redis_num(redis_client, "settings:limits:max_single_bet", settings.MAX_SINGLE_BET),
        "kyc_required_amount": await _redis_num(redis_client, "settings:limits:kyc_required_amount", 10000),
        "maintenance_mode": await _redis_bool(redis_client, "settings:maintenance:mode", False),
        "maintenance_message": await _redis_str(redis_client, "settings:maintenance:message", ""),
        "moncash_enabled": await _redis_bool(redis_client, "settings:moncash_enabled", settings.MONCASH_ENABLED),
        "moncash_merchant_id": await _redis_str(redis_client, "settings:moncash_merchant_id", settings.MONCASH_MERCHANT_ID),
        "moncash_api_key": await _redis_str(redis_client, "settings:moncash_api_key", settings.MONCASH_API_KEY),
        "moncash_api_secret": await _redis_str(redis_client, "settings:moncash_api_secret", ""),
        "natcash_enabled": await _redis_bool(redis_client, "settings:natcash_enabled", settings.NATCASH_ENABLED),
        "natcash_merchant_id": await _redis_str(redis_client, "settings:natcash_merchant_id", settings.NATCASH_MERCHANT_ID),
        "natcash_api_key": await _redis_str(redis_client, "settings:natcash_api_key", settings.NATCASH_API_KEY),
        "leh_enabled": await _redis_bool(redis_client, "settings:leh_api_enabled", settings.LEH_ENABLED),
        "leh_api_url": await _redis_str(redis_client, "settings:leh_api_url", settings.LEH_API_URL or ""),
        "log_level": await _redis_str(redis_client, "settings:log_level", settings.LOG_LEVEL),
        "log_file": await _redis_str(redis_client, "settings:log_file", settings.LOG_FILE),
    }


async def _get_security_settings(redis_client: redis.Redis) -> dict:
    """Reconstruit les paramètres de sécurité à partir de Redis (écrits par
    /api/settings/security/*), avec repli sur app.config.settings."""
    whitelist_raw = await redis_client.get("settings:security:whitelist")
    ip_whitelist = json.loads(whitelist_raw) if whitelist_raw else []

    return {
        "two_factor_auth": await _redis_bool(redis_client, "settings:security:two_factor_auth", False),
        "session_timeout_minutes": await _redis_num(redis_client, "settings:security:session_timeout_minutes", 60),
        "max_login_attempts": await _redis_num(redis_client, "settings:security:max_login_attempts", 5),
        "password_policy": {
            "min_length": await _redis_num(redis_client, "settings:password:min_length", 8),
            "require_uppercase": await _redis_bool(redis_client, "settings:password:require_uppercase", True),
            "require_lowercase": await _redis_bool(redis_client, "settings:password:require_lowercase", True),
            "require_numbers": await _redis_bool(redis_client, "settings:password:require_numbers", True),
            "require_special": await _redis_bool(redis_client, "settings:password:require_special", False),
        },
        "ip_whitelist": ip_whitelist,
        "rate_limit_requests": await _redis_num(redis_client, "settings:ratelimit:rate_limit_requests", settings.RATE_LIMIT_REQUESTS),
        "rate_limit_period": await _redis_num(redis_client, "settings:ratelimit:rate_limit_period", settings.RATE_LIMIT_PERIOD_SECONDS),
    }


async def _get_active_sessions(redis_client: redis.Redis, db: AsyncSession) -> list:
    """Sessions actives stockées dans Redis (session:*)."""
    keys = await redis_client.keys("session:*")
    sessions = []
    for key in keys:
        raw = await redis_client.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        user_id = data.get("user_id")
        user_name = None
        if user_id:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            user_name = (user.full_name or user.phone) if user else None
        sessions.append({
            "id": key.replace("session:", "", 1) if isinstance(key, str) else key,
            "user_name": user_name or "Utilisateur inconnu",
            "ip_address": data.get("ip_address", "-"),
            "created_at": datetime.fromisoformat(data["created_at"]) if data.get("created_at") else now_utc(),
            "last_activity": datetime.fromisoformat(data["last_activity"]) if data.get("last_activity") else now_utc(),
        })
    return sessions


@router.get("/settings", name="admin_settings")
async def admin_settings_redirect(admin: User = Depends(get_current_admin)):
    """Redirige vers l'onglet Général des paramètres"""
    return RedirectResponse(url="/admin/settings/general", status_code=303)


@router.get("/settings/general", response_class=HTMLResponse)
async def admin_settings_general(
    request: Request,
    admin: User = Depends(get_current_admin),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Page des paramètres généraux"""
    settings_dict = await _get_general_settings(redis_client)
    worker_count = 0
    try:
        from app.workers.celery import celery_app
        active = celery_app.control.inspect().active()
        worker_count = len(active) if active else 0
    except Exception:
        worker_count = 0

    return templates.TemplateResponse(request, "admin/settings/general.html", {
        "active": "settings",
        "settings": settings_dict,
        "worker_count": worker_count,
        "env": settings.ENVIRONMENT,
        "uptime": "N/A",
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


@router.get("/settings/security", response_class=HTMLResponse)
async def admin_settings_security(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Page des paramètres de sécurité"""
    security_dict = await _get_security_settings(redis_client)
    active_sessions = await _get_active_sessions(redis_client, db)

    logs_result = await db.execute(
        select(AuditLog)
        .where(AuditLog.action.in_([AuditAction.LOGIN, AuditAction.LOGIN_FAILED, AuditAction.LOGOUT]))
        .order_by(AuditLog.created_at.desc())
        .limit(20)
    )
    logs = logs_result.scalars().all()
    security_logs = []
    for log in logs:
        user_name = None
        if log.user_id:
            user_result = await db.execute(select(User).where(User.id == log.user_id))
            user = user_result.scalar_one_or_none()
            user_name = user.full_name if user else None
        security_logs.append({
            "created_at": log.created_at,
            "action": log.action.value if hasattr(log.action, "value") else log.action,
            "user_name": user_name,
            "user_id": log.user_id,
            "ip_address": log.ip_address,
        })

    return templates.TemplateResponse(request, "admin/settings/security.html", {
        "active": "settings",
        "security": security_dict,
        "active_sessions": active_sessions,
        "security_logs": security_logs,
        "admin_name": admin.full_name or admin.email,
        "admin_role": admin.role,
        "version": "1.0.0"
    })


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
    await broadcast_draw_result({"type": "keno_draw", **{k: v for k, v in result.items() if k != "winner_bet_ids"}})
    
    return {"success": True, "message": f"Tirage #{draw.draw_number} déclenché", "draw_id": draw.id}


@router.post("/api/keno/draws/{draw_id}/trigger")
async def admin_keno_trigger_specific_draw(
    draw_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Tire maintenant un tirage en attente et règle ses paris (même
    règlement que le worker : verrou, paiement unique par pari)."""
    from app.services.keno_service import KenoService
    from app.api.websockets.manager import broadcast_draw_result

    try:
        result = await KenoService(db, redis_client).execute_draw(draw_id)
    except NotFoundException:
        raise HTTPException(404, "Tirage non trouvé")
    except GameException:
        raise HTTPException(400, "Ce tirage n'est plus en attente")

    await broadcast_draw_result({"type": "keno_draw", **{k: v for k, v in result.items() if k != "winner_bet_ids"}})
    return {"success": True, "message": f"Tirage #{result['draw_number']} déclenché"}


@router.post("/api/keno/draws/schedule")
async def admin_keno_schedule_draws(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Planifie les tirages Keno pour les prochaines 24h"""
    from app.services.keno_service import KenoService

    created = await KenoService(db, redis_client).schedule_draws(hours=24)
    await db.commit()
    return {"success": True, "message": f"{created} tirage(s) planifié(s)"}


@router.post("/api/keno/draws/cancel-pending")
async def admin_keno_cancel_pending_draws(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Annule les tirages Keno en attente qui n'ont AUCUN pari (les autres
    doivent être tirés : les mises ne seraient pas rendues)."""
    from app.services.keno_service import KenoService

    count = await KenoService(db, redis_client).cancel_pending_draws_without_bets(by=admin.id)
    await db.commit()
    return {"success": True, "message": f"{count} tirage(s) sans pari annulé(s)"}


@router.post("/api/keno/draws/{draw_id}/cancel")
async def admin_keno_cancel_draw(
    draw_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Annule un tirage Keno en attente et rembourse ses paris."""
    try:
        result = await KenoService(db, redis_client).cancel_draw(draw_id, by=admin.id, reason="Annulation admin")
    except NotFoundException:
        raise HTTPException(404, "Tirage non trouvé")
    except GameException as e:
        raise HTTPException(400, e.detail)
    await db.commit()
    return {"success": True, "message": f"Tirage #{result['draw_number']} annulé : {result['refunded_bets']} pari(s) remboursé(s)",
            **result}


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
            Promotion.start_date <= now_utc(),
            Promotion.end_date >= now_utc()
        )
    )
    active = active_result.scalar() or 0
    
    # En attente
    pending_result = await db.execute(
        select(func.count(Promotion.id))
        .where(
            Promotion.status == PromotionStatus.ACTIVE,
            Promotion.start_date > now_utc()
        )
    )
    pending = pending_result.scalar() or 0
    
    # Expirées
    expired_result = await db.execute(
        select(func.count(Promotion.id))
        .where(
            or_(
                Promotion.status == PromotionStatus.EXPIRED,
                Promotion.end_date < now_utc()
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


@router.post("/api/promotions/bulk/{action}")
async def admin_promotions_bulk(
    action: str,
    promo_ids: List[str] = Body(..., embed=True),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Active, met en pause ou supprime plusieurs promotions (suppression refusée si déjà utilisées)."""
    if action not in ("activate", "pause", "delete"):
        raise HTTPException(400, "Action inconnue")
    ids = list(dict.fromkeys(promo_ids))[:200]
    if not ids:
        raise HTTPException(400, "Aucune promotion sélectionnée")
    promos = {x.id: x for x in (await db.execute(select(Promotion).where(Promotion.id.in_(ids)))).scalars().all()}
    labels = {"activate": "activée", "pause": "mise en pause", "delete": "supprimée"}
    done, skipped = 0, []
    for pid in ids:
        promo = promos.get(pid)
        if promo is None:
            skipped.append({"id": pid, "reason": "promotion introuvable"})
            continue
        if action == "delete":
            blocker = await _promotion_delete_blocker(db, promo)
            if blocker:
                skipped.append({"id": pid, "reason": blocker})
                continue
            await db.delete(promo)
        else:
            promo.status = PromotionStatus.ACTIVE if action == "activate" else PromotionStatus.PAUSED
        db.add(AuditLog(user_id=admin.id, action=AuditAction.USER_UPDATED, resource_type="promotion", resource_id=pid,
                        ip_address="0.0.0.0", new_values={"bulk": action}, reason=f"Action groupée : {labels[action]}"))
        done += 1
    if not done:
        raise HTTPException(400, "Aucune promotion traitée : " + "; ".join(x["reason"] for x in skipped[:5]))
    await db.commit()
    message = f"{done} promotion(s) {labels[action]}(s)" if action != "pause" else f"{done} promotion(s) mise(s) en pause"
    if skipped:
        message += f" ; {len(skipped)} ignorée(s) ({'; '.join(x['reason'] for x in skipped[:3])})"
    return {"success": True, "message": message, "done": done, "skipped": skipped}


# ==================== API SUPPLEMENTAIRES POUR AUDIT ====================

@router.get("/api/audit/statistics")
async def admin_audit_statistics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Statistiques des logs d'audit"""
    today = today_haiti()
    today_start = local_date_start_utc(today)
    
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
        select(AuditLog).options(selectinload(AuditLog.user)).where(AuditLog.id == log_id)
    )
    log = result.scalar_one_or_none()
    
    if not log:
        raise HTTPException(404, "Log non trouvé")
    
    return {
        "id": log.id,
        "user_id": log.user_id,
        "user_name": log.user.full_name if log.user else None,
        "agent_id": log.agent_id,
        "action": getattr(log.action, "value", log.action),
        "action_color": "info" if str(getattr(log.action, "value", log.action)).lower() in ["login", "logout"] else "purple" if str(getattr(log.action, "value", log.action)).lower() in ["bet_placed", "bet_settled"] else "success" if str(getattr(log.action, "value", log.action)).lower() in ["deposit", "withdrawal"] else "danger" if str(getattr(log.action, "value", log.action)).lower() in ["user_blocked", "account_frozen"] else "gray",
        "resource_type": log.resource_type,
        "resource_id": log.resource_id,
        "old_values": log.old_values,
        "new_values": log.new_values,
        "reason": log.reason,
        "metadata": log.extra_data,
        "ip_address": log.ip_address,
        "user_agent": log.user_agent,
        "session_id": log.session_id,
        "leh_exported": log.leh_exported,
        "leh_exported_at": log.leh_exported_at,
        "created_at": log.created_at,
        "updated_at": log.updated_at
    }


@router.post("/api/audit/logs/bulk/leh-export")
async def admin_audit_logs_bulk_leh_export(
    log_ids: List[str] = Body(..., embed=True),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Marque plusieurs logs comme exportés vers la LEH"""
    result = await db.execute(
        update(AuditLog)
        .where(AuditLog.id.in_(log_ids))
        .values(leh_exported=True, leh_exported_at=now_utc())
    )
    await db.commit()
    
    return {"success": True, "message": f"{result.rowcount} logs marqués comme exportés"}


@router.post("/api/audit/logs/{log_id}/leh-export")
async def admin_audit_log_leh_export(
    log_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Marque un log comme exporté vers la LEH"""
    result = await db.execute(
        select(AuditLog).options(selectinload(AuditLog.user)).where(AuditLog.id == log_id)
    )
    log = result.scalar_one_or_none()
    
    if not log:
        raise HTTPException(404, "Log non trouvé")
    
    log.leh_exported = True
    log.leh_exported_at = now_utc()
    
    await db.commit()
    
    return {"success": True, "message": "Log marqué comme exporté"}


@router.get("/api/audit/export")
async def admin_audit_export_csv(
    request: Request,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Export CSV des logs d'audit (mêmes filtres que la page)."""
    import csv
    import io

    params = dict(request.query_params)
    query = select(AuditLog).options(selectinload(AuditLog.user)).order_by(AuditLog.created_at.desc())

    if params.get('search'):
        query = query.where(or_(AuditLog.resource_id.contains(params['search']), AuditLog.reason.contains(params['search'])))
    if params.get('action') and params['action'] in {a.value for a in AuditAction}:
        query = query.where(AuditLog.action == AuditAction(params['action']))
    if params.get('resource_type'):
        query = query.where(AuditLog.resource_type == params['resource_type'])
    if params.get('user_id'):
        query = query.where(AuditLog.user_id == params['user_id'])
    if params.get('ip_address'):
        query = query.where(AuditLog.ip_address == params['ip_address'])
    if params.get('leh_exported') in ('true', 'false'):
        query = query.where(AuditLog.leh_exported == (params['leh_exported'] == 'true'))
    if params.get('ids'):
        query = query.where(AuditLog.id.in_([i for i in params['ids'].split(',') if i.strip()][:1000]))
    if params.get('start_date'):
        query = query.where(AuditLog.created_at >= local_date_start_utc(params['start_date']))
    if params.get('end_date'):
        query = query.where(AuditLog.created_at < local_date_end_utc(params['end_date']))

    logs = (await db.execute(query.limit(10000))).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow([
        "ID", "Date", "Utilisateur", "Action", "Ressource", "ID ressource",
        "Anciennes valeurs", "Nouvelles valeurs", "Raison", "IP", "Export LEH"
    ])
    for log in logs:
        who = (log.user.full_name or log.user.email or log.user.phone) if log.user else "Système"
        writer.writerow([
            log.id,
            to_haiti(log.created_at).strftime("%d/%m/%Y %H:%M:%S"),
            _csv_cell(who),
            getattr(log.action, 'value', log.action),
            _csv_cell(log.resource_type or ""),
            _csv_cell(log.resource_id or ""),
            _csv_cell(json.dumps(log.old_values, ensure_ascii=False) if log.old_values else ""),
            _csv_cell(json.dumps(log.new_values, ensure_ascii=False) if log.new_values else ""),
            _csv_cell(log.reason or ""),
            _csv_cell(log.ip_address or ""),
            "Oui" if log.leh_exported else "Non"
        ])

    return Response(
        content="﻿" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=audit_logs_{now_haiti().strftime('%Y%m%d')}.csv"}
    )


# ==================== API SUPPLEMENTAIRES POUR REPORTS ====================

@router.get("/api/reports/financial/export")
async def admin_reports_financial_export(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    year: Optional[int] = None,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Export CSV (Excel) : période choisie, détail par jeu et par bureau, revenu mois par mois."""
    data = await _financial_report(db, start_date, end_date, year, redis_client)
    p = data["period"]
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"King Paryaj - Finances du {data['start_date']} au {data['end_date']}"])
    w.writerow([])
    w.writerow(["ARGENT QUI ENTRE", "HTG"])
    w.writerow(["Ventes de tickets au guichet", p["in"]["ticket_sales"]])
    w.writerow(["Recharges de tickets", p["in"]["recharges"]])
    w.writerow(["Dépôts espèces sur comptes", p["in"]["deposits_cash"]])
    w.writerow(["Dépôts MonCash / NatCash / autres", p["in"]["deposits_mobile"]])
    w.writerow(["Total entrées", p["in"]["total"]])
    w.writerow(["ARGENT QUI SORT", "HTG"])
    w.writerow(["Paiements des tickets au guichet (partiels compris)", p["out"]["ticket_payouts"]])
    w.writerow(["Soldes rendus à l'annulation", p["out"]["cancel_refunds"]])
    w.writerow(["Retraits des comptes", p["out"]["withdrawals"]])
    w.writerow(["Total sorties", p["out"]["total"]])
    w.writerow(["Solde (entrées - sorties)", p["cash_flow"]])
    w.writerow([])
    w.writerow(["REVENU DU SYSTÈME", "Paris", "Mises", "Gains (payés + dus)", "Revenu", "Marge %"])
    for g in p["games"]:
        w.writerow([g["label"], g["bets"], g["stakes"], g["wins"], g["revenue"], g["margin"]])
    w.writerow(["Total", "", p["stakes"], p["wins"], p["revenue"], p["margin"]])
    w.writerow(["Gains payés aux joueurs", p["wins_paid"]])
    w.writerow(["Gains encore dus", p["wins_due"]])
    w.writerow(["GAINS GAGNÉS : OÙ EN SONT-ILS ?", "HTG"])
    w.writerow(["Payés au guichet", p["wins_split"]["paid"]])
    w.writerow(["Encore dus (tickets valables)", p["wins_split"]["due"]])
    w.writerow(["Sur comptes joueurs (dus)", p["wins_split"]["accounts"]])
    w.writerow(["Jamais réclamés (restent à la maison)", p["wins_split"]["unclaimed"]])
    w.writerow(["Bonus offerts", "", "", "", p["bonus"]])
    w.writerow(["Commissions des agents", "", "", "", p["commissions"]])
    w.writerow(["Revenu net", "", "", "", p["net_revenue"]])
    w.writerow([])
    w.writerow(["COMMISSIONS DES AGENTS", "Téléphone", "Taux %", "Ventes", "Commission"])
    for a in data["agent_commissions"]:
        w.writerow([a["agent"], a["phone"], a["rate"], a["sales"], a["commission"]])
    w.writerow([])
    w.writerow(["PAR BUREAU", "Ventes tickets", "Dépôts espèces", "Gains payés", "Retraits espèces", "Entrées", "Sorties", "Solde", "Écarts de caisse"])
    for b in data["bureaus"]:
        w.writerow([b["bureau"], b["ticket_sales"], b["deposits"], b["ticket_payouts"], b["withdrawals"], b["in"], b["out"], b["cash_flow"], b["cash_gaps"]])
    w.writerow([])
    w.writerow([f"REVENU PAR MOIS {data['year']}", "Entrées", "Sorties", "Solde", "Mises", "Gains payés", "Gains dus", "Revenu", "Bonus", "Commissions", "Revenu net", "Marge %"])
    for m in data["months"]:
        w.writerow([m["label"], m["in"]["total"], m["out"]["total"], m["cash_flow"], m["stakes"], m["wins_paid"], m["wins_due"], m["revenue"], m["bonus"], m["commissions"], m["net_revenue"], m["margin"]])
    t = data["year_totals"]
    w.writerow([f"Total {data['year']}", t["in"], t["out"], t["cash_flow"], t["stakes"], t["wins_paid"], t["wins_due"], t["revenue"], t["bonus"], t["commissions"], t["net_revenue"], t["margin"]])
    content = "\ufeff" + buf.getvalue()  # BOM : accents corrects dans Excel
    filename = f"finances_{data['start_date']}_{data['end_date']}.csv"
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
    params = dict(request.query_params)
    start_date = params.get('start_date', today_haiti().strftime('%Y-%m-%d'))
    end_date = params.get('end_date', today_haiti().strftime('%Y-%m-%d'))
    
    # Générer le rapport
    report = {
        "header": {
            "operator": "Parier Keno Haïti",
            "period": {"start": start_date, "end": end_date},
            "generated_at": now_utc().isoformat()
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
    now = now_utc()
    
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
    query = select(Ticket).options(selectinload(Ticket.bureau), selectinload(Ticket.agent)).order_by(Ticket.created_at.desc())
    
    if params.get('search'):
        query = query.where(
            or_(
                Ticket.ticket_number.contains(params['search']),
                Ticket.player_name.contains(params['search']),
                Ticket.player_phone.contains(params['search'])
            )
        )
    if params.get('status') and params['status'].lower() in {x.value for x in TicketStatus}:
        query = query.where(Ticket.status == TicketStatus(params['status'].lower()))
    if params.get('ids'):
        query = query.where(Ticket.id.in_([i for i in params['ids'].split(',') if i.strip()][:1000]))
    if params.get('start_date'):
        start = local_date_start_utc(params['start_date'])
        query = query.where(Ticket.created_at >= start)
    if params.get('end_date'):
        end = local_date_end_utc(params['end_date'])
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
            _csv_cell(ticket.ticket_number),
            _csv_cell(ticket.player_name or ""),
            _csv_cell(ticket.player_phone or ""),
            float(ticket.initial_amount),
            float(ticket.balance),
            getattr(ticket.status, 'value', ticket.status),
            _csv_cell(ticket.bureau.name if ticket.bureau else ""),
            _csv_cell(ticket.agent.full_name if ticket.agent else ""),
            ticket.created_at.isoformat(),
            ticket.expires_at.isoformat(),
            ticket.paid_at.isoformat() if ticket.paid_at else ""
        ])
    
    return Response(
        content="﻿" + output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=tickets_{now_haiti().strftime('%Y%m%d')}.csv"
        }
    )


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


# Routes « bulk » déclarées AVANT /api/tickets/{ticket_id}/... : sinon « bulk »
# est pris pour un identifiant de ticket (404 « Ticket non trouvé »).
@router.post("/api/tickets/bulk/payout")
async def admin_tickets_bulk_payout(
    ticket_ids: List[str] = Body(..., embed=True),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Paye plusieurs tickets"""
    result = await db.execute(
        select(Ticket).where(Ticket.id.in_(ticket_ids), Ticket.status == TicketStatus.ACTIVE).with_for_update()
    )
    tickets = result.scalars().all()
    
    total_amount = Decimal("0")
    paid_count = skipped = 0
    for ticket in tickets:
        if ticket.balance <= 0 or ticket.is_expired():
            skipped += 1
            continue
        if await TicketService(db, None).pending_bets_count(ticket.id):
            skipped += 1  # pari en attente de tirage : payé plus tard
            continue
        amount = ticket.record_payout(ticket.balance, admin.id)  # montant payé enregistré
        total_amount += amount
        paid_count += 1
        if ticket.bureau_id:
            bureau = await db.get(Bureau, ticket.bureau_id)
            if bureau:
                bureau.cash_balance = Bureau.cash_balance - (amount)  # atomique en base ; (avant : débitait 0, le solde étant déjà remis à 0)
    
    await db.commit()
    
    message = f"{paid_count} ticket(s) payé(s) pour un total de {total_amount} HTG"
    if skipped:
        message += f" ; {skipped} ignoré(s) (sans solde, expiré ou pari en attente)"
    return {"success": True, "message": message, "paid": paid_count, "skipped": skipped, "total": float(total_amount)}


@router.post("/api/tickets/bulk/cancel")
async def admin_tickets_bulk_cancel(
    ticket_ids: List[str] = Body(..., embed=True),
    reason: str = Body(...),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Annule plusieurs tickets"""
    result = await db.execute(
        select(Ticket).where(Ticket.id.in_(ticket_ids), Ticket.status == TicketStatus.ACTIVE).with_for_update()
    )
    tickets = result.scalars().all()
    
    total_amount = Decimal("0")
    skipped = 0
    for ticket in tickets:
        if (await TicketService(db, None).release_pending_bets(ticket, admin.id))["blocked"]:
            skipped += 1  # pari dont le tirage est déjà lancé : annulation après le résultat
            continue
        amount = ticket.record_cancellation(admin.id)  # sortie d'espèces datée (mises des paris annulés comprises)
        total_amount += amount
        if ticket.bureau_id and amount > 0:
            bureau = await db.get(Bureau, ticket.bureau_id)
            if bureau:
                bureau.cash_balance = Bureau.cash_balance - (amount)  # atomique en base ; (avant : débitait 0, le solde étant déjà remis à 0)
    
    await db.commit()
    
    message = f"{len(tickets) - skipped} ticket(s) annulé(s) pour un total de {total_amount} HTG"
    if skipped:
        message += f" ; {skipped} ignoré(s) (tirage déjà lancé)"
    return {"success": True, "message": message, "skipped": skipped, "total": float(total_amount)}



@router.post("/api/tickets/{ticket_id}/payout")
async def admin_ticket_payout(
    ticket_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Paye un ticket"""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update()  # jamais payé deux fois
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        raise HTTPException(400, "Ce ticket n'est plus actif")
    
    if ticket.balance <= 0:
        raise HTTPException(400, "Aucun solde à payer")

    pending = await TicketService(db, None).pending_bets_count(ticket.id)
    if pending:
        raise HTTPException(400, f"Résultat pas encore connu pour {pending} pari(s) de ce ticket : paiement après le tirage")
    
    # Marquer comme payé (montant payé enregistré)
    amount = ticket.record_payout(ticket.balance, admin.id)
    
    # Mettre à jour la caisse du bureau
    if ticket.bureau_id:
        bureau_result = await db.execute(
            select(Bureau).where(Bureau.id == ticket.bureau_id)
        )
        bureau = bureau_result.scalar_one()
        bureau.cash_balance = Bureau.cash_balance - (amount)  # atomique en base
    
    await db.commit()
    
    return {"success": True, "message": f"Ticket payé: {amount} HTG", "amount": float(amount)}


@router.post("/api/tickets/{ticket_id}/cancel")
async def admin_ticket_cancel(
    ticket_id: str,
    reason: str = Body(..., embed=True),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Annule un ticket"""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update()
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        raise HTTPException(400, "Seuls les tickets actifs peuvent être annulés")
    
    # Paris en attente d'un tirage non lancé : annulés avec le ticket (mise rendue) ; tirage déjà lancé : refus
    released = await TicketService(db, None).release_pending_bets(ticket, admin.id)
    if released["blocked"]:
        raise HTTPException(
            400,
            "Impossible d'annuler : un pari de ce ticket attend son tirage, déjà lancé. "
            "Annulation possible une fois le résultat connu.",
        )

    # Rembourser le solde (sortie d'espèces datée)
    amount = ticket.record_cancellation(admin.id)
    
    # Mettre à jour la caisse du bureau (débit)
    if ticket.bureau_id:
        bureau_result = await db.execute(
            select(Bureau).where(Bureau.id == ticket.bureau_id)
        )
        bureau = bureau_result.scalar_one()
        bureau.cash_balance = Bureau.cash_balance - (amount)  # atomique en base
    
    await db.commit()
    
    note = f" ({released['released']} pari(s) annulé(s))" if released["released"] else ""
    return {"success": True, "message": f"Ticket annulé{note}. Remboursement de {amount} HTG"}


# ==================== API SUPPLEMENTAIRES POUR TRANSACTIONS ====================

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
    
    if params.get('search'):
        query = query.where(Transaction.reference.contains(params['search']))
    if _parse_enum(TransactionType, params.get('type') or params.get('transaction_type')):
        query = query.where(Transaction.transaction_type == _parse_enum(TransactionType, params.get('type') or params.get('transaction_type')))
    if _parse_enum(TransactionStatus, params.get('status')):
        query = query.where(Transaction.status == _parse_enum(TransactionStatus, params['status']))
    if _parse_enum(PaymentMethod, params.get('method')):
        query = query.where(Transaction.payment_method == _parse_enum(PaymentMethod, params['method']))
    if params.get('start_date'):
        start = local_date_start_utc(params['start_date'])
        query = query.where(Transaction.created_at >= start)
    if params.get('end_date'):
        end = local_date_end_utc(params['end_date'])
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
            getattr(tx.transaction_type, 'value', tx.transaction_type),
            getattr(tx.payment_method, 'value', tx.payment_method) or "",
            float(tx.amount),
            float(tx.fee),
            float(tx.balance_before),
            float(tx.balance_after),
            getattr(tx.status, 'value', tx.status),
            tx.user_id,
            tx.created_at.isoformat()
        ])
    
    return Response(
        content="﻿" + output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=transactions_{now_haiti().strftime('%Y%m%d')}.csv"
        }
    )


@router.get("/api/transactions/statistics")
async def admin_transactions_statistics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Statistiques des transactions"""
    today = today_haiti()
    today_start = local_date_start_utc(today)
    
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
    """Confirme manuellement une transaction MonCash/NatCash en attente
    (paiement bloqué côté fournisseur, etc.). Délègue à WalletService pour
    rester cohérent avec la confirmation automatique par webhook : mêmes
    règles de crédit/idempotence, pas de logique dupliquée et divergente."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == transaction_id)
    )
    transaction = result.scalar_one_or_none()

    if not transaction:
        raise HTTPException(404, "Transaction non trouvée")

    if transaction.status != TransactionStatus.PENDING:
        raise HTTPException(400, "Seules les transactions en attente peuvent être confirmées")

    if not transaction.external_reference:
        raise HTTPException(400, "Transaction sans référence fournisseur : rien à confirmer")

    wallet_service = WalletService(db, redis_client)
    if transaction.transaction_type == TransactionType.DEPOSIT:
        await wallet_service.confirm_deposit(transaction.external_reference)
    elif transaction.transaction_type == TransactionType.WITHDRAWAL:
        await wallet_service.confirm_withdrawal(transaction.external_reference)
    else:
        raise HTTPException(400, f"Type de transaction non confirmable: {transaction.transaction_type}")

    await db.commit()

    return {"success": True, "message": "Transaction confirmée avec succès"}


@router.post("/api/transactions/{transaction_id}/cancel")
async def admin_transaction_cancel(
    transaction_id: str,
    reason: str = Body(..., embed=True),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Annule manuellement une transaction MonCash/NatCash en attente.
    Délègue à WalletService (même logique que l'échec signalé par webhook :
    un retrait annulé rembourse les fonds déjà réservés)."""
    result = await db.execute(
        select(Transaction).where(Transaction.id == transaction_id)
    )
    transaction = result.scalar_one_or_none()

    if not transaction:
        raise HTTPException(404, "Transaction non trouvée")

    if transaction.status != TransactionStatus.PENDING:
        raise HTTPException(400, "Seules les transactions en attente peuvent être annulées")

    if not transaction.external_reference:
        raise HTTPException(400, "Transaction sans référence fournisseur : rien à annuler")

    wallet_service = WalletService(db, redis_client)
    if transaction.transaction_type == TransactionType.DEPOSIT:
        await wallet_service.fail_deposit(transaction.external_reference, reason=reason)
    elif transaction.transaction_type == TransactionType.WITHDRAWAL:
        await wallet_service.fail_withdrawal(transaction.external_reference, reason=reason)
    else:
        raise HTTPException(400, f"Type de transaction non annulable: {transaction.transaction_type}")

    await db.commit()

    return {"success": True, "message": "Transaction annulée avec succès"}


# ==================== FONCTIONS AUXILIAIRES ====================

async def _get_dashboard_stats(db: AsyncSession, redis_client=None) -> dict:
    """Statistiques du tableau de bord (journée = jour civil d'Haïti)."""
    today_start, tomorrow_start = today_bounds_utc()

    # Utilisateurs
    users_result = await db.execute(
        select(
            func.count(User.id).label("total"),
            func.count().filter(User.created_at >= today_start).label("new_today")
        ).where(User.is_deleted == False)
    )
    users = users_result.one()

    # Transactions (total et du jour)
    is_today = and_(Transaction.created_at >= today_start, Transaction.created_at < tomorrow_start)
    volume_types = [TransactionType.DEPOSIT, TransactionType.WITHDRAWAL, TransactionType.WIN]
    tx_result = await db.execute(
        select(
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type.in_(volume_types)), 0).label("volume"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN), 0).label("wins"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type.in_(volume_types), is_today), 0).label("today_volume"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN, is_today), 0).label("today_wins"),
        )
        .where(Transaction.status == TransactionStatus.COMPLETED)
    )
    tx = tx_result.one()

    # Gains des joueurs : paris réglés de tous les jeux, tickets ET comptes
    # (les transactions WIN ne couvrent que les comptes joueurs)
    from app.services.finance_report_service import FinanceReportService

    finance = FinanceReportService(db)
    (today_totals,) = await finance.game_totals([today_start, tomorrow_start])
    # Avant aujourd'hui : tout l'historique, recalculé au plus toutes les 5 min
    # (la page se rafraîchit toutes les 30 s)
    cache_key = f"cache:dashboard:wins_paid_before:{today_start.isoformat()}"
    before = None
    if redis_client is not None:
        try:
            raw = await redis_client.get(cache_key)
            before = float(raw) if raw is not None else None
        except Exception:
            before = None
    if before is None:
        (history,) = await finance.game_totals([datetime(2000, 1, 1), today_start])
        before = history["wins_paid"]
        if redis_client is not None:
            try:
                await redis_client.setex(cache_key, 300, str(before))
            except Exception:
                pass
    total_wins = round(before + today_totals["wins_paid"], 2)  # gains remis aux joueurs

    # Paris du jour, tous jeux confondus (Keno, Lucky6, Horse Races)
    from app.models.game import GameBet

    today_bets = 0
    for model, column in ((KenoBet, KenoBet.placed_at), (GameBet, GameBet.placed_at)):
        try:
            # SAVEPOINT : si une table manque (ex. migration Horse Races pas
            # encore appliquée), seul ce comptage est ignoré, la page s'affiche.
            async with db.begin_nested():
                result = await db.execute(
                    select(func.count(model.id)).where(column >= today_start, column < tomorrow_start)
                )
                today_bets += result.scalar() or 0
        except DBAPIError as e:  # table absente (Postgres : ProgrammingError, SQLite : OperationalError)
            logger.warning(f"Tableau de bord : table {model.__tablename__} indisponible ({e.orig.__class__.__name__})")

    # Tickets actifs et tickets qui expirent dans les 48 h
    now = now_utc()
    tickets_result = await db.execute(
        select(
            func.count(Ticket.id).label("active"),
            func.count(Ticket.id).filter(Ticket.expires_at <= now + timedelta(hours=48)).label("expiring_soon"),
        ).where(Ticket.status == TicketStatus.ACTIVE, Ticket.expires_at > now)
    )
    tickets = tickets_result.one()

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
            "total_volume": float(tx.volume or 0),
            "today_volume": float(tx.today_volume or 0),
            "total_wins": total_wins,
            "today_wins": today_totals["wins_paid"],
        },
        "games": {
            "today_bets": today_bets
        },
        "tickets": {
            "active": tickets.active or 0,
            "expiring_soon": tickets.expiring_soon or 0
        },
        "bureaus": {
            "total": bureaus.total or 0,
            "active": bureaus.active or 0
        }
    }


async def _get_recent_transactions(db: AsyncSession, limit: int = 10) -> list:
    """Dernières transactions, au format attendu par admin/dashboard.html
    (type et statut en minuscules, nom du joueur)."""
    result = await db.execute(
        select(Transaction, User)
        .outerjoin(User, User.id == Transaction.user_id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    rows = []
    for tx, user in result.all():
        rows.append({
            "user_id": tx.user_id or "",
            "user_name": (user.full_name or user.phone) if user else None,
            "type": getattr(tx.transaction_type, "value", str(tx.transaction_type)).lower(),
            "status": getattr(tx.status, "value", str(tx.status)).lower(),
            "amount": float(tx.amount or 0),
            "created_at": tx.created_at,
        })
    return rows


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
    tomorrow = now_utc() + timedelta(days=1)
    tickets_result = await db.execute(
        select(func.count(Ticket.id))
        .where(
            and_(
                Ticket.status == TicketStatus.ACTIVE,
                Ticket.expires_at <= tomorrow,
                Ticket.expires_at > now_utc()
            )
        )
    )
    expiring_tickets = tickets_result.scalar() or 0
    
    if expiring_tickets > 0:
        alerts.append({
            "level": "warning",
            "message": f"{expiring_tickets} tickets expirent dans les prochaines 24h",
            "created_at": now_utc()
        })
    
    # Sessions de caisse ouvertes depuis plus de 12h
    sessions_result = await db.execute(
        select(func.count(CashierSession.id))
        .where(
            and_(
                CashierSession.status == "OPEN",
                CashierSession.opened_at < now_utc() - timedelta(hours=12)
            )
        )
    )
    open_sessions = sessions_result.scalar() or 0
    
    if open_sessions > 0:
        alerts.append({
            "level": "warning",
            "message": f"{open_sessions} sessions de caisse ouvertes depuis plus de 12h",
            "created_at": now_utc()
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
    end_date = now_utc()
    start_date = local_date_start_utc(today_haiti() - timedelta(days=period - 1))
    
    # Transactions par jour
    result = await db.execute(
        select(
            local_day(Transaction.created_at).label("day"),
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
        .group_by(local_day(Transaction.created_at))
        .order_by(local_day(Transaction.created_at))
    )
    rows = {row.day.strftime("%d/%m"): row for row in result.all()}

    # Gains des joueurs par jour : paris réglés de tous les jeux (tickets + comptes)
    from app.services.finance_report_service import FinanceReportService

    days = [today_haiti() - timedelta(days=period - 1 - i) for i in range(period)]
    bounds = [local_date_start_utc(d) for d in days] + [local_date_end_utc(days[-1])]
    daily_games = await FinanceReportService(db).game_totals(bounds)

    labels = []
    deposits = []
    withdrawals = []
    wins = []

    for day, games in zip(days, daily_games):
        key = day.strftime("%d/%m")
        row = rows.get(key)
        labels.append(key)
        deposits.append(float(row.deposits) if row else 0.0)
        withdrawals.append(float(row.withdrawals) if row else 0.0)
        wins.append(games["wins_paid"])
    
    # Répartition des jeux
    keno_result = await db.execute(
        select(func.count(KenoBet.id))
        .where(KenoBet.placed_at >= start_date)
    )
    keno_count = keno_result.scalar() or 0
    
    games_result = await db.execute(
        select(GameBet.game_type, func.count(GameBet.id))
        .where(GameBet.placed_at >= start_date)
        .group_by(GameBet.game_type)
    )
    by_game = {game: count for game, count in games_result.all()}
    lucky6_count = by_game.get("lucky6", 0)
    horse_count = by_game.get("horse_races", 0)

    total = keno_count + lucky6_count + horse_count
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
            "lucky6": round(lucky6_count / total * 100),
            "horse_races": round(horse_count / total * 100),
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
        ).where(KenoBet.status.in_([KenoBetStatus.WON, KenoBetStatus.LOST]))  # paris réglés seulement
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


async def _send_welcome_sms(phone: str, name: str):
    """Envoie un SMS de bienvenue"""
    # À implémenter avec Twilio ou autre
    logger.info(f"SMS de bienvenue à {phone}: Bienvenue {name} sur Parier Keno Haïti!")