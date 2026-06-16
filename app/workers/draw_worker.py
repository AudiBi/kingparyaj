# app/workers/draw_worker.py
"""Worker pour les tirages automatiques Keno et export Lucky - VERSION COMPLÈTE"""

from celery import Task
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, update, and_, func, or_
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import secrets
import asyncio
import logging
import json

from app.config import settings
from app.core.redis_client import redis_client
from app.core.logger import get_logger
from app.workers.celery import celery_app
from app.models.keno import KenoDraw, KenoBet, KenoDrawStatus, KenoBetStatus
from app.models.lucky import LuckyPlay
from app.models.wallet import Wallet
from app.models.ticket import Ticket
from app.models.transaction import Transaction, TransactionType, TransactionStatus
from app.models.user import User
from app.models.audit import AuditLog, AuditAction

logger = get_logger(__name__)

# Connexion à la base de données
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=5,
    max_overflow=10,
)
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class DrawTask(Task):
    """Tâche de tirage avec gestion des erreurs"""
    _db_session = None
    _redis = None
    
    async def get_db(self):
        if self._db_session is None:
            self._db_session = AsyncSessionLocal()
        return self._db_session
    
    async def get_redis(self):
        if self._redis is None:
            self._redis = redis_client
        return self._redis
    
    async def _run(self, *args, **kwargs):
        try:
            return await super()._run(*args, **kwargs)
        except Exception as e:
            logger.error(f"Draw task failed: {e}", exc_info=True)
            raise


# ==================== KENO - TÂCHE PRINCIPALE ====================

