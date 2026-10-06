# app/services/payout_service.py
"""Paiement des gains au guichet.

L'agent scanne (ou tape) le numéro du ticket imprimé sur le reçu : le serveur
montre tous les paris de ce ticket (Keno, Lucky6, Horse Races, et l'historique
Lucky Wheel), ce qui est gagné, ce qui attend encore son tirage, puis l'agent
paie le joueur en espèces.

Règles (serveur) :
- ticket verrouillé pendant le paiement : jamais payé deux fois ;
- pas de paiement tant qu'un pari du ticket attend son résultat ;
- le ticket se paie dans le bureau qui l'a vendu ;
- caisse ouverte et suffisamment approvisionnée ;
- montant payé enregistré (tickets.paid_amount), sortie de caisse comptée
  dans la session de l'agent, journal d'audit.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional

import redis.asyncio as redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, NotFoundException, ValidationException
from app.core.timezone import now_utc, to_haiti
from app.models.bureau import Bureau, CashierSession
from app.models.game import GameBet, GameRound
from app.models.keno import KenoBet, KenoDraw
from app.models.ticket import Ticket
from app.models.user import User
from app.services.ticket_service import TicketService

GAME_LABELS = {"lucky6": "Lucky6", "horse_races": "Horse Races"}
BET_LABELS = {
    "SIX": "6 numéros", "FIRST_ODD": "1re boule impaire", "FIRST_EVEN": "1re boule paire",
    "WIN": "Gagnant", "PLACE": "Placé", "EXACTA": "Exacta", "TRIFECTA": "Trifecta",
}
STATUS_LABELS = {"pending": "En attente", "won": "Gagné", "lost": "Perdu", "refunded": "Remboursé", "void": "Annulé"}


def _iso(value) -> Optional[str]:
    return value.isoformat() + "Z" if value else None


def _status(value) -> str:
    return str(getattr(value, "value", value) or "").lower()


class PayoutService:
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client
        self.tickets = TicketService(db, redis_client)

    async def _bets(self, ticket: Ticket) -> List[Dict[str, Any]]:
        bets: List[Dict[str, Any]] = []
        rows = (await self.db.execute(
            select(KenoBet, KenoDraw).join(KenoDraw, KenoDraw.id == KenoBet.draw_id)
            .where(KenoBet.ticket_id == ticket.id).order_by(KenoBet.placed_at)
        )).all()
        for bet, draw in rows:
            bets.append({
                "game": "Keno", "round": f"Tirage n° {draw.draw_number}", "bet": f"{len(bet.picks)} numéro(s)",
                "selection": " - ".join(str(n) for n in bet.picks), "stake": float(bet.stake),
                "status": _status(bet.status), "winnings": float(bet.winnings or 0),
                "detail": f"{bet.hits or 0}/{len(bet.picks)} trouvé(s)" if _status(bet.status) in ("won", "lost") else "",
                "placed_at": _iso(bet.placed_at),
            })
        rows = (await self.db.execute(
            select(GameBet, GameRound).join(GameRound, GameRound.id == GameBet.round_id)
            .where(GameBet.ticket_id == ticket.id).order_by(GameBet.placed_at)
        )).all()
        for bet, rnd in rows:
            game = GAME_LABELS.get(bet.game_type, bet.game_type)
            bets.append({
                "game": game,
                "round": f"{'Manche' if bet.game_type == 'lucky6' else 'Course'} n° {rnd.round_number}",
                "bet": BET_LABELS.get(bet.bet_type, bet.bet_type),
                "selection": " - ".join(str(n) for n in (bet.selection or [])),
                "stake": float(bet.stake), "status": _status(bet.status), "winnings": float(bet.winnings or 0),
                "detail": "", "placed_at": _iso(bet.placed_at),
            })
        try:  # historique : Lucky Wheel retirée du projet, parties passées conservées
            from app.models.lucky import LuckyPlay

            plays = (await self.db.execute(
                select(LuckyPlay).where(LuckyPlay.ticket_id == ticket.id).order_by(LuckyPlay.played_at)
            )).scalars().all()
            for p in plays:
                bets.append({
                    "game": "Lucky Wheel (historique)", "round": "", "bet": (p.result_segment or {}).get("label", ""),
                    "selection": "", "stake": float(p.stake), "status": "won" if (p.winnings or 0) > 0 else "lost",
                    "winnings": float(p.winnings or 0), "detail": "", "placed_at": _iso(p.played_at),
                })
        except Exception:
            pass
        for b in bets:
            b["status_label"] = STATUS_LABELS.get(b["status"], b["status"])
        return bets

    async def summary(self, ticket_number: str, agent: User) -> Dict[str, Any]:
        """Tout ce qu'il faut pour payer (ou refuser) un ticket, sans rien modifier."""
        number = (ticket_number or "").strip().upper()
        if not number:
            raise ValidationException("Numéro de ticket requis")
        ticket = (await self.db.execute(select(Ticket).where(Ticket.ticket_number == number))).scalar_one_or_none()
        if ticket is None:
            raise NotFoundException("Ticket", number)
        bets = await self._bets(ticket)
        pending = sum(1 for b in bets if b["status"] == "pending")
        bureau = await self.db.get(Bureau, ticket.bureau_id)
        paid_by = await self.db.get(User, ticket.paid_by_agent) if ticket.paid_by_agent else None
        status = _status(ticket.status)
        reason = None
        if status == "paid":
            reason = "Ticket déjà payé" + (f" le {to_haiti(ticket.paid_at).strftime('%d/%m/%Y à %H:%M')}" if ticket.paid_at else "") \
                + (f" par {paid_by.full_name or paid_by.phone}" if paid_by else "")
        elif status != "active":
            reason = f"Ticket {status} : paiement impossible"
        elif agent.bureau_id and ticket.bureau_id != agent.bureau_id:
            reason = f"Ticket vendu au bureau « {bureau.name if bureau else '?'} » : il se paie dans ce bureau"
        elif pending:
            reason = f"Résultat pas encore connu pour {pending} pari(s) : paiement après le tirage"
        elif ticket.expires_at and ticket.expires_at < now_utc():
            reason = "Ticket expiré : un ticket se paie le jour même (avant minuit)"
        elif (ticket.balance or 0) <= 0:
            reason = "Aucun gain à payer sur ce ticket"
        return {
            "ticket_number": ticket.ticket_number,
            "status": status,
            "bureau": bureau.name if bureau else None,
            "created_at": _iso(ticket.created_at),
            "expires_at": _iso(ticket.expires_at),
            "initial_amount": float(ticket.initial_amount or 0),
            "balance": float(ticket.balance or 0),
            "paid_amount": float(ticket.amount_paid or 0),
            "paid_at": _iso(ticket.paid_at),
            "total_stake": sum(b["stake"] for b in bets),
            "total_winnings": sum(b["winnings"] for b in bets),
            "pending": pending,
            "bets": bets,
            "can_pay": reason is None,
            "reason": reason,
            "amount_to_pay": float(ticket.balance or 0) if reason is None else 0.0,
        }

    async def pay(self, ticket_number: str, agent: User, ip_address: Optional[str] = None) -> Dict[str, Any]:
        """Paie le solde du ticket en espèces. La transaction est validée par l'appelant."""
        if not agent.bureau_id:
            raise ValidationException("Agent non affecté à un bureau")
        session = (await self.db.execute(
            select(CashierSession)
            .where(and_(CashierSession.agent_id == agent.id, CashierSession.status == "OPEN"))
            .with_for_update()
        )).scalar_one_or_none()
        if session is None:
            raise ValidationException("Ouvrez votre session de caisse (menu Caisse) avant de payer des gains")

        ticket = await self.tickets.get_for_update(ticket_number)
        amount = Decimal(ticket.balance or 0)
        if amount > 0 and Decimal(session.current_balance or 0) < amount:
            raise AppException(
                400, f"Caisse insuffisante : {Decimal(session.current_balance or 0):.2f} HTG disponibles pour payer {amount:.2f} HTG",
                "CASH_INSUFFICIENT",
            )
        result = await self.tickets.payout_ticket(ticket.ticket_number, agent_id=agent.id, bureau_id=agent.bureau_id,
                                                  session_id=session.id)
        paid = Decimal(str(result["amount"]))
        session.cash_out_count += 1
        session.cash_out_amount += paid
        session.current_balance -= paid
        await self.db.flush()
        summary = await self.summary(ticket.ticket_number, agent)
        return {**summary, "paid_now": float(paid), "paid_by": agent.full_name or agent.phone,
                "cash_balance": float(session.current_balance)}
