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
    
    TICKET_EXPIRY_DAYS = 7
    
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
            expires_at=datetime.utcnow() + timedelta(days=self.TICKET_EXPIRY_DAYS),
            status=TicketStatus.ACTIVE
        )
        
        self.db.add(ticket)
        
        # Mettre à jour la caisse du bureau
        bureau.cash_balance += amount
        bureau.total_cash_in_today += amount
        
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
    
    async def payout_ticket(
        self,
        ticket_number: str,
        agent_id: str,
        bureau_id: str = None
    ) -> Dict[str, Any]:
        """Paiement cash d'un ticket"""
        
        ticket = await self.get_or_raise_by_number(ticket_number)
        
        # Vérifications
        if ticket.status != TicketStatus.ACTIVE:
            raise AppException(400, f"Ticket déjà {ticket.status}")
        
        if ticket.balance <= 0:
            raise AppException(400, "Aucun solde à payer")
        
        if ticket.expires_at < datetime.utcnow():
            ticket.status = TicketStatus.EXPIRED
            await self.db.flush()
            raise AppException(400, "Ticket expiré")
        
        # Vérifier le bureau si spécifié
        if bureau_id and ticket.bureau_id != bureau_id:
            raise AppException(400, "Ce ticket n'appartient pas à ce bureau")
        
        amount = ticket.balance
        
        # Effectuer le paiement
        ticket.status = TicketStatus.PAID
        ticket.paid_at = datetime.utcnow()
        ticket.paid_by_agent = agent_id
        ticket.balance = Decimal("0")
        
        # Mettre à jour la caisse du bureau
        bureau = await self.db.get(Bureau, ticket.bureau_id)
        if bureau:
            bureau.cash_balance -= amount
            bureau.total_cash_out_today += amount
        
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
        payout_result = await self.db.execute(
            select(
                func.count(Ticket.id).label("total_payouts"),
                func.sum(Ticket.initial_amount).label("total_paid")
            )
            .where(
                and_(
                    Ticket.bureau_id == bureau_id,
                    Ticket.paid_at >= today_start,
                    Ticket.status == TicketStatus.PAID
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
                    Ticket.expires_at < datetime.utcnow()
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