# app/api/v1/agent.py
"""API complète pour les agents de bureau"""

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import List, Optional
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_agent
from app.schemas.ticket import (
    TicketCreate, TicketResponse, TicketPayoutRequest,
    TicketInfoResponse, CashierSessionCreate, CashierSessionClose
)
from app.schemas.wallet import DepositRequest, WithdrawRequest
from app.schemas.common import SuccessResponse
from app.services.ticket_service import TicketService
from app.services.wallet_service import WalletService
from app.models.user import User
from app.models.ticket import Ticket, TicketStatus
from app.models.bureau import Bureau, CashierSession
from app.models.transaction import Transaction, TransactionType, PaymentMethod, TransactionStatus
import redis.asyncio as redis

router = APIRouter(prefix="/agent", tags=["Agent"])


# ==================== CAISSE ====================

@router.post(
    "/cashier/open",
    response_model=SuccessResponse,
    summary="Ouvrir caisse",
    description="Ouvre une session de caisse pour l'agent"
)
async def open_cashier_session(
    request: CashierSessionCreate,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Ouvre une session de caisse."""
    
    # Vérifier si l'agent a déjà une session ouverte
    existing = await db.execute(
        select(CashierSession).where(
            and_(
                CashierSession.agent_id == current_agent.id,
                CashierSession.status == "OPEN"
            )
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Une session de caisse est déjà ouverte")
    
    # Vérifier le bureau
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == request.bureau_id)
    )
    bureau = bureau_result.scalar_one_or_none()
    
    if not bureau:
        raise HTTPException(status_code=404, detail="Bureau non trouvé")
    
    # Créer la session
    session = CashierSession(
        bureau_id=request.bureau_id,
        agent_id=current_agent.id,
        starting_balance=request.starting_balance,
        current_balance=request.starting_balance,
        expected_balance=request.starting_balance,
        opened_at=datetime.utcnow()
    )
    
    db.add(session)
    await db.commit()
    
    return SuccessResponse(
        message=f"Session de caisse ouverte - Solde initial: {request.starting_balance} HTG",
        data={"session_id": session.id}
    )


@router.post(
    "/cashier/close",
    response_model=SuccessResponse,
    summary="Fermer caisse",
    description="Ferme la session de caisse et calcule l'écart"
)
async def close_cashier_session(
    request: CashierSessionClose,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Ferme la session de caisse."""
    
    # Récupérer la session ouverte
    session_result = await db.execute(
        select(CashierSession).where(
            and_(
                CashierSession.agent_id == current_agent.id,
                CashierSession.status == "OPEN"
            )
        )
    )
    session = session_result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=400, detail="Aucune session de caisse ouverte")
    
    # Calculer l'écart
    expected_balance = session.starting_balance + session.cash_in_amount - session.cash_out_amount
    difference = request.actual_balance - expected_balance
    
    session.expected_balance = expected_balance
    session.current_balance = request.actual_balance
    session.difference = difference
    session.difference_reason = request.difference_reason
    session.status = "CLOSED"
    session.closed_at = datetime.utcnow()
    
    await db.commit()
    
    message = f"Session fermée. Écart: {difference} HTG"
    if abs(difference) > 100:
        message += f" - Raison: {request.difference_reason or 'Non spécifiée'}"
    
    return SuccessResponse(
        message=message,
        data={
            "expected_balance": float(expected_balance),
            "actual_balance": float(request.actual_balance),
            "difference": float(difference),
            "cash_in_total": float(session.cash_in_amount),
            "cash_out_total": float(session.cash_out_amount)
        }
    )


@router.get(
    "/cashier/session",
    summary="Session en cours",
    description="Récupère les informations de la session de caisse en cours"
)
async def get_current_session(
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Récupère la session de caisse en cours."""
    
    session_result = await db.execute(
        select(CashierSession).where(
            and_(
                CashierSession.agent_id == current_agent.id,
                CashierSession.status == "OPEN"
            )
        )
    )
    session = session_result.scalar_one_or_none()
    
    if not session:
        return {"has_open_session": False}
    
    return {
        "has_open_session": True,
        "session_id": session.id,
        "starting_balance": float(session.starting_balance),
        "current_balance": float(session.current_balance),
        "cash_in_amount": float(session.cash_in_amount),
        "cash_out_amount": float(session.cash_out_amount),
        "cash_in_count": session.cash_in_count,
        "cash_out_count": session.cash_out_count,
        "opened_at": session.opened_at
    }


# ==================== TICKETS ====================

@router.post(
    "/tickets",
    response_model=TicketResponse,
    summary="Créer ticket",
    description="Crée un ticket pour un joueur sans compte (encaissement cash)"
)
async def create_ticket(
    ticket_data: TicketCreate,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Crée un nouveau ticket."""
    
    # Vérifier la session de caisse ouverte
    session_result = await db.execute(
        select(CashierSession).where(
            and_(
                CashierSession.agent_id == current_agent.id,
                CashierSession.status == "OPEN"
            )
        )
    )
    session = session_result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=400, detail="Aucune session de caisse ouverte")
    
    # Vérifier le bureau
    if not current_agent.bureau_id:
        raise HTTPException(status_code=400, detail="Agent non affecté à un bureau")
    
    # Créer le ticket
    ticket_service = TicketService(db, redis_client)
    ticket = await ticket_service.create_ticket(
        agent_id=current_agent.id,
        bureau_id=current_agent.bureau_id,
        amount=ticket_data.amount,
        player_name=ticket_data.player_name,
        player_phone=ticket_data.player_phone
    )
    
    # Mettre à jour la session de caisse
    session.cash_in_count += 1
    session.cash_in_amount += ticket_data.amount
    session.current_balance += ticket_data.amount
    
    await db.commit()
    
    return ticket


