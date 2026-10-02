# app/services/cash_ticket.py
"""Vente au comptant : le numéro de ticket est créé AUTOMATIQUEMENT.

Le joueur paie en espèces au guichet ; le serveur crée un ticket bureau
(TicketService, numéro KNO-XXXX-XXXX généré) d'un montant égal à la mise,
l'enregistre dans la caisse de l'agent, puis le pari est financé par ce
ticket. Le numéro est imprimé sur le reçu : il sert à suivre le pari et
à encaisser un gain (paiement des tickets, inchangé).

Tout se fait dans la transaction de l'appelant : si le pari est refusé,
rien n'est créé ni encaissé (rollback).
"""

from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import redis.asyncio as redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationException
from app.models.bureau import CashierSession
from app.models.user import User
from app.services.ticket_service import TicketService


async def sell_cash_ticket(
    db: AsyncSession,
    redis_client: redis.Redis,
    agent: User,
    amount: Any,
    player_name: Optional[str] = None,
) -> str:
    """Crée le ticket du montant encaissé et renvoie son numéro."""
    if not agent.bureau_id:
        raise ValidationException("Agent non affecté à un bureau")
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationException("Mise invalide")
    if not value.is_finite() or value <= 0:
        raise ValidationException("Mise invalide")

    session = (await db.execute(
        select(CashierSession)
        .where(and_(CashierSession.agent_id == agent.id, CashierSession.status == "OPEN"))
        .with_for_update()
    )).scalar_one_or_none()
    if session is None:
        raise ValidationException("Ouvrez votre session de caisse (menu Caisse) avant de vendre des tickets")

    ticket = await TicketService(db, redis_client).create_ticket(
        agent_id=agent.id,
        bureau_id=agent.bureau_id,
        amount=value,
        player_name=(player_name or None),
    )
    session.cash_in_count += 1
    session.cash_in_amount += value
    session.current_balance += value
    await db.flush()
    return ticket["ticket_number"]


async def resolve_funding(
    db: AsyncSession,
    redis_client: redis.Redis,
    agent: User,
    player_type: Optional[str],
    identifier: Optional[str],
    stake: Any,
    player_name: Optional[str] = None,
) -> tuple:
    """Qui paie le pari ? Renvoie (user_id, ticket_number).

    - « cash » (par défaut) : espèces, ticket créé automatiquement ;
    - « ticket » : ticket existant (son solde) ;
    - « account » : compte joueur (téléphone)."""
    from app.core.exceptions import NotFoundException
    from app.services.user_service import UserService

    player_type = player_type or "cash"
    identifier = (identifier or "").strip()
    if player_type == "cash":
        return None, await sell_cash_ticket(db, redis_client, agent, stake, player_name)
    if player_type == "ticket":
        if not identifier:
            raise ValidationException("Numéro de ticket requis")
        return None, identifier.upper()
    if player_type == "account":
        if not identifier:
            raise ValidationException("Téléphone du joueur requis")
        user = await UserService(db, redis_client).get_by_phone(identifier)
        if not user:
            raise NotFoundException("Joueur", identifier)
        return user.id, None
    raise ValidationException("Type de joueur invalide")


async def ticket_numbers(db: AsyncSession, ticket_ids) -> dict:
    """{ticket_id: numéro} pour afficher le numéro de ticket dans les historiques
    (retrouver quel ticket a gagné quand il y a plusieurs gagnants)."""
    from app.models.ticket import Ticket

    ids = [i for i in set(ticket_ids or []) if i]
    if not ids:
        return {}
    rows = await db.execute(select(Ticket.id, Ticket.ticket_number).where(Ticket.id.in_(ids)))
    return {tid: number for tid, number in rows.all()}
