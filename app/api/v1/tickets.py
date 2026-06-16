# app/api/v1/tickets.py
"""API complète pour la gestion des tickets (jeu sans compte)"""

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from typing import List, Optional
from datetime import datetime, timedelta
from decimal import Decimal
import qrcode
from io import BytesIO
import base64

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_user, get_current_agent, get_current_admin
from app.schemas.ticket import (
    TicketCreate, TicketResponse, TicketInfoResponse,
    TicketBetRequest, TicketPayoutRequest, TicketSearchRequest,
    TicketStatisticsResponse, TicketRechargeRequest, TicketTransactionResponse
)
from app.schemas.common import SuccessResponse, PaginatedResponse, ErrorResponse
from app.models.ticket import Ticket, TicketStatus
from app.models.keno import KenoBet, KenoDraw, KenoDrawStatus
from app.models.lucky import LuckyPlay
from app.models.bureau import Bureau, CashierSession
from app.models.user import User
from app.models.transaction import Transaction
from app.models.audit import AuditLog, AuditAction
import redis.asyncio as redis

router = APIRouter(prefix="/tickets", tags=["Tickets"])


# ==================== CRÉATION ET GESTION ====================

@router.post(
    "/",
    response_model=TicketResponse,
    status_code=201,
    summary="Créer un ticket",
    description="Crée un nouveau ticket pour un joueur sans compte (encaissement cash)"
)
async def create_ticket(
    ticket_data: TicketCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Crée un nouveau ticket après encaissement cash.
    
    - **amount**: Montant encaissé (min 10 HTG, max 500 000 HTG)
    - **player_name**: Nom du joueur (optionnel)
    - **player_phone**: Téléphone du joueur (optionnel, pour SMS)
    
    Un ticket permet de jouer sans compte. Le ticket expire après 7 jours.
    """
    
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
        raise HTTPException(
            status_code=400, 
            detail="Aucune session de caisse ouverte. Veuillez ouvrir la caisse d'abord."
        )
    
    # Vérifier le bureau
    if not current_agent.bureau_id:
        raise HTTPException(
            status_code=400, 
            detail="Agent non affecté à un bureau"
        )
    
    # Vérifier le montant
    if ticket_data.amount < 10:
        raise HTTPException(400, "Montant minimum: 10 HTG")
    if ticket_data.amount > 500000:
        raise HTTPException(400, "Montant maximum: 500 000 HTG")
    
    # Vérifier la caisse du bureau
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == current_agent.bureau_id)
    )
    bureau = bureau_result.scalar_one()
    
    if bureau.cash_balance < 0:
        raise HTTPException(400, "Erreur de caisse, contactez l'administrateur")
    
    # Créer le ticket
    ticket = Ticket(
        ticket_number=Ticket.generate_ticket_number(),
        bureau_id=current_agent.bureau_id,
        agent_id=current_agent.id,
        player_name=ticket_data.player_name,
        player_phone=ticket_data.player_phone,
        balance=ticket_data.amount,
        initial_amount=ticket_data.amount,
        expires_at=datetime.utcnow() + timedelta(days=7)
    )
    
    db.add(ticket)
    await db.flush()
    
    # Mettre à jour la session de caisse
    session.cash_in_count += 1
    session.cash_in_amount += ticket_data.amount
    session.current_balance += ticket_data.amount
    
    # Mettre à jour la caisse du bureau
    bureau.cash_balance += ticket_data.amount
    
    # Audit log
    audit = AuditLog(
        user_id=current_agent.id,
        action=AuditAction.DEPOSIT,
        resource_type="ticket",
        resource_id=ticket.id,
        new_values={
            "ticket_number": ticket.ticket_number,
            "amount": float(ticket_data.amount),
            "player_name": ticket_data.player_name
        },
        ip_address=request.client.host if request.client else None
    )
    db.add(audit)
    
    await db.commit()
    
    # Générer le QR code
    qr_base64 = generate_qr_code(ticket.ticket_number)
    
    # Envoyer SMS si numéro fourni
    if ticket_data.player_phone:
        background_tasks.add_task(
            send_ticket_created_sms,
            ticket_data.player_phone,
            ticket.ticket_number,
            float(ticket_data.amount)
        )
    
    return {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "balance": float(ticket.balance),
        "initial_amount": float(ticket.initial_amount),
        "status": ticket.status,
        "expires_at": ticket.expires_at,
        "created_at": ticket.created_at,
        "qr_code": qr_base64
    }


@router.get(
    "/{ticket_number}",
    response_model=TicketInfoResponse,
    summary="Informations ticket",
    description="Récupère les informations détaillées d'un ticket"
)
async def get_ticket_info(
    ticket_number: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Récupère les informations d'un ticket.
    
    Accessible aux agents, managers et admins uniquement.
    """
    
    # Vérifier les permissions
    if not (current_user.is_agent or current_user.is_admin):
        raise HTTPException(403, "Accès non autorisé")
    
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    # Récupérer l'historique des paris
    bets_result = await db.execute(
        select(KenoBet).where(KenoBet.ticket_id == ticket.id)
        .order_by(KenoBet.placed_at.desc())
        .limit(20)
    )
    bets = bets_result.scalars().all()
    
    # Récupérer les parties Lucky
    lucky_result = await db.execute(
        select(LuckyPlay).where(LuckyPlay.ticket_id == ticket.id)
        .order_by(LuckyPlay.played_at.desc())
        .limit(20)
    )
    lucky_plays = lucky_result.scalars().all()
    
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
        "bureau_id": ticket.bureau_id,
        "agent_id": ticket.agent_id,
        "recent_bets": [
            {
                "id": b.id,
                "game": "keno",
                "stake": float(b.stake),
                "winnings": float(b.winnings),
                "status": b.status,
                "played_at": b.placed_at
            }
            for b in bets
        ],
        "recent_lucky_plays": [
            {
                "id": l.id,
                "stake": float(l.stake),
                "multiplier": float(l.multiplier),
                "winnings": float(l.winnings),
                "segment": l.result_segment["label"],
                "played_at": l.played_at
            }
            for l in lucky_plays
        ]
    }