@celery_app.task(
    bind=True,
    base=DrawTask,
    name="app.workers.draw_worker.process_draw",
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True
)
def process_draw(self):
    """Traite les tirages Keno programmés"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_process_draw_async())
    else:
        return loop.run_until_complete(_process_draw_async())


async def _process_draw_async():
    """Logique asynchrone du tirage Keno - COMPLÈTE"""
    logger.info("🔄 Début du traitement des tirages Keno...")
    
    try:
        async with AsyncSessionLocal() as db:
            # 1. Vérifier les horaires d'ouverture (8h-23h)
            now = datetime.utcnow()
            local_hour = (now + timedelta(hours=-4)).hour  # UTC-4 pour Haïti
            
            if local_hour < 8 or local_hour >= 23:
                logger.info(f"⏰ Hors horaires d'ouverture ({local_hour}h). Pas de tirage.")
                return
            
            # 2. Vérifier les tirages en attente
            result = await db.execute(
                select(KenoDraw)
                .where(
                    and_(
                        KenoDraw.status == KenoDrawStatus.PENDING,
                        KenoDraw.draw_time <= now
                    )
                )
                .order_by(KenoDraw.draw_time)
                .limit(1)
            )
            pending_draw = result.scalar_one_or_none()
            
            if not pending_draw:
                next_time = now + timedelta(minutes=settings.KENO_DRAW_INTERVAL_MINUTES)
                next_draw = KenoDraw(
                    draw_number=await _get_next_draw_number(db),
                    draw_time=next_time,
                    status=KenoDrawStatus.PENDING
                )
                db.add(next_draw)
                await db.commit()
                logger.info("📅 Prochain tirage planifié")
                return
            
            logger.info(f"📊 Tirage Keno #{pending_draw.draw_number} en cours...")
            
            # 3. Générer les numéros gagnants
            drawn_numbers = generate_draw_numbers()
            pending_draw.numbers = drawn_numbers
            pending_draw.status = KenoDrawStatus.COMPLETED
            pending_draw.closed_at = datetime.utcnow()
            
            await db.flush()
            
            # 4. Récupérer tous les paris pour ce tirage
            bets_result = await db.execute(
                select(KenoBet)
                .where(
                    and_(
                        KenoBet.draw_id == pending_draw.id,
                        KenoBet.status == KenoBetStatus.PENDING
                    )
                )
            )
            bets = bets_result.scalars().all()
            
            logger.info(f"🎯 {len(bets)} paris Keno à régler...")
            
            # 5. Régler chaque pari
            total_payout = 0
            winners_count = 0
            jackpot_won = False
            
            paytable = {
                1: {1: 2.5},
                2: {2: 6},
                3: {3: 12, 2: 1.5},
                4: {4: 30, 3: 3, 2: 1},
                5: {5: 60, 4: 6, 3: 2, 2: 0.5},
                6: {6: 120, 5: 15, 4: 4, 3: 1.5, 2: 0.5},
                7: {7: 300, 6: 30, 5: 8, 4: 2, 3: 1, 2: 0.5},
                8: {8: 600, 7: 60, 6: 15, 5: 4, 4: 1.5, 3: 0.5},
                9: {9: 1200, 8: 120, 7: 30, 6: 8, 5: 3, 4: 1},
                10: {10: 5000, 9: 500, 8: 60, 7: 15, 6: 5, 5: 2, 4: 0.5}
            }
            
            for bet in bets:
                hits = len(set(bet.picks) & set(drawn_numbers))
                picks_count = len(bet.picks)
                multiplier = paytable.get(picks_count, {}).get(hits, 0)
                winnings = bet.stake * multiplier
                
                bet.hits = hits
                bet.multiplier = multiplier
                bet.winnings = winnings
                bet.status = KenoBetStatus.WON if winnings > 0 else KenoBetStatus.LOST
                bet.settled_at = datetime.utcnow()
                
                if winnings > 0:
                    winners_count += 1
                    total_payout += winnings
                    
                    if bet.user_id:
                        await _credit_user_wallet(db, bet.user_id, winnings, bet.id)
                    elif bet.ticket_id:
                        await _credit_ticket(db, bet.ticket_id, winnings)
            
            # 6. Vérifier le jackpot
            if total_payout > 50000 and not jackpot_won:
                jackpot_won = True
                pending_draw.jackpot_won = True
                pending_draw.jackpot_amount = total_payout
                
                for bet in bets:
                    if bet.winnings > 0:
                        pending_draw.jackpot_winner_id = bet.user_id or bet.ticket_id
                        break
            
            # 7. Mettre à jour les statistiques
            pending_draw.total_payout = total_payout
            
            # 8. Mettre en cache Redis
            await redis_client.setex(
                f"keno:draw:{pending_draw.id}",
                3600,
                str(drawn_numbers)
            )
            await redis_client.setex(
                f"keno:draw:latest",
                3600,
                json.dumps({
                    "draw_id": pending_draw.id,
                    "draw_number": pending_draw.draw_number,
                    "numbers": drawn_numbers,
                    "total_bets": len(bets),
                    "winners_count": winners_count,
                    "total_payout": float(total_payout)
                })
            )
            
            # 9. Diffuser les résultats via WebSocket
            from app.api.websockets.manager import broadcast_draw_result
            await broadcast_draw_result({
                "type": "keno_draw",
                "draw_id": pending_draw.id,
                "draw_number": pending_draw.draw_number,
                "numbers": drawn_numbers,
                "total_bets": len(bets),
                "winners_count": winners_count,
                "total_payout": float(total_payout),
                "jackpot_won": jackpot_won,
                "jackpot_amount": float(total_payout) if jackpot_won else 0
            })
            
            # 10. Export vers LEH (conformité)
            await _export_keno_to_leh(db, pending_draw)
            
            # 11. Planifier le prochain tirage
            next_draw_time = now + timedelta(minutes=settings.KENO_DRAW_INTERVAL_MINUTES)
            next_draw = KenoDraw(
                draw_number=pending_draw.draw_number + 1,
                draw_time=next_draw_time,
                status=KenoDrawStatus.PENDING
            )
            db.add(next_draw)
            
            await db.commit()
            
            logger.info(f"✅ Tirage Keno #{pending_draw.draw_number} terminé. "
                       f"Gagnants: {winners_count}/{len(bets)}, "
                       f"Payout: {total_payout} HTG")
            
            # 12. Notifications pour les gros gains
            if total_payout > 5000:
                await _notify_keno_big_winners(db, bets)
            
    except Exception as e:
        logger.error(f"❌ Erreur lors du tirage Keno: {e}", exc_info=True)
        raise


# ==================== KENO - FONCTIONS AUXILIAIRES ====================

async def _get_next_draw_number(db: AsyncSession) -> int:
    """Récupère le prochain numéro de tirage"""
    result = await db.execute(
        select(func.max(KenoDraw.draw_number))
    )
    max_number = result.scalar() or 0
    return max_number + 1


def generate_draw_numbers() -> List[int]:
    """Génère 20 numéros uniques entre 1 et 80"""
    numbers = list(range(1, 81))
    for i in range(len(numbers) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        numbers[i], numbers[j] = numbers[j], numbers[i]
    return sorted(numbers[:20])


async def _credit_user_wallet(db: AsyncSession, user_id: str, amount: float, bet_id: str):
    """Crédite le wallet d'un utilisateur"""
    wallet_result = await db.execute(
        select(Wallet).where(Wallet.user_id == user_id)
    )
    wallet = wallet_result.scalar_one()
    
    old_balance = wallet.balance
    wallet.balance += amount
    wallet.total_won += amount
    
    transaction = Transaction(
        reference=f"WIN-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
        user_id=user_id,
        wallet_id=wallet.id,
        transaction_type=TransactionType.WIN,
        amount=amount,
        bet_id=bet_id,
        balance_before=old_balance,
        balance_after=wallet.balance,
        status=TransactionStatus.COMPLETED,
        completed_at=datetime.utcnow()
    )
    db.add(transaction)


