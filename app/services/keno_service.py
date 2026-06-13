# app/services/keno_service.py
"""Service complet pour le jeu Keno"""

import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Tuple, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, update
import redis.asyncio as redis

from app.core.exceptions import AppException, GameException, InsufficientBalanceException
from app.core.logger import get_logger
from app.models.keno import KenoDraw, KenoBet, KenoDrawStatus, KenoBetStatus
from app.models.user import User
from app.models.ticket import Ticket
from app.models.wallet import Wallet
from app.models.transaction import Transaction, TransactionType, TransactionStatus
from app.services.base import BaseService
from app.services.audit_service import AuditService, AuditAction
from app.services.rng_service import RNGService
from app.services.wallet_service import WalletService
from app.schemas.keno import KenoBetCreate, KenoBetResponse


class KenoService(BaseService[KenoDraw, None, None]):
    """
    Service complet pour le jeu Keno.
    Gère les tirages, les paris, et le règlement.
    """
    
    # Configuration du jeu
    TOTAL_NUMBERS = 80
    DRAWN_COUNT = 20
    MIN_PICKS = 1
    MAX_PICKS = 10
    MIN_BET = Decimal("10")
    MAX_BET = Decimal("100000")
    
    # Table de paiement
    PAYTABLE = {
        1: {1: Decimal("2.5")},
        2: {2: Decimal("6")},
        3: {3: Decimal("12"), 2: Decimal("1.5")},
        4: {4: Decimal("30"), 3: Decimal("3"), 2: Decimal("1")},
        5: {5: Decimal("60"), 4: Decimal("6"), 3: Decimal("2"), 2: Decimal("0.5")},
        6: {6: Decimal("120"), 5: Decimal("15"), 4: Decimal("4"), 3: Decimal("1.5"), 2: Decimal("0.5")},
        7: {7: Decimal("300"), 6: Decimal("30"), 5: Decimal("8"), 4: Decimal("2"), 3: Decimal("1"), 2: Decimal("0.5")},
        8: {8: Decimal("600"), 7: Decimal("60"), 6: Decimal("15"), 5: Decimal("4"), 4: Decimal("1.5"), 3: Decimal("0.5")},
        9: {9: Decimal("1200"), 8: Decimal("120"), 7: Decimal("30"), 6: Decimal("8"), 5: Decimal("3"), 4: Decimal("1")},
        10: {10: Decimal("5000"), 9: Decimal("500"), 8: Decimal("60"), 7: Decimal("15"), 6: Decimal("5"), 5: Decimal("2"), 4: Decimal("0.5")}
    }
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, KenoDraw)
        self.redis = redis_client
        self.rng = RNGService()
        self.audit_service = AuditService(db, redis_client)
        self.wallet_service = WalletService(db, redis_client)
        self.logger = get_logger("KenoService")
    
    # ========== Tirages ==========
    
    async def generate_draw(self, draw_time: Optional[datetime] = None) -> KenoDraw:
        """Génère un nouveau tirage"""
        # Récupérer le dernier numéro de tirage
        last_draw_result = await self.db.execute(
            select(func.max(KenoDraw.draw_number))
        )
        last_number = last_draw_result.scalar() or 0
        
        draw = KenoDraw(
            draw_number=last_number + 1,
            draw_time=draw_time or datetime.utcnow(),
            status=KenoDrawStatus.PENDING
        )
        
        self.db.add(draw)
        await self.db.flush()
        
        self.logger.info(f"Draw generated: #{draw.draw_number} ({draw.id})")
        
        # Planifier le règlement
        await self._schedule_draw_settlement(draw.id)
        
        return draw
    
    async def execute_draw(self, draw_id: str) -> Dict[str, Any]:
        """Exécute un tirage et règle tous les paris"""
        draw = await self.get_or_raise(draw_id)
        
        if draw.status != KenoDrawStatus.PENDING:
            raise GameException(f"Tirage déjà {draw.status}")
        
        # Générer les numéros gagnants
        numbers = self.rng.generate_keno_numbers()
        draw.numbers = numbers
        draw.status = KenoDrawStatus.COMPLETED
        draw.closed_at = datetime.utcnow()
        
        # Récupérer tous les paris en attente
        bets_result = await self.db.execute(
            select(KenoBet).where(
                and_(
                    KenoBet.draw_id == draw_id,
                    KenoBet.status == KenoBetStatus.PENDING
                )
            )
        )
        bets = bets_result.scalars().all()
        
        # Calculer les gains et mettre à jour
        total_payout = Decimal("0")
        winners_count = 0
        
        for bet in bets:
            winnings, hits = self._calculate_winnings(bet.picks, numbers, bet.stake)
            
            bet.hits = hits
            bet.multiplier = self._get_multiplier(len(bet.picks), hits)
            bet.winnings = winnings
            bet.status = KenoBetStatus.WON if winnings > 0 else KenoBetStatus.LOST
            bet.settled_at = datetime.utcnow()
            
            if winnings > 0:
                winners_count += 1
                total_payout += winnings
                
                # Créditer les gains
                if bet.user_id:
                    await self.wallet_service.credit(bet.user_id, winnings, "WIN", bet.id)
                elif bet.ticket_id:
                    await self._credit_ticket(bet.ticket_id, winnings)
        
        draw.total_bets = len(bets)
        draw.total_amount = sum(b.stake for b in bets)
        draw.total_payout = total_payout
        
        await self.db.commit()
        
        # Log audit
        await self.audit_service.log(
            action=AuditAction.DRAW_GENERATED,
            resource_type="keno_draw",
            resource_id=draw.id,
            new_values={
                "draw_number": draw.draw_number,
                "numbers": numbers,
                "total_bets": len(bets),
                "total_payout": float(total_payout)
            }
        )
        
        self.logger.info(f"Draw executed: #{draw.draw_number} - {len(bets)} bets, {total_payout} payout")
        
        return {
            "draw_id": draw.id,
            "draw_number": draw.draw_number,
            "numbers": numbers,
            "total_bets": len(bets),
            "total_payout": float(total_payout),
            "winners_count": winners_count
        }
    
    # ========== Paris ==========
    
    async def place_bet(
        self,
        user_id: str,
        bet_data: KenoBetCreate,
        ip_address: str = None
    ) -> KenoBet:
        """Place un pari pour un utilisateur connecté"""
        # Vérifier le tirage
        draw = await self.get_or_raise(bet_data.draw_id)
        if draw.status != KenoDrawStatus.PENDING:
            raise GameException("Ce tirage n'est plus disponible")
        
        if draw.draw_time < datetime.utcnow():
            raise GameException("Ce tirage est déjà passé")
        
        # Vérifier la mise
        stake = Decimal(str(bet_data.stake))
        if stake < self.MIN_BET or stake > self.MAX_BET:
            raise GameException(f"Mise invalide. Min: {self.MIN_BET}, Max: {self.MAX_BET}")
        
        if not (self.MIN_PICKS <= len(bet_data.picks) <= self.MAX_PICKS):
            raise GameException(f"Nombre de numéros invalide. Min: {self.MIN_PICKS}, Max: {self.MAX_PICKS}")
        
        # Vérifier le solde
        wallet = await self.wallet_service.get_by_user_id(user_id)
        if not wallet or wallet.balance < stake:
            raise InsufficientBalanceException(stake, wallet.balance if wallet else 0)
        
        # Débiter le wallet
        await self.wallet_service.debit(user_id, stake, "BET")
        
        # Créer le pari
        bet = KenoBet(
            user_id=user_id,
            draw_id=draw.id,
            picks=bet_data.picks,
            stake=stake,
            status=KenoBetStatus.PENDING,
            placed_at=datetime.utcnow()
        )
        
        self.db.add(bet)
        await self.db.flush()
        
        # Log audit
        await self.audit_service.log(
            user_id=user_id,
            action=AuditAction.BET_PLACED,
            resource_type="keno_bet",
            resource_id=bet.id,
            ip_address=ip_address,
            new_values={"stake": float(stake), "picks": bet_data.picks}
        )
        
        # Mettre à jour les statistiques utilisateur
        await self._update_user_stats(user_id, stake, is_bet=True)
        
        self.logger.info(f"Bet placed: user={user_id}, stake={stake}, draw={draw.draw_number}")
        
        return bet
    
    async def place_bet_with_ticket(
        self,
        ticket_number: str,
        draw_id: str,
        picks: List[int],
        stake: Decimal,
        agent_id: str
    ) -> KenoBet:
        """Place un pari avec un ticket (bureau)"""
        from app.services.ticket_service import TicketService
        
        ticket_service = TicketService(self.db, self.redis)
        ticket = await ticket_service.get_by_number(ticket_number)
        
        if not ticket:
            raise GameException("Ticket invalide")
        
        if ticket.balance < stake:
            raise GameException("Solde ticket insuffisant")
        
        # Vérifier le tirage
        draw = await self.get_or_raise(draw_id)
        if draw.status != KenoDrawStatus.PENDING:
            raise GameException("Ce tirage n'est plus disponible")
        
        # Débiter le ticket
        ticket.balance -= stake
        await self.db.flush()
        
        # Créer le pari
        bet = KenoBet(
            ticket_id=ticket.id,
            draw_id=draw.id,
            picks=picks,
            stake=stake,
            agent_id=agent_id,
            status=KenoBetStatus.PENDING,
            placed_at=datetime.utcnow()
        )
        
        self.db.add(bet)
        await self.db.flush()
        
        await self.audit_service.log(
            agent_id=agent_id,
            action=AuditAction.BET_PLACED,
            resource_type="keno_bet",
            resource_id=bet.id,
            new_values={"stake": float(stake), "ticket": ticket_number}
        )
        
        self.logger.info(f"Bet placed with ticket: {ticket_number}, stake={stake}")
        
        return bet
    
    # ========== Calculs ==========
    
    def _calculate_winnings(
        self,
        picks: List[int],
        draw_numbers: List[int],
        stake: Decimal
    ) -> Tuple[Decimal, int]:
        """Calcule les gains en fonction des numéros tirés"""
        hits = len(set(picks) & set(draw_numbers))
        multiplier = self._get_multiplier(len(picks), hits)
        winnings = stake * multiplier
        return winnings, hits
    
    def _get_multiplier(self, picks_count: int, hits: int) -> Decimal:
        """Récupère le multiplicateur pour un nombre de picks et hits"""
        return self.PAYTABLE.get(picks_count, {}).get(hits, Decimal("0"))
    
    # ========== Utilitaires ==========
    
    async def get_user_bets(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[KenoBet]:
        """Récupère l'historique des paris d'un utilisateur"""
        result = await self.db.execute(
            select(KenoBet)
            .where(KenoBet.user_id == user_id)
            .order_by(KenoBet.placed_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def get_upcoming_draws(self, limit: int = 5) -> List[KenoDraw]:
        """Récupère les prochains tirages"""
        result = await self.db.execute(
            select(KenoDraw)
            .where(
                and_(
                    KenoDraw.draw_time > datetime.utcnow(),
                    KenoDraw.status == KenoDrawStatus.PENDING
                )
            )
            .order_by(KenoDraw.draw_time)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def get_last_results(self, limit: int = 10) -> List[KenoDraw]:
        """Récupère les derniers résultats"""
        result = await self.db.execute(
            select(KenoDraw)
            .where(KenoDraw.status == KenoDrawStatus.COMPLETED)
            .order_by(KenoDraw.draw_time.desc())
            .limit(limit)
        )
        return result.scalars().all()
    
    async def _update_user_stats(self, user_id: str, stake: Decimal, is_bet: bool = True) -> None:
        """Met à jour les statistiques utilisateur"""
        user = await self.db.get(User, user_id)
        if user:
            if is_bet:
                user.total_bets_count += 1
                user.total_bets_amount += stake
            else:
                user.total_wins += stake
            await self.db.flush()
    
    async def _credit_ticket(self, ticket_id: str, amount: Decimal) -> None:
        """Crédite un ticket (gains)"""
        ticket = await self.db.get(Ticket, ticket_id)
        if ticket:
            ticket.balance += amount
            await self.db.flush()
    
    async def _schedule_draw_settlement(self, draw_id: str) -> None:
        """Planifie le règlement du tirage"""
        # Implémentation avec Celery ou asyncio
        # Pour l'instant, on règle immédiatement
        pass
    
    async def get_statistics(self, user_id: str) -> Dict[str, Any]:
        """Récupère les statistiques Keno d'un utilisateur"""
        result = await self.db.execute(
            select(
                func.count(KenoBet.id).label("total_bets"),
                func.sum(KenoBet.stake).label("total_stake"),
                func.sum(KenoBet.winnings).label("total_winnings"),
                func.count().filter(KenoBet.winnings > 0).label("wins")
            ).where(KenoBet.user_id == user_id)
        )
        stats = result.one()
        
        total_bets = stats.total_bets or 0
        total_wins = stats.wins or 0
        
        return {
            "total_bets": total_bets,
            "total_stake": float(stats.total_stake or 0),
            "total_winnings": float(stats.total_winnings or 0),
            "win_rate": round((total_wins / total_bets * 100) if total_bets > 0 else 0, 2),
            "net_result": float((stats.total_winnings or 0) - (stats.total_stake or 0)),
            "best_win": float(await self._get_best_win(user_id))
        }
    
    async def _get_best_win(self, user_id: str) -> Decimal:
        """Récupère le meilleur gain d'un utilisateur"""
        result = await self.db.execute(
            select(func.max(KenoBet.winnings))
            .where(KenoBet.user_id == user_id)
        )
        return result.scalar() or Decimal("0")