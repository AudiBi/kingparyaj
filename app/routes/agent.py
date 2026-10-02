# app/routes/agent.py
"""Routes pour les agents de bureau - panel HTML complet + API Lucky live"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_
import redis.asyncio as redis

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import (
    get_current_agent,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.core.csrf import register_csrf_globals
from app.core.timezone import (
    now_utc, now_haiti, today_haiti, today_bounds_utc, to_haiti,
    local_date_start_utc, local_date_end_utc, local_day,
)
from app.core.exceptions import AppException, NotFoundException, ValidationException, InsufficientBalanceException
from app.config import settings
from app.models.user import User
from app.models.enums import UserRole, TicketStatus, TransactionType, KenoDrawStatus, KenoBetStatus, AuditAction
from app.models.lucky import LuckyPlay, LuckyWheelConfig
from app.models.bureau import Bureau, CashierSession
from app.models.ticket import Ticket
from app.models.transaction import Transaction
from app.models.keno import KenoDraw, KenoBet
from app.schemas.wallet import DepositRequest, WithdrawRequest
from app.services.wallet_service import WalletService
from app.services.ticket_service import TicketService
from app.services.user_service import UserService
from app.services.audit_service import AuditService
from app.api.websockets.manager import manager, broadcast_lucky_result

router = APIRouter(prefix="/agent", tags=["Agent"])

# Racine = app/templates (pas app/templates/agent) : les templates agent
# utilisent {% extends "agent/base.html" %} (préfixe inclus), comme les
# templates admin - cf. la même correction faite sur app/routes/admin.py.
templates = Jinja2Templates(directory="app/templates")
register_csrf_globals(templates)


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


templates.env.filters["format_number"] = format_number
templates.env.filters["timeago"] = timeago
templates.env.filters["local"] = local_dt
# Markup sûr : sans cela l'auto-échappement casse `const x = {{ y|tojson }}` (&#34;)
from jinja2.utils import htmlsafe_json_dumps  # noqa: E402
templates.env.filters["tojson"] = lambda v, indent=None: htmlsafe_json_dumps(
    v, dumps=lambda o, **kw: json.dumps(o, indent=indent, default=str, **kw)
)


# ==================== HELPERS ====================

async def _get_open_session(db: AsyncSession, agent_id: str) -> Optional[CashierSession]:
    result = await db.execute(
        select(CashierSession).where(
            and_(CashierSession.agent_id == agent_id, CashierSession.status == "OPEN")
        )
    )
    return result.scalar_one_or_none()


def _agent_view(agent: User, bureau: Optional[Bureau]) -> dict:
    return {
        "id": agent.id,
        "full_name": agent.full_name,
        "phone": agent.phone,
        "email": agent.email,
        "role": agent.role.value if hasattr(agent.role, "value") else agent.role,
        "bureau_name": bureau.name if bureau else "Sans bureau",
        "bureau_address": (bureau.address or "") if bureau else "",
        "bureau_phone": (bureau.phone or "") if bureau else "",
        # Pas de champ "code agent" dédié dans le modèle User : le téléphone
        # (identifiant réel de connexion) fait office d'identifiant affiché.
        "code": agent.phone,
        "created_at": agent.created_at,
        "last_login": agent.last_login,
    }


async def _base_context(db: AsyncSession, agent: User, active: str) -> dict:
    bureau = await db.get(Bureau, agent.bureau_id) if agent.bureau_id else None
    session = await _get_open_session(db, agent.id)

    active_tickets_count = 0
    if agent.bureau_id:
        count_result = await db.execute(
            select(func.count(Ticket.id)).where(
                and_(
                    Ticket.bureau_id == agent.bureau_id,
                    Ticket.status == TicketStatus.ACTIVE,
                    Ticket.expires_at > now_utc(),
                )
            )
        )
        active_tickets_count = count_result.scalar() or 0

    return {
        "agent": _agent_view(agent, bureau),
        "active": active,
        "session_open": session,
        "session_cash_balance": float(session.current_balance) if session else 0,
        "active_tickets_count": active_tickets_count,
    }


def _pct_delta(today, yesterday) -> int:
    if yesterday:
        return round(float((today - yesterday) / yesterday) * 100)
    return 100 if today else 0


# ==================== AUTHENTIFICATION ====================
# Le jeton CSRF est géré pour tout /agent par AdminCsrfMiddleware
# (app/core/csrf.py, étendue à ce préfixe), exposé aux templates via le
# global Jinja `{{ csrf_token() }}` (register_csrf_globals ci-dessus).

@router.get("/login", response_class=HTMLResponse)
async def agent_login_page(
    request: Request,
    error: Optional[str] = None,
    csrf_error: Optional[str] = None,
):
    """Page de connexion agent"""
    if not error and csrf_error:
        error = "Session expirée, veuillez réessayer"

    return templates.TemplateResponse(request, "agent/login.html", {
        "error": error,
        "success": None,
    })


@router.post("/login")
async def agent_login(
    request: Request,
    code: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Traitement de la connexion agent.

    Le formulaire agent/login.html utilise un champ `code` plutôt que
    `phone` : le modèle User n'a pas de champ "code agent" dédié, le
    téléphone (identifiant réel de connexion) est donc utilisé pour cette
    valeur, comme partout ailleurs dans le panel agent (cf. _agent_view).
    """
    result = await db.execute(
        select(User).where(User.phone == code, User.is_deleted == False)
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        return await agent_login_page(request, error="Code ou mot de passe incorrect")

    if user.role not in [UserRole.AGENT, UserRole.MANAGER]:
        return await agent_login_page(request, error="Accès réservé aux agents")

    if not user.is_active:
        return await agent_login_page(request, error="Compte désactivé")

    access_token = create_access_token({"sub": user.id, "role": user.role})
    refresh_token = create_refresh_token({"sub": user.id})
    expire = 3600 * 24  # 24h

    await redis_client.setex(f"agent:refresh:{user.id}", expire, refresh_token)

    user.last_login = now_utc()
    user.last_ip = request.client.host if request.client else None
    await db.commit()

    audit_service = AuditService(db, redis_client)
    await audit_service.log(
        user_id=user.id,
        action=AuditAction.LOGIN,
        resource_type="agent",
        resource_id=user.id,
        ip_address=request.client.host if request.client else "0.0.0.0",
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    response = RedirectResponse(url="/agent/dashboard", status_code=303)
    response.set_cookie(
        key="agent_token", value=access_token, httponly=True,
        secure=not settings.DEBUG, samesite="lax", max_age=expire,
    )
    response.set_cookie(
        key="agent_refresh", value=refresh_token, httponly=True,
        secure=not settings.DEBUG, samesite="lax", max_age=expire,
    )
    return response


@router.post("/logout")
async def agent_logout(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Déconnexion agent"""
    token = request.cookies.get("agent_token")
    if token:
        payload = decode_token(token)
        if payload and payload.get("exp"):
            ttl = payload["exp"] - datetime.now(timezone.utc).timestamp()
            if ttl > 0:
                await redis_client.setex(f"blacklist:{token}", int(ttl), "1")

        user_id = payload.get("sub") if payload else None
        if user_id:
            await redis_client.delete(f"agent:refresh:{user_id}")

    response = RedirectResponse(url="/agent/login", status_code=303)
    response.delete_cookie("agent_token")
    response.delete_cookie("agent_refresh")
    return response


# ==================== DASHBOARD ====================

@router.get("/dashboard", response_class=HTMLResponse)
async def agent_dashboard(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    base = await _base_context(db, current_agent, "dashboard")
    session = base["session_open"]

    today = today_haiti()
    today_start = local_date_start_utc(today)
    tomorrow_start = local_date_end_utc(today)
    yesterday_start = local_date_start_utc(today - timedelta(days=1))

    async def _count_bets(start, end) -> int:
        b = await db.execute(
            select(func.count(KenoBet.id)).where(
                KenoBet.agent_id == current_agent.id, KenoBet.placed_at >= start, KenoBet.placed_at < end
            )
        )
        p = await db.execute(
            select(func.count(LuckyPlay.id)).where(
                LuckyPlay.agent_id == current_agent.id, LuckyPlay.played_at >= start, LuckyPlay.played_at < end
            )
        )
        return (b.scalar() or 0) + (p.scalar() or 0)

    async def _cash_in(start, end) -> Decimal:
        t = await db.execute(
            select(func.coalesce(func.sum(Ticket.initial_amount), 0)).where(
                Ticket.agent_id == current_agent.id, Ticket.created_at >= start, Ticket.created_at < end
            )
        )
        tx = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.created_by == current_agent.id,
                Transaction.transaction_type == TransactionType.DEPOSIT,
                Transaction.created_at >= start,
                Transaction.created_at < end,
            )
        )
        return Decimal(str(t.scalar() or 0)) + Decimal(str(tx.scalar() or 0))

    async def _cash_out(start, end) -> Decimal:
        t = await db.execute(
            select(func.coalesce(func.sum(Ticket.initial_amount), 0)).where(
                Ticket.paid_by_agent == current_agent.id, Ticket.paid_at >= start, Ticket.paid_at < end
            )
        )
        tx = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.created_by == current_agent.id,
                Transaction.transaction_type == TransactionType.WITHDRAWAL,
                Transaction.created_at >= start,
                Transaction.created_at < end,
            )
        )
        return Decimal(str(t.scalar() or 0)) + Decimal(str(tx.scalar() or 0))

    async def _tickets_count(start, end) -> int:
        r = await db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.agent_id == current_agent.id, Ticket.created_at >= start, Ticket.created_at < end
            )
        )
        return r.scalar() or 0

    today_bets = await _count_bets(today_start, tomorrow_start)
    yesterday_bets = await _count_bets(yesterday_start, today_start)
    today_cash_in = await _cash_in(today_start, tomorrow_start)
    yesterday_cash_in = await _cash_in(yesterday_start, today_start)
    today_cash_out = await _cash_out(today_start, tomorrow_start)
    yesterday_cash_out = await _cash_out(yesterday_start, today_start)
    today_tickets = await _tickets_count(today_start, tomorrow_start)
    yesterday_tickets = await _tickets_count(yesterday_start, today_start)

    recent_transactions = []
    recent_tickets = await db.execute(
        select(Ticket).where(Ticket.agent_id == current_agent.id).order_by(Ticket.created_at.desc()).limit(5)
    )
    for t in recent_tickets.scalars().all():
        recent_transactions.append({"type": "deposit", "amount": float(t.initial_amount), "player": t.player_name, "time": t.created_at})

    recent_paid = await db.execute(
        select(Ticket).where(Ticket.paid_by_agent == current_agent.id).order_by(Ticket.paid_at.desc()).limit(5)
    )
    for t in recent_paid.scalars().all():
        recent_transactions.append({"type": "payout", "amount": float(t.initial_amount), "player": t.player_name, "time": t.paid_at})

    recent_transactions.sort(key=lambda x: x["time"] or datetime.min, reverse=True)
    recent_transactions = recent_transactions[:8]

    active_tickets = []
    if current_agent.bureau_id:
        active_result = await db.execute(
            select(Ticket)
            .where(
                Ticket.bureau_id == current_agent.bureau_id,
                Ticket.status == TicketStatus.ACTIVE,
                Ticket.expires_at > now_utc(),
            )
            .order_by(Ticket.expires_at.asc())
            .limit(5)
        )
        for t in active_result.scalars().all():
            active_tickets.append({"number": t.ticket_number, "balance": float(t.balance), "expires_at": t.expires_at})

    next_draw_result = await db.execute(
        select(KenoDraw)
        .where(KenoDraw.status == KenoDrawStatus.PENDING, KenoDraw.mode == "scheduled", KenoDraw.draw_time > now_utc())
        .order_by(KenoDraw.draw_time.asc())
        .limit(1)
    )
    next_draw = next_draw_result.scalar_one_or_none()

    stats = {
        "today_bets": today_bets,
        "today_bets_delta": _pct_delta(today_bets, yesterday_bets),
        "today_cash_in": float(today_cash_in),
        "today_cash_in_delta": _pct_delta(today_cash_in, yesterday_cash_in),
        "today_cash_out": float(today_cash_out),
        "today_cash_out_delta": _pct_delta(today_cash_out, yesterday_cash_out),
        "today_tickets": today_tickets,
        "today_tickets_delta": _pct_delta(today_tickets, yesterday_tickets),
        "cash_balance": float(session.current_balance) if session else 0.0,
        "commission": 0,
    }

    return templates.TemplateResponse(request, "agent/dashboard.html", {
        **base,
        "stats": stats,
        "recent_transactions": recent_transactions,
        "active_tickets": active_tickets,
        "next_draw": next_draw,
    })


# ==================== CAISSE ====================

@router.get("/cashier", response_class=HTMLResponse)
async def agent_cashier(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    base = await _base_context(db, current_agent, "cashier")
    session = base["session_open"]

    operations = []
    if session:
        tickets_result = await db.execute(
            select(Ticket)
            .where(Ticket.agent_id == current_agent.id, Ticket.created_at >= session.opened_at)
            .order_by(Ticket.created_at.desc())
            .limit(10)
        )
        for t in tickets_result.scalars().all():
            operations.append({"type": "deposit", "amount": float(t.initial_amount), "player": t.player_name or "Ticket", "method": "cash", "time": t.created_at})

        paid_result = await db.execute(
            select(Ticket)
            .where(Ticket.paid_by_agent == current_agent.id, Ticket.paid_at >= session.opened_at)
            .order_by(Ticket.paid_at.desc())
            .limit(10)
        )
        for t in paid_result.scalars().all():
            operations.append({"type": "payout", "amount": float(t.initial_amount), "player": t.player_name or "Ticket", "method": "cash", "time": t.paid_at})

        tx_result = await db.execute(
            select(Transaction)
            .where(
                Transaction.created_by == current_agent.id,
                Transaction.payment_method == "cash",
                Transaction.created_at >= session.opened_at,
            )
            .order_by(Transaction.created_at.desc())
            .limit(10)
        )
        for tx in tx_result.scalars().all():
            user = await db.get(User, tx.user_id)
            op_type = "deposit" if tx.transaction_type == TransactionType.DEPOSIT else "payout"
            operations.append({"type": op_type, "amount": float(tx.amount), "player": user.full_name if user else None, "method": "cash", "time": tx.created_at})

        operations.sort(key=lambda o: o["time"] or datetime.min, reverse=True)
        operations = operations[:15]

    return templates.TemplateResponse(request, "agent/casier.html", {
        **base,
        "session": session,
        "operations": operations,
    })


@router.post("/api/cashier/open")
async def agent_open_session(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    if not current_agent.bureau_id:
        raise ValidationException("Agent non affecté à un bureau")

    if await _get_open_session(db, current_agent.id):
        raise ValidationException("Une session de caisse est déjà ouverte")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    try:
        starting_balance = Decimal(str(payload.get("starting_balance", 0)))
    except InvalidOperation:
        raise ValidationException("Solde initial invalide")
    if starting_balance < 0:
        raise ValidationException("Solde initial invalide")

    session = CashierSession(
        bureau_id=current_agent.bureau_id,
        agent_id=current_agent.id,
        starting_balance=starting_balance,
        current_balance=starting_balance,
        expected_balance=starting_balance,
        opened_at=now_utc(),
    )
    db.add(session)
    await db.commit()

    return {"success": True, "message": f"Session ouverte - solde initial {starting_balance} HTG", "session_id": session.id}


@router.post("/api/cashier/close")
async def agent_close_session(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_open_session(db, current_agent.id)
    if not session:
        raise ValidationException("Aucune session de caisse ouverte")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    expected = session.calculate_expected_balance()
    actual_raw = payload.get("actual_balance")
    try:
        actual_balance = Decimal(str(actual_raw)) if actual_raw is not None else expected
    except InvalidOperation:
        raise ValidationException("Solde réel invalide")

    session.close(actual_balance, reason=payload.get("difference_reason"))
    await db.commit()

    return {
        "success": True,
        "message": f"Session fermée. Écart: {session.difference} HTG",
        "difference": float(session.difference),
    }


@router.get("/api/cash-balance")
async def agent_get_cash_balance(
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_open_session(db, current_agent.id)
    return {"balance": float(session.current_balance) if session else 0.0, "session_open": session is not None}


@router.post("/api/cashier/deposit")
async def agent_cashier_deposit(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Encaissement cash au bureau : crée un ticket (joueur sans compte) ou
    crédite le compte d'un joueur existant (identifié par téléphone)."""
    session = await _get_open_session(db, current_agent.id)
    if not session:
        raise ValidationException("Aucune session de caisse ouverte")
    if not current_agent.bureau_id:
        raise ValidationException("Agent non affecté à un bureau")

    try:
        payload = await request.json()
    except Exception:
        raise ValidationException("Requête invalide")

    player_type = payload.get("player_type")
    try:
        amount = Decimal(str(payload.get("amount")))
    except (InvalidOperation, TypeError):
        raise ValidationException("Montant invalide")
    if amount <= 0:
        raise ValidationException("Montant invalide")

    bureau = await db.get(Bureau, current_agent.bureau_id)

    if player_type == "ticket":
        ticket_service = TicketService(db, redis_client)
        ticket = await ticket_service.create_ticket(
            agent_id=current_agent.id,
            bureau_id=current_agent.bureau_id,
            amount=amount,
            player_name=payload.get("player_name") or None,
            player_phone=payload.get("phone") or None,
        )
        session.cash_in_count += 1
        session.cash_in_amount += amount
        session.current_balance += amount
        await db.commit()

        return {"success": True, "message": f"Ticket {ticket['ticket_number']} créé - {amount} HTG", "data": ticket}

    elif player_type == "account":
        phone = payload.get("phone")
        if not phone:
            raise ValidationException("Numéro de téléphone requis pour un dépôt sur compte")

        user_service = UserService(db, redis_client)
        user = await user_service.get_by_phone(phone)
        if not user:
            raise NotFoundException("Joueur", phone)

        try:
            deposit_request = DepositRequest(amount=float(amount), payment_method="cash")
        except ValidationError as exc:
            raise ValidationException(exc.errors()[0]["msg"])

        wallet_service = WalletService(db, redis_client)
        result = await wallet_service.deposit(
            user_id=user.id,
            request=deposit_request,
            ip_address=request.client.host if request.client else "0.0.0.0",
        )
        tx = await db.get(Transaction, result["transaction_id"])
        tx.created_by = current_agent.id

        session.cash_in_count += 1
        session.cash_in_amount += amount
        session.current_balance += amount
        if bureau:
            bureau.cash_balance += amount
            bureau.total_cash_in_today += amount

        await db.commit()
        return {"success": True, "message": f"Dépôt de {amount} HTG effectué pour {user.full_name}", "data": result}

    raise ValidationException("Type de joueur invalide")


@router.post("/api/cashier/payout")
async def agent_payout(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Paiement cash au bureau : ticket (total ou partiel) ou compte joueur."""
    session = await _get_open_session(db, current_agent.id)
    if not session:
        raise ValidationException("Aucune session de caisse ouverte")

    try:
        payload = await request.json()
    except Exception:
        raise ValidationException("Requête invalide")

    payout_type = payload.get("payout_type")
    identifier = payload.get("identifier")
    raw_amount = payload.get("amount")
    if not identifier:
        raise ValidationException("Identifiant requis")

    if payout_type == "ticket":
        ticket_service = TicketService(db, redis_client)
        ticket = await ticket_service.get_by_number(identifier)
        if not ticket:
            raise NotFoundException("Ticket", identifier)
        if ticket.status != TicketStatus.ACTIVE:
            raise ValidationException(f"Ticket déjà {ticket.status.value}")
        if ticket.expires_at < now_utc():
            ticket.status = TicketStatus.EXPIRED
            await db.commit()
            raise ValidationException("Ticket expiré")

        partial_amount = None
        if raw_amount is not None:
            try:
                partial_amount = Decimal(str(raw_amount))
            except InvalidOperation:
                raise ValidationException("Montant invalide")
            if partial_amount <= 0:
                raise ValidationException("Montant invalide")
            if partial_amount > ticket.balance:
                raise ValidationException("Montant supérieur au solde du ticket")

        if partial_amount is not None and partial_amount < ticket.balance:
            # Paiement partiel : le ticket reste actif avec le solde restant.
            ticket.balance -= partial_amount
            paid_amount = partial_amount
            bureau = await db.get(Bureau, ticket.bureau_id)
            if bureau:
                bureau.cash_balance -= paid_amount
                bureau.total_cash_out_today += paid_amount

            audit_service = AuditService(db, redis_client)
            await audit_service.log(
                agent_id=current_agent.id,
                action=AuditAction.WITHDRAWAL,
                resource_type="ticket",
                resource_id=ticket.id,
                new_values={"ticket_number": ticket.ticket_number, "amount": float(paid_amount), "partial": True},
                ip_address=request.client.host if request.client else "0.0.0.0",
            )
        else:
            result = await ticket_service.payout_ticket(identifier, agent_id=current_agent.id, bureau_id=ticket.bureau_id)
            paid_amount = Decimal(str(result["amount"]))

        session.cash_out_count += 1
        session.cash_out_amount += paid_amount
        session.current_balance -= paid_amount
        await db.commit()

        return {"success": True, "message": f"Paiement de {paid_amount} HTG effectué", "data": {"amount": float(paid_amount)}}

    elif payout_type == "account":
        user_service = UserService(db, redis_client)
        user = await user_service.get_by_phone(identifier)
        if not user:
            raise NotFoundException("Joueur", identifier)

        wallet_service = WalletService(db, redis_client)
        balance = await wallet_service.get_balance(user.id)

        if raw_amount is not None:
            try:
                amount = Decimal(str(raw_amount))
            except InvalidOperation:
                raise ValidationException("Montant invalide")
        else:
            amount = balance

        if amount <= 0:
            raise ValidationException("Montant invalide")
        if amount > balance:
            raise InsufficientBalanceException(float(amount), float(balance))

        try:
            withdraw_request = WithdrawRequest(amount=float(amount), payment_method="cash")
        except ValidationError as exc:
            raise ValidationException(exc.errors()[0]["msg"])

        result = await wallet_service.withdraw(
            user_id=user.id,
            request=withdraw_request,
            ip_address=request.client.host if request.client else "0.0.0.0",
        )
        tx = await db.get(Transaction, result["transaction_id"])
        tx.created_by = current_agent.id

        bureau = await db.get(Bureau, current_agent.bureau_id) if current_agent.bureau_id else None
        session.cash_out_count += 1
        session.cash_out_amount += amount
        session.current_balance -= amount
        if bureau:
            bureau.cash_balance -= amount
            bureau.total_cash_out_today += amount

        await db.commit()
        return {"success": True, "message": f"Paiement de {amount} HTG effectué pour {user.full_name}", "data": result}

    raise ValidationException("Type de paiement invalide")


# ==================== TICKETS ====================

@router.get("/tickets", response_class=HTMLResponse)
async def agent_tickets(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    base = await _base_context(db, current_agent, "tickets")

    tickets = []
    if current_agent.bureau_id:
        result = await db.execute(
            select(Ticket)
            .where(Ticket.bureau_id == current_agent.bureau_id, Ticket.status == TicketStatus.ACTIVE)
            .order_by(Ticket.created_at.desc())
            .limit(100)
        )
        for t in result.scalars().all():
            tickets.append({
                "id": t.id,
                "number": t.ticket_number,
                "player_name": t.player_name,
                "initial_amount": float(t.initial_amount),
                "balance": float(t.balance),
                "expires_at": t.expires_at,
                "status": t.status.value,
            })

    today_start = today_bounds_utc()[0]
    stats = {"active": len(tickets), "total_balance": sum(t["balance"] for t in tickets), "today_created": 0, "today_paid": 0}
    if current_agent.bureau_id:
        created_today = await db.execute(
            select(func.count(Ticket.id)).where(Ticket.bureau_id == current_agent.bureau_id, Ticket.created_at >= today_start)
        )
        paid_today = await db.execute(
            select(func.count(Ticket.id)).where(
                Ticket.bureau_id == current_agent.bureau_id, Ticket.paid_at >= today_start, Ticket.status == TicketStatus.PAID
            )
        )
        stats["today_created"] = created_today.scalar() or 0
        stats["today_paid"] = paid_today.scalar() or 0

    return templates.TemplateResponse(request, "agent/tickets.html", {
        **base,
        "stats": stats,
        "tickets": tickets,
        "now": now_utc(),
    })


@router.post("/api/tickets")
async def agent_create_ticket(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    if not current_agent.bureau_id:
        raise ValidationException("Agent non affecté à un bureau")

    session = await _get_open_session(db, current_agent.id)
    if not session:
        raise ValidationException("Aucune session de caisse ouverte")

    try:
        payload = await request.json()
    except Exception:
        raise ValidationException("Requête invalide")

    try:
        amount = Decimal(str(payload.get("amount")))
    except (InvalidOperation, TypeError):
        raise ValidationException("Montant invalide")
    if amount <= 0:
        raise ValidationException("Montant invalide")

    ticket_service = TicketService(db, redis_client)
    ticket = await ticket_service.create_ticket(
        agent_id=current_agent.id,
        bureau_id=current_agent.bureau_id,
        amount=amount,
        player_name=payload.get("player_name") or None,
        player_phone=payload.get("player_phone") or None,
    )

    session.cash_in_count += 1
    session.cash_in_amount += amount
    session.current_balance += amount
    await db.commit()

    return {"success": True, "message": f"Ticket {ticket['ticket_number']} créé", "data": ticket}


@router.get("/api/tickets/{ticket_id}")
async def agent_get_ticket_detail(
    ticket_id: str,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    ticket = await db.get(Ticket, ticket_id)
    if not ticket:
        raise NotFoundException("Ticket", ticket_id)

    bets_result = await db.execute(
        select(KenoBet).where(KenoBet.ticket_id == ticket.id).order_by(KenoBet.placed_at.desc()).limit(20)
    )
    bets = [
        {"game": "Keno", "stake": float(b.stake), "winnings": float(b.winnings), "status": b.status.value}
        for b in bets_result.scalars().all()
    ]

    plays_result = await db.execute(
        select(LuckyPlay).where(LuckyPlay.ticket_id == ticket.id).order_by(LuckyPlay.played_at.desc()).limit(20)
    )
    for p in plays_result.scalars().all():
        bets.append({
            "game": "Lucky Wheel",
            "stake": float(p.stake),
            "winnings": float(p.winnings),
            "status": "won" if p.winnings > 0 else "lost",
        })

    return {
        "id": ticket.id,
        "number": ticket.ticket_number,
        "player_name": ticket.player_name,
        "initial_amount": float(ticket.initial_amount),
        "balance": float(ticket.balance),
        "status": ticket.status.value,
        "expires_at": ticket.expires_at.isoformat(),
        "bets": bets,
    }


@router.post("/api/tickets/{ticket_id}/payout")
async def agent_payout_ticket_by_id(
    ticket_id: str,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    session = await _get_open_session(db, current_agent.id)
    if not session:
        raise ValidationException("Aucune session de caisse ouverte")

    ticket = await db.get(Ticket, ticket_id)
    if not ticket:
        raise NotFoundException("Ticket", ticket_id)

    ticket_service = TicketService(db, redis_client)
    result = await ticket_service.payout_ticket(ticket.ticket_number, agent_id=current_agent.id, bureau_id=ticket.bureau_id)
    paid_amount = Decimal(str(result["amount"]))

    session.cash_out_count += 1
    session.cash_out_amount += paid_amount
    session.current_balance -= paid_amount
    await db.commit()

    return result


@router.get("/api/tickets/{ticket_number}/qr")
async def agent_ticket_qr(
    ticket_number: str,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    ticket_service = TicketService(db, redis_client)
    ticket = await ticket_service.get_by_number(ticket_number)
    if not ticket:
        raise NotFoundException("Ticket", ticket_number)

    qr_code = ticket_service._generate_qr_code(ticket.ticket_number)
    return {"success": True, "ticket_number": ticket.ticket_number, "qr_code": qr_code, "expires_at": ticket.expires_at.isoformat()}


# ==================== HISTORIQUE ====================

@router.get("/history", response_class=HTMLResponse)
async def agent_history(
    request: Request,
    page: int = 1,
    type: Optional[str] = None,
    date: Optional[str] = None,
    search: Optional[str] = None,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    base = await _base_context(db, current_agent, "history")

    items: List[dict] = []

    tickets_created = await db.execute(
        select(Ticket).where(Ticket.agent_id == current_agent.id).order_by(Ticket.created_at.desc()).limit(200)
    )
    for t in tickets_created.scalars().all():
        items.append({"date": t.created_at, "type": "deposit", "player": t.player_name, "amount": float(t.initial_amount), "method": "cash", "reference": t.ticket_number})

    tickets_paid = await db.execute(
        select(Ticket).where(Ticket.paid_by_agent == current_agent.id).order_by(Ticket.paid_at.desc()).limit(200)
    )
    for t in tickets_paid.scalars().all():
        items.append({"date": t.paid_at, "type": "payout", "player": t.player_name, "amount": float(t.initial_amount), "method": "cash", "reference": t.ticket_number})

    tx_result = await db.execute(
        select(Transaction).where(Transaction.created_by == current_agent.id).order_by(Transaction.created_at.desc()).limit(200)
    )
    for tx in tx_result.scalars().all():
        user = await db.get(User, tx.user_id)
        op_type = "deposit" if tx.transaction_type == TransactionType.DEPOSIT else "payout"
        items.append({
            "date": tx.created_at, "type": op_type, "player": user.full_name if user else None,
            "amount": float(tx.amount), "method": tx.payment_method.value if tx.payment_method else "cash", "reference": tx.reference,
        })

    bets_result = await db.execute(
        select(KenoBet).where(KenoBet.agent_id == current_agent.id).order_by(KenoBet.placed_at.desc()).limit(200)
    )
    for b in bets_result.scalars().all():
        player = None
        if b.user_id:
            u = await db.get(User, b.user_id)
            player = u.full_name if u else None
        elif b.ticket_id:
            t = await db.get(Ticket, b.ticket_id)
            player = t.player_name if t else "Ticket"
        items.append({"date": b.placed_at, "type": "bet", "player": player, "amount": float(b.stake), "method": "keno", "reference": b.id})
        if b.status == KenoBetStatus.WON and b.winnings:
            items.append({"date": b.settled_at or b.placed_at, "type": "win", "player": player, "amount": float(b.winnings), "method": "keno", "reference": b.id})

    plays_result = await db.execute(
        select(LuckyPlay).where(LuckyPlay.agent_id == current_agent.id).order_by(LuckyPlay.played_at.desc()).limit(200)
    )
    for p in plays_result.scalars().all():
        player = None
        if p.user_id:
            u = await db.get(User, p.user_id)
            player = u.full_name if u else None
        elif p.ticket_id:
            t = await db.get(Ticket, p.ticket_id)
            player = t.player_name if t else "Ticket"
        items.append({"date": p.played_at, "type": "bet", "player": player, "amount": float(p.stake), "method": "lucky", "reference": p.id})
        if p.winnings and p.winnings > 0:
            items.append({"date": p.played_at, "type": "win", "player": player, "amount": float(p.winnings), "method": "lucky", "reference": p.id})

    if type:
        items = [i for i in items if i["type"] == type]
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
            items = [i for i in items if i["date"] and to_haiti(i["date"]).date() == target_date]
        except ValueError:
            pass
    if search:
        s = search.lower()
        items = [i for i in items if (i["player"] and s in i["player"].lower()) or (i["reference"] and s in str(i["reference"]).lower())]

    items.sort(key=lambda i: i["date"] or datetime.min, reverse=True)

    total_deposits = sum(i["amount"] for i in items if i["type"] == "deposit")
    total_payouts = sum(i["amount"] for i in items if i["type"] == "payout")

    page_size = 20
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    page_items = items[(page - 1) * page_size: page * page_size]

    return templates.TemplateResponse(request, "agent/history.html", {
        **base,
        "stats": {"total": total, "total_deposits": total_deposits, "total_payouts": total_payouts, "net": total_deposits - total_payouts},
        "items": page_items,
        "pagination": {"total": total, "pages": pages, "page": page, "has_prev": page > 1, "has_next": page < pages},
    })


# ==================== RAPPORTS ====================

@router.get("/reports", response_class=HTMLResponse)
async def agent_reports(
    request: Request,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    base = await _base_context(db, current_agent, "reports")

    today = today_haiti()
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else today - timedelta(days=7)
    except ValueError:
        start = today - timedelta(days=7)
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else today
    except ValueError:
        end = today
    if end < start:
        start, end = end, start

    range_start = local_date_start_utc(start)
    range_end = local_date_end_utc(end)  # exclusive

    tickets_created_result = await db.execute(
        select(Ticket).where(Ticket.agent_id == current_agent.id, Ticket.created_at >= range_start, Ticket.created_at < range_end)
    )
    tickets_created = tickets_created_result.scalars().all()

    tickets_paid_result = await db.execute(
        select(Ticket).where(Ticket.paid_by_agent == current_agent.id, Ticket.paid_at >= range_start, Ticket.paid_at < range_end)
    )
    tickets_paid = tickets_paid_result.scalars().all()

    tx_result = await db.execute(
        select(Transaction).where(Transaction.created_by == current_agent.id, Transaction.created_at >= range_start, Transaction.created_at < range_end)
    )
    transactions = tx_result.scalars().all()

    bets_result = await db.execute(
        select(KenoBet).where(KenoBet.agent_id == current_agent.id, KenoBet.placed_at >= range_start, KenoBet.placed_at < range_end)
    )
    bets = bets_result.scalars().all()

    plays_result = await db.execute(
        select(LuckyPlay).where(LuckyPlay.agent_id == current_agent.id, LuckyPlay.played_at >= range_start, LuckyPlay.played_at < range_end)
    )
    plays = plays_result.scalars().all()

    total_deposits = sum(float(t.initial_amount) for t in tickets_created)
    total_deposits += sum(float(tx.amount) for tx in transactions if tx.transaction_type == TransactionType.DEPOSIT)
    total_payouts = sum(float(t.initial_amount) for t in tickets_paid)
    total_payouts += sum(float(tx.amount) for tx in transactions if tx.transaction_type == TransactionType.WITHDRAWAL)

    daily, labels, dep_series, pay_series = [], [], [], []
    cursor = start
    while cursor <= end:
        day_start = local_date_start_utc(cursor)
        day_end = local_date_end_utc(cursor)  # exclusive

        day_deposits = sum(float(t.initial_amount) for t in tickets_created if day_start <= t.created_at < day_end)
        day_deposits += sum(float(tx.amount) for tx in transactions if tx.transaction_type == TransactionType.DEPOSIT and day_start <= tx.created_at < day_end)

        day_payouts = sum(float(t.initial_amount) for t in tickets_paid if t.paid_at and day_start <= t.paid_at < day_end)
        day_payouts += sum(float(tx.amount) for tx in transactions if tx.transaction_type == TransactionType.WITHDRAWAL and day_start <= tx.created_at < day_end)

        day_bets = sum(1 for b in bets if day_start <= b.placed_at < day_end) + sum(1 for p in plays if day_start <= p.played_at < day_end)
        day_tickets = sum(1 for t in tickets_created if day_start <= t.created_at < day_end)

        label = cursor.strftime("%d/%m")
        daily.append({"date": label, "deposits": day_deposits, "payouts": day_payouts, "bets": day_bets, "tickets": day_tickets, "net": day_deposits - day_payouts})
        labels.append(label)
        dep_series.append(day_deposits)
        pay_series.append(day_payouts)
        cursor += timedelta(days=1)

    summary = {
        "total_deposits": total_deposits,
        "total_payouts": total_payouts,
        "total_bets": len(bets) + len(plays),
        "total_tickets": len(tickets_created),
        "commission": 0,
        "net": total_deposits - total_payouts,
    }

    return templates.TemplateResponse(request, "agent/reports.html", {
        **base,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "summary": summary,
        "daily": daily,
        "chart_data": {"labels": labels, "deposits": dep_series, "payouts": pay_series},
    })


# ==================== PROFIL ====================

@router.get("/profile", response_class=HTMLResponse)
async def agent_profile(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    base = await _base_context(db, current_agent, "profile")

    total_tickets_result = await db.execute(select(func.count(Ticket.id)).where(Ticket.agent_id == current_agent.id))
    total_tickets = total_tickets_result.scalar() or 0

    bets_result = await db.execute(select(func.count(KenoBet.id)).where(KenoBet.agent_id == current_agent.id))
    plays_result = await db.execute(select(func.count(LuckyPlay.id)).where(LuckyPlay.agent_id == current_agent.id))
    total_bets = (bets_result.scalar() or 0) + (plays_result.scalar() or 0)

    tickets_sum = await db.execute(select(func.coalesce(func.sum(Ticket.initial_amount), 0)).where(Ticket.agent_id == current_agent.id))
    total_deposits = float(tickets_sum.scalar() or 0)
    tx_deposits_sum = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.created_by == current_agent.id, Transaction.transaction_type == TransactionType.DEPOSIT
        )
    )
    total_deposits += float(tx_deposits_sum.scalar() or 0)

    tickets_paid_sum = await db.execute(select(func.coalesce(func.sum(Ticket.initial_amount), 0)).where(Ticket.paid_by_agent == current_agent.id))
    total_payouts = float(tickets_paid_sum.scalar() or 0)
    tx_withdraw_sum = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.created_by == current_agent.id, Transaction.transaction_type == TransactionType.WITHDRAWAL
        )
    )
    total_payouts += float(tx_withdraw_sum.scalar() or 0)

    active_days_result = await db.execute(
        select(func.count(func.distinct(local_day(Ticket.created_at)))).where(Ticket.agent_id == current_agent.id)
    )
    active_days = active_days_result.scalar() or 0

    return templates.TemplateResponse(request, "agent/profile.html", {
        **base,
        "stats": {
            "total_bets": total_bets,
            "total_tickets": total_tickets,
            "total_deposits": total_deposits,
            "total_payouts": total_payouts,
            "commission": 0,
            "active_days": active_days,
        },
    })


@router.post("/api/profile/change-password")
async def agent_change_password(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    try:
        payload = await request.json()
    except Exception:
        raise ValidationException("Requête invalide")

    current_password = payload.get("current_password")
    new_password = payload.get("new_password")
    if not current_password or not new_password:
        raise ValidationException("Mot de passe actuel et nouveau requis")
    if len(new_password) < 6:
        raise ValidationException("Le nouveau mot de passe doit contenir au moins 6 caractères")
    if not verify_password(current_password, current_agent.password_hash):
        raise ValidationException("Mot de passe actuel incorrect")

    user_service = UserService(db, redis_client)
    await user_service.update_password(current_agent.id, new_password, updater_id=current_agent.id)
    await db.commit()

    return {"success": True, "message": "Mot de passe modifié avec succès"}


# ==================== KENO ====================

@router.get("/keno", response_class=HTMLResponse)
async def agent_keno(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Keno au bureau : tirages PARTAGÉS (par défaut) ou instantanés (option admin)."""
    from app.services import keno_engine
    from app.services.keno_service import MODE_INSTANT, KenoService

    base = await _base_context(db, current_agent, "keno")
    service = KenoService(db, redis_client)
    config = await service.get_config()
    if config["mode"] != MODE_INSTANT:
        return templates.TemplateResponse(request, "agent/keno_shared.html", {
            **base,
            "keno_config": keno_engine.public_config(config),
            "screen_code": KenoService.shared_screen_code(current_agent.id),
        })
    draw = await service.prepare_instant_draw(current_agent.id) if config["enabled"] else None
    await db.commit()
    return templates.TemplateResponse(request, "agent/keno.html", {
        **base,
        "keno_config": keno_engine.public_config(config),
        "prepared": _prepared_payload(draw),
        "screen_code": KenoService.screen_code(current_agent.id),
    })


def _prepared_payload(draw) -> Optional[Dict[str, Any]]:
    if draw is None:
        return None
    return {"draw_id": draw.id, "draw_number": draw.draw_number, "server_seed_hash": draw.server_seed_hash}


@router.get("/api/keno/state")
async def agent_keno_state(
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Configuration en vigueur + tirage préparé (empreinte affichée avant le pari)."""
    from app.services import keno_engine
    from app.services.keno_service import KenoService

    from app.services.keno_service import MODE_INSTANT

    service = KenoService(db, redis_client)
    config = await service.get_config()
    instant = config["enabled"] and config["mode"] == MODE_INSTANT
    draw = await service.prepare_instant_draw(current_agent.id) if instant else None
    await db.commit()
    return {"config": keno_engine.public_config(config), "prepared": _prepared_payload(draw)}


@router.post("/api/keno/screen")
async def agent_keno_screen_preview(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Montre au joueur, sur son écran, le ticket en cours de saisie
    (numéros demandés, mise, gains possibles). Affichage seulement."""
    from app.services.keno_service import KenoService

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    service = KenoService(db, redis_client)
    prepared = await service.prepare_instant_draw(current_agent.id)
    await db.commit()
    state = await service.screen_preview(current_agent.id, payload.get("picks"), payload.get("stake"), prepared)
    return {"success": True, "seq": state["seq"]}


@router.get("/api/keno/quick-pick")
async def agent_keno_quick_pick(
    count: int = Query(5, ge=1, le=10),
    current_agent: User = Depends(get_current_agent),
):
    """Sélection aléatoire (générateur du serveur). Ne joue rien et n'a aucun
    effet sur le tirage, qui dépend uniquement du seed déjà engagé."""
    from app.services.rng_service import RNGService

    return {"picks": RNGService().generate_lucky_numbers(1, 80, count)}


@router.post("/api/keno/bet")
async def agent_place_keno_bet(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Ticket Keno instantané, pour un compte joueur ou un ticket bureau.

    Contrôles, débit, tirage, règlement et crédit : KenoService.play_instant
    (une seule transaction). Le client n'envoie que les numéros, la mise et
    le joueur : aucun résultat, multiplicateur ou gain n'est accepté."""
    from app.services.keno_service import KenoService

    try:
        payload = await request.json()
    except Exception:
        raise ValidationException("Requête invalide")
    if not isinstance(payload, dict):
        raise ValidationException("Requête invalide")

    player_type = payload.get("player_type")
    identifier = str(payload.get("identifier") or "").strip()
    draw_id = payload.get("draw_id")
    if not draw_id:
        raise ValidationException("Tirage indisponible : rechargez la page")

    service = KenoService(db, redis_client)
    ip_address = request.client.host if request.client else "0.0.0.0"
    kwargs = dict(draw_id=draw_id, picks=payload.get("picks"), stake=payload.get("stake"),
                  agent_id=current_agent.id, ip_address=ip_address)

    if player_type in (None, "", "cash"):
        # espèces : le numéro de ticket est créé automatiquement (même transaction)
        from app.services.cash_ticket import sell_cash_ticket

        identifier = await sell_cash_ticket(db, redis_client, current_agent, payload.get("stake"), payload.get("player_name"))
        player_type = "ticket"
    if player_type == "account":
        user = await UserService(db, redis_client).get_by_phone(identifier)
        if not user:
            raise NotFoundException("Joueur", identifier)
        result = await service.play_instant(user_id=user.id, **kwargs)
        player_name = user.full_name or user.phone
    elif player_type == "ticket":
        result = await service.play_instant(ticket_number=identifier, **kwargs)
        ticket = await db.get(Ticket, result["ticket_id"])
        player_name = (ticket.player_name if ticket else None) or "Ticket"
        result["ticket_number"] = ticket.ticket_number if ticket else identifier
        result["ticket_balance"] = float(ticket.balance) if ticket else None
    else:
        raise ValidationException("Type de joueur invalide")

    # Tirage suivant préparé tout de suite (son empreinte s'affiche pour le prochain ticket)
    next_draw = await service.prepare_instant_draw(current_agent.id)
    await db.commit()
    # Écran joueur : lancement du tirage et sortie des numéros (résultat déjà réglé)
    await service.screen_play(current_agent.id, result)

    return {
        "success": True,
        "message": f"Ticket Keno de {result['stake']:g} HTG joué pour {player_name}",
        "player_name": player_name,
        "agent_name": current_agent.full_name or current_agent.phone,
        **result,
        "next": _prepared_payload(next_draw),
    }


@router.get("/api/keno/bets/{bet_id}")
async def agent_keno_bet_detail(
    bet_id: str,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Réimpression d'un reçu : uniquement les tickets pris par cet agent."""
    from app.services.keno_service import KenoService

    bet = await db.get(KenoBet, bet_id)
    if bet is None or bet.agent_id != current_agent.id:
        raise NotFoundException("Pari", bet_id)
    draw = await db.get(KenoDraw, bet.draw_id)
    service = KenoService(db, redis_client)
    data = service.serialize_instant(draw, bet, await service.get_config())
    if bet.ticket_id:
        ticket = await db.get(Ticket, bet.ticket_id)
        data["ticket_number"] = ticket.ticket_number if ticket else None
    return data


@router.get("/api/keno/history")
async def agent_keno_history(
    limit: int = Query(15, ge=1, le=50),
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    """Derniers tickets Keno pris par cet agent."""
    from app.services.cash_ticket import ticket_numbers

    rows = (await db.execute(
        select(KenoBet, KenoDraw.draw_number)
        .join(KenoDraw, KenoDraw.id == KenoBet.draw_id)
        .where(KenoBet.agent_id == current_agent.id)
        .order_by(KenoBet.placed_at.desc())
        .limit(limit)
    )).all()
    numbers = await ticket_numbers(db, [bet.ticket_id for bet, _ in rows])
    return {"items": [
        {
            "bet_id": bet.id,
            "draw_number": number,
            "placed_at": bet.placed_at.isoformat() + "Z" if bet.placed_at else None,
            "picks": [int(n) for n in bet.picks],
            "stake": float(bet.stake),
            "match_count": int(bet.hits or 0),
            "payout": float(bet.winnings or 0),
            "status": bet.status.value if hasattr(bet.status, "value") else bet.status,
            "ticket_number": numbers.get(bet.ticket_id),
        }
        for bet, number in rows
    ]}


# ==================== KENO PARTAGÉ ====================
# Tirage commun à tous les bureaux toutes les N minutes : l'agent prend les
# tickets sur le tirage ouvert ; le worker tire et règle à l'heure.

@router.get("/api/keno/csrf-token")
async def agent_keno_csrf_token(request: Request, current_agent: User = Depends(get_current_agent)):
    """Jeton CSRF frais avant chaque envoi (la page reste ouverte longtemps)."""
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


@router.get("/api/keno/live")
async def agent_keno_live(
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    from app.services import keno_engine
    from app.services.keno_service import KenoService

    service = KenoService(db, redis_client)
    state = await service.live_state()
    state["config"] = keno_engine.public_config(await service.get_config())
    return state


@router.post("/api/keno/shared/bet")
async def agent_keno_shared_bet(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Ticket sur le tirage partagé ouvert, pour un compte joueur ou un ticket.
    Le navigateur n'envoie que le tirage, les numéros, la mise et le joueur."""
    from app.services.keno_service import KenoService

    try:
        payload = await request.json()
    except Exception:
        raise ValidationException("Requête invalide")
    if not isinstance(payload, dict) or not payload.get("draw_id"):
        raise ValidationException("Tirage manquant")
    allowed = {"draw_id", "picks", "stake", "player_type", "identifier", "player_name"}
    if set(payload) - allowed:
        raise ValidationException("Champ non autorisé : " + ", ".join(sorted(set(payload) - allowed)))

    from app.services.cash_ticket import resolve_funding

    service = KenoService(db, redis_client)
    user_id, ticket_number = await resolve_funding(
        db, redis_client, current_agent, payload.get("player_type"), str(payload.get("identifier") or ""),
        payload.get("stake"), payload.get("player_name"),
    )
    bet = await service.create_bet(
        draw_id=str(payload["draw_id"]), picks=payload.get("picks"), stake=payload.get("stake"),
        user_id=user_id, ticket_number=ticket_number,
        agent_id=current_agent.id, ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    try:
        await service.shared_screen_ticket(current_agent.id, bet)
    except Exception:
        pass  # l'affichage ne bloque jamais un ticket enregistré
    draw = await db.get(KenoDraw, bet.draw_id)
    data = service.serialize_shared_bet(bet, draw, await service.get_config())
    if bet.ticket_id:
        ticket = await db.get(Ticket, bet.ticket_id)
        data["ticket_number"] = ticket.ticket_number if ticket else None
        data["ticket_balance"] = float(ticket.balance) if ticket else None
    return {"success": True, "message": "Ticket Keno enregistré", "bet": data}


@router.post("/api/keno/shared/screen")
async def agent_keno_shared_screen(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Ticket en cours de saisie montré sur l'écran du joueur (affichage seulement)."""
    from app.services.keno_service import KenoService

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    state = await KenoService(db, redis_client).shared_screen_preview(
        current_agent.id, payload.get("draw_id"), payload.get("picks"), payload.get("stake"),
    )
    return {"success": True, "seq": state["seq"]}


@router.get("/api/keno/shared/my-bets")
async def agent_keno_shared_bets(
    limit: int = Query(20, ge=1, le=100),
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Derniers tickets de cet agent sur les tirages partagés."""
    from app.services.keno_service import MODE_SCHEDULED, KenoService

    rows = (await db.execute(
        select(KenoBet, KenoDraw)
        .join(KenoDraw, KenoDraw.id == KenoBet.draw_id)
        .where(KenoBet.agent_id == current_agent.id, KenoDraw.mode == MODE_SCHEDULED)
        .order_by(KenoBet.placed_at.desc())
        .limit(limit)
    )).all()
    from app.services.cash_ticket import ticket_numbers

    service = KenoService(db, redis_client)
    config = await service.get_config()
    numbers = await ticket_numbers(db, [bet.ticket_id for bet, _ in rows])
    return {"bets": [{**service.serialize_shared_bet(bet, draw, config), "ticket_number": numbers.get(bet.ticket_id)}
                     for bet, draw in rows]}


@router.get("/api/keno/shared/bets/{bet_id}")
async def agent_keno_shared_receipt(
    bet_id: str,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Réimpression : uniquement les tickets pris par cet agent."""
    from app.services.keno_service import KenoService

    bet = await db.get(KenoBet, bet_id)
    if bet is None or bet.agent_id != current_agent.id:
        raise NotFoundException("Pari", bet_id)
    service = KenoService(db, redis_client)
    data = service.serialize_shared_bet(bet, await db.get(KenoDraw, bet.draw_id), await service.get_config())
    if bet.ticket_id:
        ticket = await db.get(Ticket, bet.ticket_id)
        data["ticket_number"] = ticket.ticket_number if ticket else None
    return {"bet": data}


@router.get("/api/keno/shared/history")
async def agent_keno_shared_history(
    limit: int = Query(10, ge=1, le=50),
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    from app.services.keno_service import KenoService

    service = KenoService(db, redis_client)
    config = await service.get_config()
    return {"draws": [service.serialize_draw(d, config) for d in await service.shared_history(limit)]}


# ==================== LUCKY WHEEL ====================

@router.get("/lucky", response_class=HTMLResponse)
async def agent_lucky(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    base = await _base_context(db, current_agent, "lucky")

    config_result = await db.execute(
        select(LuckyWheelConfig).where(LuckyWheelConfig.is_active == True).order_by(LuckyWheelConfig.is_default.desc()).limit(1)
    )
    config = config_result.scalar_one_or_none()
    segments = config.segments if config else []

    today_start = today_bounds_utc()[0]
    plays_result = await db.execute(
        select(LuckyPlay).where(LuckyPlay.agent_id == current_agent.id, LuckyPlay.played_at >= today_start)
    )
    plays = plays_result.scalars().all()

    today_volume = sum((p.stake for p in plays), Decimal("0"))
    today_wins = sum((p.winnings for p in plays), Decimal("0"))
    best_multiplier = max((p.multiplier for p in plays), default=Decimal("0"))

    return templates.TemplateResponse(request, "agent/lucky.html", {
        **base,
        "stats": {
            "today_plays": len(plays),
            "today_volume": float(today_volume),
            "today_wins": float(today_wins),
            "best_multiplier": float(best_multiplier),
        },
        "segments": segments,
    })


# ==================== LUCKY LIVE RESULTS ====================

@router.get("/api/lucky/latest")
async def agent_lucky_latest(
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Récupère le dernier résultat Lucky"""

    result = await db.execute(
        select(LuckyPlay)
        .order_by(desc(LuckyPlay.played_at))
        .limit(1)
    )
    play = result.scalar_one_or_none()

    if not play:
        return {"has_result": False, "message": "Aucun résultat disponible"}

    # Récupérer la configuration de la roue
    config_result = await db.execute(
        select(LuckyWheelConfig).where(LuckyWheelConfig.id == play.wheel_config_id)
    )
    config = config_result.scalar_one_or_none()

    # Trouver le segment
    segment = None
    if config and config.segments:
        for seg in config.segments:
            if seg["label"] == play.result_segment.get("label"):
                segment = seg
                break

    return {
        "has_result": True,
        "play_id": play.id,
        "segment": play.result_segment.get("label"),
        "multiplier": float(play.multiplier),
        "winnings": float(play.winnings),
        "stake": float(play.stake),
        "color": segment.get("color") if segment else "#94a3b8",
        "player": play.user.full_name if play.user else "Ticket",
        "played_at": play.played_at.isoformat()
    }


@router.get("/api/lucky/history")
async def agent_lucky_history(
    limit: int = 20,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Récupère l'historique des résultats Lucky"""

    result = await db.execute(
        select(LuckyPlay)
        .order_by(desc(LuckyPlay.played_at))
        .limit(limit)
    )
    plays = result.scalars().all()

    history = []
    for play in plays:
        # Récupérer la config
        config_result = await db.execute(
            select(LuckyWheelConfig).where(LuckyWheelConfig.id == play.wheel_config_id)
        )
        config = config_result.scalar_one_or_none()

        segment = None
        if config and config.segments:
            for seg in config.segments:
                if seg["label"] == play.result_segment.get("label"):
                    segment = seg
                    break

        history.append({
            "play_id": play.id,
            "segment": play.result_segment.get("label"),
            "multiplier": float(play.multiplier),
            "winnings": float(play.winnings),
            "stake": float(play.stake),
            "color": segment.get("color") if segment else "#94a3b8",
            "player": play.user.full_name if play.user else "Ticket",
            "played_at": play.played_at.isoformat()
        })

    return history


@router.post("/api/lucky/spin")
async def agent_lucky_spin(
    request: Request,
    data: dict,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Effectue un tour de Lucky Wheel pour un joueur.

    Passe par WalletService/TicketService (au lieu de manipuler directement
    wallet.balance/ticket.balance) pour que chaque tour génère une vraie
    Transaction + AuditLog, comme tous les autres flux d'argent de cette
    session - l'ancienne implémentation ne laissait aucune trace comptable.
    """
    current_agent = current_user
    player_type = data.get("player_type")
    identifier = data.get("identifier")

    try:
        stake = Decimal(str(data.get("stake")))
    except (InvalidOperation, TypeError):
        raise ValidationException("Mise invalide")
    if stake < 10:
        raise ValidationException("Mise minimum: 10 HTG")

    session = await _get_open_session(db, current_agent.id)
    if not session:
        raise ValidationException("Aucune session de caisse ouverte")

    config_result = await db.execute(
        select(LuckyWheelConfig)
        .where(LuckyWheelConfig.is_active == True)
        .order_by(LuckyWheelConfig.is_default.desc())
        .limit(1)
    )
    config = config_result.scalar_one_or_none()
    if not config:
        raise AppException(500, "Configuration de la roue non trouvée")

    user = None
    ticket = None
    player_name = "Anonyme"

    if player_type == "account":
        user_service = UserService(db, redis_client)
        user = await user_service.get_by_phone(identifier)
        if not user:
            raise NotFoundException("Joueur", identifier)
        player_name = user.full_name or user.phone

        wallet_service = WalletService(db, redis_client)
        await wallet_service.debit(user_id=user.id, amount=stake, transaction_type="BET")

    elif player_type in (None, "", "cash", "ticket"):
        if player_type != "ticket":
            # espèces : le numéro de ticket est créé automatiquement (même transaction)
            from app.services.cash_ticket import sell_cash_ticket

            identifier = await sell_cash_ticket(db, redis_client, current_agent, stake, data.get("player_name"))
        ticket_service = TicketService(db, redis_client)
        ticket = await ticket_service.get_by_number((identifier or "").strip().upper())
        if not ticket:
            raise NotFoundException("Ticket", identifier)
        if ticket.status != TicketStatus.ACTIVE:
            raise ValidationException("Ticket inactif")
        if ticket.expires_at < now_utc():
            ticket.status = TicketStatus.EXPIRED
            await db.commit()
            raise ValidationException("Ticket expiré")
        if ticket.balance < stake:
            raise ValidationException("Solde du ticket insuffisant")
        ticket.balance -= stake
        player_name = ticket.player_name or "Ticket"

    else:
        raise ValidationException("Type de joueur invalide")

    winning_segment = LuckyPlay.spin_wheel(config.segments)
    multiplier = Decimal(str(winning_segment["multiplier"]))
    winnings = stake * multiplier

    random_seed = secrets.token_hex(32)
    verification_hash = hashlib.sha256(
        f"{random_seed}{stake}{now_utc().isoformat()}".encode()
    ).hexdigest()

    lucky_play = LuckyPlay(
        user_id=user.id if user else None,
        ticket_id=ticket.id if ticket else None,
        agent_id=current_agent.id,
        wheel_config_id=config.id,
        stake=stake,
        result_segment=winning_segment,
        multiplier=multiplier,
        winnings=winnings,
        random_seed=random_seed,
        verification_hash=verification_hash,
        played_at=now_utc(),
    )
    db.add(lucky_play)
    await db.flush()

    if winnings > 0:
        if user:
            wallet_service = WalletService(db, redis_client)
            await wallet_service.credit(user_id=user.id, amount=winnings, transaction_type="WIN")
        elif ticket:
            ticket.balance += winnings

    await db.commit()

    await broadcast_lucky_result({
        "type": "lucky_result",
        "data": {
            "segment": winning_segment["label"],
            "multiplier": float(multiplier),
            "winnings": float(winnings),
            "player": player_name,
            "played_at": now_utc().isoformat(),
            "stake": float(stake),
        }
    })

    return {
        "success": True,
        "segment": winning_segment["label"],
        "multiplier": float(multiplier),
        "winnings": float(winnings),
        "color": winning_segment["color"],
        "play_id": lucky_play.id,
        "player": player_name,
        "stake": float(stake),
        "ticket_number": ticket.ticket_number if ticket else None,
        "verification_hash": verification_hash,
        "played_at": lucky_play.played_at.isoformat() + "Z",
        "message": f"Tour terminé ! {('Gain: ' + str(winnings) + ' HTG') if winnings > 0 else 'Perdu'}",
    }