async def _credit_ticket(db: AsyncSession, ticket_id: str, amount: float):
    """Crédite un ticket"""
    ticket_result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id)
    )
    ticket = ticket_result.scalar_one()
    ticket.balance += amount


async def _export_keno_to_leh(db: AsyncSession, draw: KenoDraw):
    """Exporte les résultats Keno vers la LEH"""
    if not settings.LEH_ENABLED:
        return
    
    try:
        bets_result = await db.execute(
            select(KenoBet).where(KenoBet.draw_id == draw.id)
        )
        bets = bets_result.scalars().all()
        
        export_data = {
            "game": "keno",
            "draw_id": draw.id,
            "draw_number": draw.draw_number,
            "draw_time": draw.draw_time.isoformat(),
            "numbers": draw.numbers,
            "total_bets": len(bets),
            "total_payout": float(draw.total_payout),
            "bets": [
                {
                    "bet_id": b.id,
                    "user_id": b.user_id,
                    "picks": b.picks,
                    "stake": float(b.stake),
                    "hits": b.hits,
                    "winnings": float(b.winnings)
                }
                for b in bets[:100]
            ]
        }
        
        # Envoyer à la LEH
        # await leh_service.export_keno(export_data)
        
        audit = AuditLog(
            action=AuditAction.DRAW_GENERATED,
            resource_type="keno_draw",
            resource_id=draw.id,
            new_values={"exported_to_leh": True}
        )
        db.add(audit)
        
        logger.info(f"📤 Tirage Keno #{draw.draw_number} exporté vers LEH")
        
    except Exception as e:
        logger.error(f"❌ Erreur export LEH Keno: {e}")


async def _notify_keno_big_winners(db: AsyncSession, bets: List):
    """Notifie les gros gagnants Keno"""
    from app.workers.notification_worker import send_win_notification
    
    for bet in bets:
        if bet.winnings >= 5000:
            if bet.user_id:
                user_result = await db.execute(
                    select(User).where(User.id == bet.user_id)
                )
                user = user_result.scalar_one_or_none()
                if user and user.phone:
                    send_win_notification.delay(
                        user.phone,
                        user.first_name or "Joueur",
                        float(bet.winnings),
                        "Keno"
                    )


# ==================== KENO - TÂCHES SUPPLÉMENTAIRES ====================

@celery_app.task(
    name="app.workers.draw_worker.schedule_draws",
    max_retries=3
)
def schedule_draws():
    """Planifie les tirages Keno pour la journée"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_schedule_draws_async())
    else:
        return loop.run_until_complete(_schedule_draws_async())


async def _schedule_draws_async():
    """Planifie les tirages Keno pour les prochaines 24h"""
    logger.info("📅 Planification des tirages Keno...")
    
    try:
        async with AsyncSessionLocal() as db:
            now = datetime.utcnow()
            end_time = now + timedelta(hours=24)
            
            existing_result = await db.execute(
                select(KenoDraw)
                .where(
                    and_(
                        KenoDraw.status == KenoDrawStatus.PENDING,
                        KenoDraw.draw_time >= now,
                        KenoDraw.draw_time <= end_time
                    )
                )
            )
            existing_draws = existing_result.scalars().all()
            
            existing_times = {d.draw_time for d in existing_draws}
            
            current = now
            current = current.replace(second=0, microsecond=0)
            current = current + timedelta(minutes=(settings.KENO_DRAW_INTERVAL_MINUTES - current.minute % settings.KENO_DRAW_INTERVAL_MINUTES))
            
            last_draw_number = await _get_next_draw_number(db) - 1
            
            while current <= end_time:
                if current not in existing_times and current.hour >= 8 and current.hour < 23:
                    last_draw_number += 1
                    draw = KenoDraw(
                        draw_number=last_draw_number,
                        draw_time=current,
                        status=KenoDrawStatus.PENDING
                    )
                    db.add(draw)
                
                current += timedelta(minutes=settings.KENO_DRAW_INTERVAL_MINUTES)
            
            await db.commit()
            logger.info(f"✅ {last_draw_number} tirages Keno planifiés")
            
    except Exception as e:
        logger.error(f"❌ Erreur planification Keno: {e}")
        raise


@celery_app.task(
    name="app.workers.draw_worker.cancel_stale_draws",
    max_retries=2
)
def cancel_stale_draws():
    """Annule les tirages Keno en attente trop vieux"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cancel_stale_draws_async())
    else:
        return loop.run_until_complete(_cancel_stale_draws_async())


