# app/services/ticket_service.py
"""Service pour la gestion des tickets (jeu sans compte)"""

import secrets
import string
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
import redis.asyncio as redis
import qrcode
from io import BytesIO
import base64

from app.core.exceptions import AppException, NotFoundException
from app.core.logger import get_logger
from app.core.timezone import to_haiti
from app.models.ticket import Ticket, TicketStatus
from app.models.bureau import Bureau
from app.services.base import BaseService
from app.services.audit_service import AuditService, AuditAction
from app.schemas.ticket import TicketCreate, TicketResponse


class TicketService(BaseService[Ticket, TicketCreate, None]):
    """
    Service pour la gestion des tickets.
    Permet aux joueurs sans compte de jouer au bureau.
    """
    
    # Un ticket se paie LE JOUR MÊME : il expire à minuit (heure d'Haïti).
    # (Avant : 7 jours.) Un ticket dont un pari attend son tirage n'expire pas ;
    # un gain qui tombe après minuit le rend payable jusqu'à la fin de ce jour.
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, Ticket)
        self.redis = redis_client
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger("TicketService")
    
    @staticmethod
    def generate_ticket_number() -> str:
        """Génère un numéro de ticket unique"""
        prefix = "KNO"
        random_part = ''.join(
            secrets.choice(string.ascii_uppercase + string.digits) 
            for _ in range(8)
        )
        return f"{prefix}-{random_part[:4]}-{random_part[4:]}"
    
    async def create_ticket(
        self,
        agent_id: str,
        bureau_id: str,
        amount: Decimal,
        player_name: str = None,
        player_phone: str = None
    ) -> Dict[str, Any]:
        """Crée un nouveau ticket après encaissement cash"""
        
        # Vérifier le bureau
        bureau = await self.db.get(Bureau, bureau_id)
        if not bureau:
            raise NotFoundException("Bureau", bureau_id)
        
        # Créer le ticket
        ticket = Ticket(
            ticket_number=self.generate_ticket_number(),
            bureau_id=bureau_id,
            agent_id=agent_id,
            player_name=player_name,
            player_phone=player_phone,
            balance=amount,
            initial_amount=amount,
            expires_at=Ticket.end_of_local_day(),
            status=TicketStatus.ACTIVE
        )
        
        self.db.add(ticket)
        
        # Mettre à jour la caisse du bureau
        bureau.cash_balance = Bureau.cash_balance + (amount)  # atomique en base
        bureau.total_cash_in_today = Bureau.total_cash_in_today + (amount)  # atomique en base
        
        await self.db.flush()
        
        # Générer QR code
        qr_base64 = self._generate_qr_code(ticket.ticket_number)
        
        # Audit log
        await self.audit_service.log(
            agent_id=agent_id,
            action=AuditAction.DEPOSIT,
            resource_type="ticket",
            resource_id=ticket.id,
            new_values={
                "ticket_number": ticket.ticket_number,
                "amount": float(amount),
                "bureau_id": bureau_id
            }
        )
        
        self.logger.info(f"Ticket created: {ticket.ticket_number} for {amount} HTG")
        
        return {
            "id": ticket.id,
            "ticket_number": ticket.ticket_number,
            "balance": float(ticket.balance),
            "initial_amount": float(ticket.initial_amount),
            "expires_at": ticket.expires_at,
            "qr_code": qr_base64,
            "status": ticket.status
        }
    
    async def get_by_number(self, ticket_number: str) -> Optional[Ticket]:
        """Récupère un ticket par son numéro"""
        result = await self.db.execute(
            select(Ticket).where(Ticket.ticket_number == ticket_number)
        )
        return result.scalar_one_or_none()
    
    async def get_or_raise_by_number(self, ticket_number: str) -> Ticket:
        """Récupère un ticket par son numéro ou lève une exception"""
        ticket = await self.get_by_number(ticket_number)
        if not ticket:
            raise NotFoundException("Ticket", ticket_number)
        return ticket
    
    async def pending_bets_count(self, ticket_id: str) -> int:
        """Paris de ce ticket dont le résultat n'est pas encore connu
        (tirage Keno, manche Lucky6 ou course pas encore réglés)."""
        from app.models.enums import KenoBetStatus
        from app.models.game import GameBet
        from app.models.keno import KenoBet

        keno = (await self.db.execute(
            select(func.count(KenoBet.id)).where(KenoBet.ticket_id == ticket_id, KenoBet.status == KenoBetStatus.PENDING)
        )).scalar() or 0
        games = (await self.db.execute(
            select(func.count(GameBet.id)).where(GameBet.ticket_id == ticket_id, GameBet.status == "PENDING")
        )).scalar() or 0
        return int(keno) + int(games)

    async def get_for_update(self, ticket_number: str) -> Ticket:
        """Ticket verrouillé (SELECT … FOR UPDATE) : deux paiements simultanés
        du même ticket se suivent, le second voit le ticket déjà payé."""
        result = await self.db.execute(
            select(Ticket).where(Ticket.ticket_number == (ticket_number or "").strip().upper()).with_for_update()
        )
        ticket = result.scalar_one_or_none()
        if not ticket:
            raise NotFoundException("Ticket", ticket_number)
        return ticket

    async def payout_ticket(
        self,
        ticket_number: str,
        agent_id: str,
        bureau_id: str = None,
        session_id: str = None,
    ) -> Dict[str, Any]:
        """Paiement cash d'un ticket (solde complet : gains + reste éventuel).

        Sécurités : ticket verrouillé, une seule fois (statut PAID), pas de
        paiement tant qu'un pari du ticket attend son résultat, bureau du
        ticket, montant payé enregistré (tickets.paid_amount)."""

        ticket = await self.get_for_update(ticket_number)

        # Vérifications
        if ticket.status == TicketStatus.PAID:
            when = to_haiti(ticket.paid_at).strftime("%d/%m/%Y %H:%M") if ticket.paid_at else ""
            raise AppException(400, f"Ticket déjà payé{(' le ' + when) if when else ''}", "ALREADY_PAID")
        if ticket.status != TicketStatus.ACTIVE:
            raise AppException(400, f"Ticket {getattr(ticket.status, 'value', ticket.status)} : paiement impossible")

        # Vérifier le bureau si spécifié
        if bureau_id and ticket.bureau_id != bureau_id:
            raise AppException(400, "Ce ticket a été vendu dans un autre bureau : il se paie dans ce bureau")

        pending = await self.pending_bets_count(ticket.id)
        if pending:  # avant l'expiration : le gain éventuel rendra le ticket payable le jour du résultat
            raise AppException(400, f"Résultat pas encore connu pour {pending} pari(s) de ce ticket : paiement après le tirage", "PENDING_BETS")

        if ticket.expires_at < datetime.utcnow():
            ticket.status = TicketStatus.EXPIRED
            await self.db.flush()
            raise AppException(400, "Ticket expiré : un ticket se paie le jour même")

        if ticket.balance <= 0:
            raise AppException(400, "Aucun gain à payer sur ce ticket", "NOTHING_TO_PAY")

        amount = ticket.balance

        # Effectuer le paiement
        ticket.record_payout(amount, agent_id, session_id)  # solde -> 0, PAYÉ, paiement daté

        # Mettre à jour la caisse du bureau
        bureau = await self.db.get(Bureau, ticket.bureau_id)
        if bureau:
            bureau.cash_balance = Bureau.cash_balance - (amount)  # atomique en base
            bureau.total_cash_out_today = Bureau.total_cash_out_today + (amount)  # atomique en base
        
        await self.db.flush()
        
        # Audit log
        await self.audit_service.log(
            agent_id=agent_id,
            action=AuditAction.WITHDRAWAL,
            resource_type="ticket",
            resource_id=ticket.id,
            new_values={
                "ticket_number": ticket.ticket_number,
                "amount": float(amount),
                "paid_at": ticket.paid_at.isoformat()
            }
        )
        
        self.logger.info(f"Ticket payout: {ticket_number} for {amount} HTG")
        
        return {
            "success": True,
            "amount": float(amount),
            "ticket_number": ticket.ticket_number,
            "message": f"Paiement de {amount} HTG effectué"
        }
    
    async def get_active_tickets_for_bureau(
        self,
        bureau_id: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[Ticket]:
        """Récupère les tickets actifs d'un bureau"""
        result = await self.db.execute(
            select(Ticket)
            .where(
                and_(
                    Ticket.bureau_id == bureau_id,
                    Ticket.status == TicketStatus.ACTIVE,
                    Ticket.expires_at > datetime.utcnow()
                )
            )
            .order_by(Ticket.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def get_bureau_stats(self, bureau_id: str) -> Dict[str, Any]:
        """Récupère les statistiques d'un bureau"""
        
        # Tickets actifs
        active_tickets = await self.get_active_tickets_for_bureau(bureau_id)
        total_active_balance = sum(t.balance for t in active_tickets)
        
        # Tickets aujourd'hui
        today_start = datetime.combine(datetime.utcnow().date(), datetime.min.time())
        result = await self.db.execute(
            select(
                func.count(Ticket.id).label("total_tickets"),
                func.sum(Ticket.initial_amount).label("total_created"),
                func.sum(Ticket.balance).label("total_balance")
            )
            .where(
                and_(
                    Ticket.bureau_id == bureau_id,
                    Ticket.created_at >= today_start
                )
            )
        )
        stats = result.one()
        
        # Paiements aujourd'hui
        from app.models.cash_movement import KIND_PAYOUT, TicketCashMovement

        payout_result = await self.db.execute(  # chaque paiement daté, partiels compris
            select(
                func.count(TicketCashMovement.id).label("total_payouts"),
                func.sum(TicketCashMovement.amount).label("total_paid")
            )
            .where(
                and_(
                    TicketCashMovement.bureau_id == bureau_id,
                    TicketCashMovement.created_at >= today_start,
                    TicketCashMovement.kind == KIND_PAYOUT,
                )
            )
        )
        payout_stats = payout_result.one()
        
        return {
            "active_tickets_count": len(active_tickets),
            "active_tickets_balance": float(total_active_balance),
            "today_created_count": stats.total_tickets or 0,
            "today_created_amount": float(stats.total_created or 0),
            "today_payouts_count": payout_stats.total_payouts or 0,
            "today_payouts_amount": float(payout_stats.total_paid or 0),
            "total_outstanding_balance": float(stats.total_balance or 0)
        }
    
    async def expire_old_tickets(self) -> int:
        """Expire les tickets arrivés à expiration"""
        result = await self.db.execute(
            select(Ticket).where(
                and_(
                    Ticket.status == TicketStatus.ACTIVE,
                    Ticket.expires_at < datetime.utcnow(),
                    Ticket.no_pending_bet_clause(),  # résultat encore attendu : pas d'expiration
                )
            )
        )
        expired_tickets = result.scalars().all()
        
        for ticket in expired_tickets:
            ticket.status = TicketStatus.EXPIRED
            self.logger.info(f"Ticket expired: {ticket.ticket_number}")
        
        await self.db.flush()
        
        return len(expired_tickets)
    
    def _generate_qr_code(self, ticket_number: str) -> str:
        """Génère un QR code en base64 pour impression"""
        qr = qrcode.QRCode(box_size=4, border=2)
        qr.add_data(ticket_number)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        
        return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}"