@router.get(
    "/bureau/{bureau_id}/active",
    response_model=List[TicketInfoResponse],
    summary="Tickets actifs du bureau",
    description="Récupère tous les tickets actifs d'un bureau"
)
async def get_active_tickets_by_bureau(
    bureau_id: str,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Récupère les tickets actifs d'un bureau spécifique.
    
    Accessible aux managers et admins uniquement.
    """
    
    if not (current_user.is_agent or current_user.is_admin):
        raise HTTPException(403, "Accès non autorisé")
    
    # Vérifier le bureau
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == bureau_id)
    )
    bureau = bureau_result.scalar_one_or_none()
    
    if not bureau:
        raise HTTPException(404, "Bureau non trouvé")
    
    result = await db.execute(
        select(Ticket)
        .where(Ticket.bureau_id == bureau_id)
        .where(Ticket.status == TicketStatus.ACTIVE)
        .where(Ticket.expires_at > datetime.utcnow())
        .order_by(Ticket.created_at.desc())
        .limit(limit)
    )
    tickets = result.scalars().all()
    
    return [
        {
            "id": t.id,
            "ticket_number": t.ticket_number,
            "player_name": t.player_name,
            "balance": float(t.balance),
            "initial_amount": float(t.initial_amount),
            "status": t.status,
            "expires_at": t.expires_at,
            "created_at": t.created_at
        }
        for t in tickets
    ]


# ==================== RECHARGE DE TICKET ====================

@router.post(
    "/{ticket_number}/recharge",
    response_model=TicketResponse,
    summary="Recharger un ticket",
    description="Ajoute de l'argent à un ticket existant"
)
async def recharge_ticket(
    ticket_number: str,
    recharge_data: TicketRechargeRequest,
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Recharge un ticket existant avec de l'argent cash.
    
    - **amount**: Montant à ajouter (min 10 HTG)
    """
    
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
        raise HTTPException(400, "Aucune session de caisse ouverte")
    
    # Vérifier le ticket
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        raise HTTPException(400, "Ticket expiré ou déjà payé")
    
    if ticket.expires_at < datetime.utcnow():
        ticket.status = TicketStatus.EXPIRED
        await db.commit()
        raise HTTPException(400, "Ticket expiré")
    
    # Vérifier le montant
    if recharge_data.amount < 10:
        raise HTTPException(400, "Montant minimum: 10 HTG")
    
    # Ajouter au solde
    old_balance = ticket.balance
    ticket.balance += recharge_data.amount
    
    # Mettre à jour la session de caisse
    session.cash_in_count += 1
    session.cash_in_amount += recharge_data.amount
    session.current_balance += recharge_data.amount
    
    # Mettre à jour la caisse du bureau
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == ticket.bureau_id)
    )
    bureau = bureau_result.scalar_one()
    bureau.cash_balance += recharge_data.amount
    
    # Audit log
    audit = AuditLog(
        user_id=current_agent.id,
        action=AuditAction.DEPOSIT,
        resource_type="ticket",
        resource_id=ticket.id,
        old_values={"balance": float(old_balance)},
        new_values={"balance": float(ticket.balance)},
        metadata={"recharge_amount": float(recharge_data.amount)},
        ip_address=request.client.host if request.client else None
    )
    db.add(audit)
    
    await db.commit()
    
    # Générer le QR code
    qr_base64 = generate_qr_code(ticket.ticket_number)
    
    return {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "balance": float(ticket.balance),
        "initial_amount": float(ticket.initial_amount),
        "status": ticket.status,
        "expires_at": ticket.expires_at,
        "created_at": ticket.created_at,
        "qr_code": qr_base64
    }