@router.get(
    "/tickets/{ticket_number}",
    response_model=TicketInfoResponse,
    summary="Infos ticket",
    description="Récupère les informations d'un ticket"
)
async def get_ticket_info(
    ticket_number: str,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Récupère les informations d'un ticket."""
    
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")
    
    return ticket


@router.post(
    "/tickets/payout",
    response_model=SuccessResponse,
    summary="Payer ticket",
    description="Effectue le paiement cash d'un ticket"
)
async def payout_ticket(
    request: TicketPayoutRequest,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Paye un ticket."""
    
    # Vérifier la session de caisse ouverte
    session_result = await db.execute(
        select(CashierSession).where(
            and_(
                CashierSession.agent_id == current_agent.id,
                CashierSession.status == "OPEN"
            )
        )
    )
    session = session_result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=400, detail="Aucune session de caisse ouverte")
    
    # Récupérer le ticket
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == request.ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Ticket déjà payé ou expiré")
    
    if ticket.balance <= 0:
        raise HTTPException(status_code=400, detail="Aucun solde à payer")
    
    if ticket.expires_at < datetime.utcnow():
        ticket.status = TicketStatus.EXPIRED
        await db.commit()
        raise HTTPException(status_code=400, detail="Ticket expiré")
    
    amount = ticket.balance
    
    # Payer le ticket
    ticket.status = TicketStatus.PAID
    ticket.paid_at = datetime.utcnow()
    ticket.paid_by_agent = current_agent.id
    ticket.balance = 0
    
    # Mettre à jour la session de caisse
    session.cash_out_count += 1
    session.cash_out_amount += amount
    session.current_balance -= amount
    
    # Mettre à jour la caisse du bureau
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == ticket.bureau_id)
    )
    bureau = bureau_result.scalar_one()
    bureau.cash_balance -= amount
    
    await db.commit()
    
    return SuccessResponse(
        message=f"Ticket payé: {amount} HTG",
        data={"amount": float(amount), "ticket_number": ticket.ticket_number}
    )


@router.get(
    "/tickets/active",
    response_model=List[TicketInfoResponse],
    summary="Tickets actifs",
    description="Récupère la liste des tickets actifs du bureau"
)
async def get_active_tickets(
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Récupère les tickets actifs."""
    
    if not current_agent.bureau_id:
        raise HTTPException(status_code=400, detail="Agent non affecté à un bureau")
    
    result = await db.execute(
        select(Ticket)
        .where(Ticket.bureau_id == current_agent.bureau_id)
        .where(Ticket.status == TicketStatus.ACTIVE)
        .where(Ticket.expires_at > datetime.utcnow())
        .order_by(Ticket.created_at.desc())
        .limit(50)
    )
    tickets = result.scalars().all()
    
    return tickets


# ==================== DÉPÔTS / RETRAITS COMPTE ====================

@router.post(
    "/deposit",
    response_model=SuccessResponse,
    summary="Dépôt sur compte (cash)",
    description="Effectue un dépôt cash sur le compte d'un joueur"
)
async def agent_deposit(
    phone: str,
    amount: float,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Dépôt cash sur compte joueur."""
    
    # Vérifier la session de caisse
    session_result = await db.execute(
        select(CashierSession).where(
            and_(
                CashierSession.agent_id == current_agent.id,
                CashierSession.status == "OPEN"
            )
        )
    )
    session = session_result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=400, detail="Aucune session de caisse ouverte")
    
    # Récupérer l'utilisateur
    from app.services.user_service import UserService
    user_service = UserService(db, redis_client)
    user = await user_service.get_user_by_phone(phone)
    
    if not user:
        raise HTTPException(status_code=404, detail="Joueur non trouvé")
    
    # Effectuer le dépôt
    wallet_service = WalletService(db, redis_client)
    transaction = await wallet_service.deposit(
        user_id=user.id,
        amount=Decimal(str(amount)),
        payment_method=PaymentMethod.CASH,
        external_reference=f"AGENT-{current_agent.id}",
        ip_address=None,
        user_agent=None
    )
    
    # Mettre à jour la session de caisse
    session.cash_in_count += 1
    session.cash_in_amount += Decimal(str(amount))
    session.current_balance += Decimal(str(amount))
    
    # Mettre à jour la caisse du bureau
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == current_agent.bureau_id)
    )
    bureau = bureau_result.scalar_one()
    bureau.cash_balance += Decimal(str(amount))
    
    await db.commit()
    
    return SuccessResponse(
        message=f"Dépôt de {amount} HTG effectué sur le compte de {user.full_name}",
        data={
            "user_id": user.id,
            "user_name": user.full_name,
            "new_balance": float(user.wallet.balance) if user.wallet else 0,
            "transaction_ref": transaction.reference
        }
    )


@router.post(
    "/withdraw",
    response_model=SuccessResponse,
    summary="Retrait sur compte (cash)",
    description="Effectue un retrait cash sur le compte d'un joueur"
)
async def agent_withdraw(
    phone: str,
    amount: float,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Retrait cash depuis compte joueur."""
    
    # Vérifier la session de caisse
    session_result = await db.execute(
        select(CashierSession).where(
            and_(
                CashierSession.agent_id == current_agent.id,
                CashierSession.status == "OPEN"
            )
        )
    )
    session = session_result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(status_code=400, detail="Aucune session de caisse ouverte")
    
    # Vérifier la caisse du bureau
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == current_agent.bureau_id)
    )
    bureau = bureau_result.scalar_one()
    
    if bureau.cash_balance < amount:
        raise HTTPException(status_code=400, detail="Caisse insuffisante")
    
    # Récupérer l'utilisateur
    from app.services.user_service import UserService
    user_service = UserService(db, redis_client)
    user = await user_service.get_user_by_phone(phone)
    
    if not user:
        raise HTTPException(status_code=404, detail="Joueur non trouvé")
    
    # Effectuer le retrait
    wallet_service = WalletService(db, redis_client)
    transaction = await wallet_service.withdraw(
        user_id=user.id,
        amount=Decimal(str(amount)),
        payment_method=PaymentMethod.CASH,
        destination="BUREAU",
        ip_address=None,
        user_agent=None
    )
    
    # Confirmer immédiatement (retrait cash)
    transaction.status = TransactionStatus.COMPLETED
    transaction.completed_at = datetime.utcnow()
    
    # Mettre à jour la session de caisse
    session.cash_out_count += 1
    session.cash_out_amount += Decimal(str(amount))
    session.current_balance -= Decimal(str(amount))
    
    # Mettre à jour la caisse du bureau
    bureau.cash_balance -= Decimal(str(amount))
    
    await db.commit()
    
    return SuccessResponse(
        message=f"Retrait de {amount} HTG effectué pour {user.full_name}",
        data={
            "user_id": user.id,
            "user_name": user.full_name,
            "new_balance": float(user.wallet.balance) if user.wallet else 0,
            "transaction_ref": transaction.reference
        }
    )


# ==================== STATISTIQUES AGENT ====================

@router.get(
    "/statistics",
    summary="Statistiques agent",
    description="Récupère les statistiques de l'agent pour la journée"
)
async def get_agent_statistics(
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Récupère les statistiques de l'agent."""
    
    today = datetime.utcnow().date()
    today_start = datetime.combine(today, datetime.min.time())
    today_end = datetime.combine(today, datetime.max.time())
    
    # Tickets créés aujourd'hui
    tickets_result = await db.execute(
        select(
            func.count(Ticket.id).label("count"),
            func.coalesce(func.sum(Ticket.initial_amount), 0).label("total")
        ).where(
            and_(
                Ticket.agent_id == current_agent.id,
                Ticket.created_at >= today_start,
                Ticket.created_at <= today_end
            )
        )
    )
    tickets = tickets_result.one()
    
    # Tickets payés aujourd'hui
    paid_result = await db.execute(
        select(
            func.count(Ticket.id).label("count"),
            func.coalesce(func.sum(Ticket.initial_amount), 0).label("total")
        ).where(
            and_(
                Ticket.paid_by_agent == current_agent.id,
                Ticket.paid_at >= today_start,
                Ticket.paid_at <= today_end
            )
        )
    )
    paid = paid_result.one()
    
    # Paris placés aujourd'hui
    from app.models.keno import KenoBet
    bets_result = await db.execute(
        select(
            func.count(KenoBet.id).label("count"),
            func.coalesce(func.sum(KenoBet.stake), 0).label("total_stake")
        ).where(
            and_(
                KenoBet.agent_id == current_agent.id,
                KenoBet.placed_at >= today_start,
                KenoBet.placed_at <= today_end
            )
        )
    )
    bets = bets_result.one()
    
    return {
        "tickets_created": {
            "count": tickets.count or 0,
            "total": float(tickets.total)
        },
        "tickets_paid": {
            "count": paid.count or 0,
            "total": float(paid.total)
        },
        "bets_placed": {
            "count": bets.count or 0,
            "total_stake": float(bets.total_stake)
        },
        "net_cash": {
            "in": float(tickets.total),
            "out": float(paid.total),
            "net": float(tickets.total - paid.total)
        }
    }