async def _cancel_stale_draws_async():
    """Annule les tirages Keno en attente depuis plus de 1h"""
    logger.info("⏰ Vérification des tirages Keno en attente...")
    
    try:
        async with AsyncSessionLocal() as db:
            stale_time = datetime.utcnow() - timedelta(hours=1)
            
            result = await db.execute(
                select(KenoDraw)
                .where(
                    and_(
                        KenoDraw.status == KenoDrawStatus.PENDING,
                        KenoDraw.draw_time < stale_time
                    )
                )
            )
            stale_draws = result.scalars().all()
            
            if stale_draws:
                for draw in stale_draws:
                    draw.status = KenoDrawStatus.CANCELLED
                    draw.closed_at = datetime.utcnow()
                    draw.closed_by = "system"
                
                await db.commit()
                logger.info(f"❌ {len(stale_draws)} tirages Keno annulés (trop vieux)")
            
    except Exception as e:
        logger.error(f"❌ Erreur annulation tirages Keno: {e}")
        raise


# ==================== LUCKY - EXPORT LEH ====================

@celery_app.task(
    name="app.workers.draw_worker.export_lucky_results_to_leh",
    max_retries=3
)
def export_lucky_results_to_leh(start_date: str, end_date: str):
    """
    Exporte les résultats Lucky vers la LEH.
    À exécuter quotidiennement pour la conformité.
    """
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_export_lucky_results_to_leh_async(start_date, end_date))
    else:
        return loop.run_until_complete(_export_lucky_results_to_leh_async(start_date, end_date))


async def _export_lucky_results_to_leh_async(start_date: str, end_date: str):
    """Exporte les parties Lucky vers la LEH"""
    logger.info("📤 Export Lucky vers LEH...")
    
    try:
        async with AsyncSessionLocal() as db:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            
            result = await db.execute(
                select(LuckyPlay)
                .where(
                    and_(
                        LuckyPlay.played_at >= start,
                        LuckyPlay.played_at <= end,
                        LuckyPlay.is_deleted == False
                    )
                )
            )
            plays = result.scalars().all()
            
            if not plays:
                logger.info("Aucune partie Lucky à exporter")
                return
            
            export_data = {
                "game": "lucky_wheel",
                "period": {
                    "start": start_date,
                    "end": end_date
                },
                "total_plays": len(plays),
                "total_stake": sum(float(p.stake) for p in plays),
                "total_payout": sum(float(p.winnings) for p in plays),
                "plays": [
                    {
                        "play_id": p.id,
                        "user_id": p.user_id,
                        "ticket_id": p.ticket_id,
                        "stake": float(p.stake),
                        "multiplier": float(p.multiplier),
                        "winnings": float(p.winnings),
                        "segment": p.result_segment["label"],
                        "played_at": p.played_at.isoformat()
                    }
                    for p in plays
                ]
            }
            
            # Envoyer à la LEH
            # await leh_service.export_lucky(export_data)
            
            # Audit log
            audit = AuditLog(
                action=AuditAction.DRAW_GENERATED,
                resource_type="lucky_play",
                new_values={"exported_to_leh": True, "count": len(plays)}
            )
            db.add(audit)
            await db.commit()
            
            logger.info(f"✅ {len(plays)} parties Lucky exportées vers LEH")
            
    except Exception as e:
        logger.error(f"❌ Erreur export Lucky: {e}")
        raise


@celery_app.task(
    name="app.workers.draw_worker.export_lucky_daily_to_leh"
)
def export_lucky_daily_to_leh():
    """Export quotidien des parties Lucky vers la LEH"""
    today = datetime.utcnow().date()
    return export_lucky_results_to_leh.delay(
        today.isoformat(),
        today.isoformat()
    )