# ==================== PAIEMENT ====================

@router.post(
    "/{ticket_number}/payout",
    response_model=SuccessResponse,
    summary="Payer un ticket",
    description="Effectue le paiement cash d'un ticket"
)
async def payout_ticket(
    ticket_number: str,
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Paiement cash d'un ticket.
    
    Le ticket est marqué comme payé et ne peut plus être utilisé.
    """
    
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
        raise HTTPException(400, "Aucune session de caisse ouverte")
    
    # Vérifier le ticket
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        if ticket.status == TicketStatus.PAID:
            raise HTTPException(400, "Ce ticket a déjà été payé")
        elif ticket.status == TicketStatus.EXPIRED:
            raise HTTPException(400, "Ce ticket est expiré")
        else:
            raise HTTPException(400, f"Ticket {ticket.status}")
    
    if ticket.balance <= 0:
        raise HTTPException(400, "Aucun solde à payer sur ce ticket")
    
    if ticket.expires_at < datetime.utcnow():
        ticket.status = TicketStatus.EXPIRED
        await db.commit()
        raise HTTPException(400, "Ticket expiré")
    
    # Vérifier la caisse du bureau
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == ticket.bureau_id)
    )
    bureau = bureau_result.scalar_one()
    
    if bureau.cash_balance < ticket.balance:
        raise HTTPException(400, f"Caisse insuffisante. Solde caisse: {bureau.cash_balance} HTG")
    
    amount_to_pay = ticket.balance
    
    # Marquer comme payé
    old_balance = ticket.balance
    ticket.status = TicketStatus.PAID
    ticket.paid_at = datetime.utcnow()
    ticket.paid_by_agent = current_agent.id
    ticket.balance = 0
    
    # Mettre à jour la session de caisse
    session.cash_out_count += 1
    session.cash_out_amount += amount_to_pay
    session.current_balance -= amount_to_pay
    
    # Mettre à jour la caisse du bureau
    bureau.cash_balance -= amount_to_pay
    
    # Audit log
    audit = AuditLog(
        user_id=current_agent.id,
        action=AuditAction.WITHDRAWAL,
        resource_type="ticket",
        resource_id=ticket.id,
        old_values={"balance": float(old_balance), "status": ticket.status},
        new_values={"balance": 0, "status": TicketStatus.PAID},
        metadata={"paid_amount": float(amount_to_pay)},
        ip_address=request.client.host if request.client else None
    )
    db.add(audit)
    
    await db.commit()
    
    # Envoyer SMS si numéro disponible
    if ticket.player_phone:
        await send_payout_confirmation_sms(
            ticket.player_phone,
            ticket.ticket_number,
            float(amount_to_pay)
        )
    
    return SuccessResponse(
        message=f"Ticket payé avec succès: {amount_to_pay} HTG",
        data={
            "ticket_number": ticket.ticket_number,
            "amount_paid": float(amount_to_pay),
            "paid_at": ticket.paid_at,
            "paid_by": current_agent.full_name
        }
    )


@router.post(
    "/{ticket_number}/partial-payout",
    response_model=SuccessResponse,
    summary="Paiement partiel",
    description="Effectue un paiement partiel d'un ticket"
)
async def partial_payout_ticket(
    ticket_number: str,
    amount: float,
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Paiement partiel d'un ticket.
    
    Permet de payer une partie du solde et de garder le reste sur le ticket.
    """
    
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
        raise HTTPException(400, "Aucune session de caisse ouverte")
    
    # Vérifier le ticket
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        raise HTTPException(400, "Ticket non actif")
    
    if ticket.balance < amount:
        raise HTTPException(400, "Solde insuffisant")
    
    if amount <= 0:
        raise HTTPException(400, "Montant invalide")
    
    # Vérifier la caisse
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == ticket.bureau_id)
    )
    bureau = bureau_result.scalar_one()
    
    if bureau.cash_balance < amount:
        raise HTTPException(400, "Caisse insuffisante")
    
    # Effectuer le paiement partiel
    old_balance = ticket.balance
    ticket.balance -= amount
    
    # Mettre à jour la session
    session.cash_out_count += 1
    session.cash_out_amount += amount
    session.current_balance -= amount
    
    # Mettre à jour la caisse
    bureau.cash_balance -= amount
    
    # Si solde devient nul, marquer comme payé
    if ticket.balance == 0:
        ticket.status = TicketStatus.PAID
        ticket.paid_at = datetime.utcnow()
        ticket.paid_by_agent = current_agent.id
    
    # Audit
    audit = AuditLog(
        user_id=current_agent.id,
        action=AuditAction.WITHDRAWAL,
        resource_type="ticket",
        resource_id=ticket.id,
        old_values={"balance": float(old_balance)},
        new_values={"balance": float(ticket.balance)},
        metadata={"partial_amount": float(amount)}
    )
    db.add(audit)
    
    await db.commit()
    
    return SuccessResponse(
        message=f"Paiement partiel de {amount} HTG effectué. Solde restant: {ticket.balance} HTG",
        data={
            "ticket_number": ticket.ticket_number,
            "amount_paid": float(amount),
            "remaining_balance": float(ticket.balance),
            "is_fully_paid": ticket.status == TicketStatus.PAID
        }
    )


# ==================== ANNULATION ET REMBOURSEMENT ====================

@router.post(
    "/{ticket_number}/cancel",
    response_model=SuccessResponse,
    summary="Annuler un ticket",
    description="Annule un ticket et rembourse le solde (Admin uniquement)"
)
async def cancel_ticket(
    ticket_number: str,
    reason: str = Query(..., description="Raison de l'annulation"),
    current_admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Annule un ticket et rembourse le solde.
    
    Réservé aux administrateurs.
    """
    
    # Vérifier le ticket
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    if ticket.status != TicketStatus.ACTIVE:
        raise HTTPException(400, "Ce ticket ne peut pas être annulé")
    
    # Vérifier s'il y a des paris en attente
    bets_result = await db.execute(
        select(KenoBet).where(
            and_(
                KenoBet.ticket_id == ticket.id,
                KenoBet.status == "PENDING"
            )
        )
    )
    pending_bets = bets_result.scalars().all()
    
    if pending_bets:
        raise HTTPException(
            400, 
            f"Impossible d'annuler: {len(pending_bets)} paris en attente. "
            "Attendez que les tirages soient effectués."
        )
    
    # Rembourser le solde
    amount_to_refund = ticket.balance
    
    # Mettre à jour la caisse du bureau (débit)
    bureau_result = await db.execute(
        select(Bureau).where(Bureau.id == ticket.bureau_id)
    )
    bureau = bureau_result.scalar_one()
    bureau.cash_balance -= amount_to_refund
    
    # Marquer comme annulé
    ticket.status = TicketStatus.CANCELLED
    ticket.balance = 0
    ticket.paid_at = datetime.utcnow()
    ticket.paid_by_agent = current_admin.id
    
    # Audit
    audit = AuditLog(
        user_id=current_admin.id,
        action=AuditAction.USER_UPDATED,
        resource_type="ticket",
        resource_id=ticket.id,
        old_values={"status": "ACTIVE", "balance": float(amount_to_refund)},
        new_values={"status": "CANCELLED", "balance": 0},
        reason=reason
    )
    db.add(audit)
    
    await db.commit()
    
    return SuccessResponse(
        message=f"Ticket annulé. Remboursement de {amount_to_refund} HTG effectué.",
        data={
            "ticket_number": ticket.ticket_number,
            "refund_amount": float(amount_to_refund),
            "reason": reason
        }
    )


# ==================== RECHERCHE ====================

@router.post(
    "/search",
    response_model=PaginatedResponse[TicketInfoResponse],
    summary="Rechercher des tickets",
    description="Recherche avancée de tickets"
)
async def search_tickets(
    search_data: TicketSearchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Recherche avancée de tickets.
    
    Accessible aux agents, managers et admins.
    """
    
    if not (current_user.is_agent or current_user.is_admin):
        raise HTTPException(403, "Accès non autorisé")
    
    query = select(Ticket)
    
    # Filtres
    if search_data.ticket_number:
        query = query.where(Ticket.ticket_number.contains(search_data.ticket_number))
    
    if search_data.player_name:
        query = query.where(Ticket.player_name.contains(search_data.player_name))
    
    if search_data.player_phone:
        query = query.where(Ticket.player_phone.contains(search_data.player_phone))
    
    if search_data.bureau_id:
        query = query.where(Ticket.bureau_id == search_data.bureau_id)
    
    if search_data.agent_id:
        query = query.where(Ticket.agent_id == search_data.agent_id)
    
    if search_data.status:
        query = query.where(Ticket.status == search_data.status)
    
    if search_data.min_amount:
        query = query.where(Ticket.initial_amount >= search_data.min_amount)
    
    if search_data.max_amount:
        query = query.where(Ticket.initial_amount <= search_data.max_amount)
    
    if search_data.start_date:
        query = query.where(Ticket.created_at >= search_data.start_date)
    
    if search_data.end_date:
        query = query.where(Ticket.created_at <= search_data.end_date)
    
    # Pagination
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar()
    
    query = query.order_by(Ticket.created_at.desc())
    query = query.offset((search_data.page - 1) * search_data.per_page).limit(search_data.per_page)
    
    result = await db.execute(query)
    tickets = result.scalars().all()
    
    return {
        "items": [
            {
                "id": t.id,
                "ticket_number": t.ticket_number,
                "player_name": t.player_name,
                "player_phone": t.player_phone,
                "balance": float(t.balance),
                "initial_amount": float(t.initial_amount),
                "status": t.status,
                "expires_at": t.expires_at,
                "created_at": t.created_at,
                "paid_at": t.paid_at
            }
            for t in tickets
        ],
        "total": total,
        "page": search_data.page,
        "per_page": search_data.per_page,
        "pages": (total + search_data.per_page - 1) // search_data.per_page,
        "has_next": search_data.page * search_data.per_page < total,
        "has_prev": search_data.page > 1
    }


# ==================== STATISTIQUES ====================

@router.get(
    "/statistics/bureau/{bureau_id}",
    response_model=TicketStatisticsResponse,
    summary="Statistiques des tickets",
    description="Statistiques des tickets pour un bureau"
)
async def get_ticket_statistics(
    bureau_id: str,
    period: str = Query("day", regex="^(day|week|month)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Récupère les statistiques des tickets pour un bureau.
    
    - **day**: Dernières 24h
    - **week**: 7 derniers jours
    - **month**: 30 derniers jours
    """
    
    if not (current_user.is_agent or current_user.is_admin):
        raise HTTPException(403, "Accès non autorisé")
    
    # Calculer la date de début
    now = datetime.utcnow()
    if period == "day":
        start_date = now - timedelta(days=1)
    elif period == "week":
        start_date = now - timedelta(days=7)
    else:  # month
        start_date = now - timedelta(days=30)
    
    # Tickets créés
    created_result = await db.execute(
        select(
            func.count(Ticket.id).label("count"),
            func.coalesce(func.sum(Ticket.initial_amount), 0).label("total")
        ).where(
            and_(
                Ticket.bureau_id == bureau_id,
                Ticket.created_at >= start_date
            )
        )
    )
    created = created_result.one()
    
    # Tickets payés
    paid_result = await db.execute(
        select(
            func.count(Ticket.id).label("count"),
            func.coalesce(func.sum(Ticket.initial_amount), 0).label("total")
        ).where(
            and_(
                Ticket.bureau_id == bureau_id,
                Ticket.paid_at >= start_date,
                Ticket.status == TicketStatus.PAID
            )
        )
    )
    paid = paid_result.one()
    
    # Tickets expirés
    expired_result = await db.execute(
        select(
            func.count(Ticket.id).label("count"),
            func.coalesce(func.sum(Ticket.balance), 0).label("total")
        ).where(
            and_(
                Ticket.bureau_id == bureau_id,
                Ticket.expires_at < now,
                Ticket.status == TicketStatus.ACTIVE
            )
        )
    )
    expired = expired_result.one()
    
    # Tickets actifs
    active_result = await db.execute(
        select(
            func.count(Ticket.id).label("count"),
            func.coalesce(func.sum(Ticket.balance), 0).label("total")
        ).where(
            and_(
                Ticket.bureau_id == bureau_id,
                Ticket.status == TicketStatus.ACTIVE,
                Ticket.expires_at > now
            )
        )
    )
    active = active_result.one()
    
    return {
        "period": period,
        "start_date": start_date,
        "end_date": now,
        "created": {
            "count": created.count or 0,
            "total_amount": float(created.total)
        },
        "paid": {
            "count": paid.count or 0,
            "total_amount": float(paid.total)
        },
        "expired": {
            "count": expired.count or 0,
            "total_amount": float(expired.total)
        },
        "active": {
            "count": active.count or 0,
            "total_balance": float(active.total)
        },
        "conversion_rate": round((paid.count or 0) / (created.count or 1) * 100, 2)
    }


# ==================== HISTORIQUE ====================

@router.get(
    "/{ticket_number}/transactions",
    response_model=List[TicketTransactionResponse],
    summary="Historique des transactions",
    description="Historique complet des transactions d'un ticket"
)
async def get_ticket_transactions(
    ticket_number: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Récupère l'historique complet des transactions d'un ticket.
    
    Inclut les paris Keno, les parties Lucky, les recharges, etc.
    """
    
    if not (current_user.is_agent or current_user.is_admin):
        raise HTTPException(403, "Accès non autorisé")
    
    # Vérifier le ticket
    result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(404, "Ticket non trouvé")
    
    transactions = []
    
    # Paris Keno
    keno_result = await db.execute(
        select(KenoBet).where(KenoBet.ticket_id == ticket.id)
        .order_by(KenoBet.placed_at.desc())
    )
    for bet in keno_result.scalars().all():
        transactions.append({
            "id": bet.id,
            "type": "keno_bet",
            "amount": float(-bet.stake),
            "balance_after": None,  # Pas de suivi de balance pour ticket
            "status": "completed",
            "created_at": bet.placed_at,
            "metadata": {
                "draw_id": bet.draw_id,
                "picks": bet.picks,
                "hits": bet.hits,
                "winnings": float(bet.winnings) if bet.winnings else 0
            }
        })
    
    # Parties Lucky
    lucky_result = await db.execute(
        select(LuckyPlay).where(LuckyPlay.ticket_id == ticket.id)
        .order_by(LuckyPlay.played_at.desc())
    )
    for play in lucky_result.scalars().all():
        transactions.append({
            "id": play.id,
            "type": "lucky_play",
            "amount": float(-play.stake),
            "balance_after": None,
            "status": "completed",
            "created_at": play.played_at,
            "metadata": {
                "segment": play.result_segment["label"],
                "multiplier": float(play.multiplier),
                "winnings": float(play.winnings)
            }
        })
    
    # Gains (si > 0)
    winning_bets = [t for t in transactions if t.get("metadata", {}).get("winnings", 0) > 0]
    for bet in winning_bets:
        if bet["metadata"]["winnings"] > 0:
            transactions.append({
                "id": f"win_{bet['id']}",
                "type": "win",
                "amount": float(bet["metadata"]["winnings"]),
                "balance_after": None,
                "status": "completed",
                "created_at": bet["created_at"],
                "metadata": {
                    "source_type": bet["type"],
                    "source_id": bet["id"]
                }
            })
    
    # Tri par date
    transactions.sort(key=lambda x: x["created_at"], reverse=True)
    
    return transactions


# ==================== FONCTIONS UTILITAIRES ====================

def generate_qr_code(ticket_number: str) -> str:
    """
    Génère un QR code en base64 pour le ticket.
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(ticket_number)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"


async def send_ticket_created_sms(phone: str, ticket_number: str, amount: float):
    """
    Envoie un SMS de confirmation de création de ticket.
    """
    # À implémenter avec Twilio ou autre service SMS
    # from app.payments.sms import send_sms
    # await send_sms(
    #     phone,
    #     f"Votre ticket {ticket_number} de {amount} HTG a été créé. "
    #     f"Valable 7 jours. Présentez ce code pour jouer."
    # )
    pass


async def send_payout_confirmation_sms(phone: str, ticket_number: str, amount: float):
    """
    Envoie un SMS de confirmation de paiement.
    """
    # À implémenter
    # await send_sms(
    #     phone,
    #     f"Votre ticket {ticket_number} a été payé: {amount} HTG. "
    #     f"Merci d'avoir joué chez Parier Keno Haïti!"
    # )
    pass

