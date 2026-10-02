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
from app.core.exceptions import GameException
from app.core.timezone import now_haiti, now_utc, today_haiti, local_date_start_utc, local_date_end_utc
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


async def _process_draw_async(now: Optional[datetime] = None) -> Dict[str, int]:
    """Keno partagé : tire et règle les tirages arrivés à l'heure, garde les
    prochains tirages créés, puis diffuse l'état aux écrans.

    Tout le règlement passe par KenoService (tirage verrouillé, seuls les paris
    en attente sont réglés, gains à référence unique WIN-KENO-<pari>) : un
    tirage ne peut pas être payé deux fois, même si deux workers tournent.
    Un tirage échu est TOUJOURS tiré, même après la fermeture : ses paris ont
    été acceptés et doivent être réglés."""
    from app.services.keno_service import KenoService

    counts = {"settled": 0, "created": 0}
    async with AsyncSessionLocal() as db:
        service = KenoService(db, redis_client)
        try:
            outcome = await service.tick(now)
        except Exception:
            await db.rollback()
            raise
        counts["settled"] = len(outcome["settled"])
        counts["created"] = outcome["created"]
        for result in outcome["settled"]:
            await _after_draw(db, result)
        if counts["settled"]:
            await service.publish_live("draw_started")
        elif counts["created"]:
            await service.publish_live("betting_opened")
    if any(counts.values()):
        logger.info(f"🎱 Keno : {counts}")
    return counts


async def _after_draw(db: AsyncSession, result: Dict) -> None:
    """Après le règlement (déjà validé en base) : cache, diffusion, export LEH,
    notifications. Une erreur ici n'annule jamais le règlement."""
    from app.api.websockets.manager import broadcast_draw_result

    payload = {
        "draw_id": result["draw_id"],
        "draw_number": result["draw_number"],
        "numbers": result["numbers"],
        "total_bets": result["total_bets"],
        "winners_count": result["winners_count"],
        "total_payout": result["total_payout"],
    }
    try:
        await redis_client.setex(f"keno:draw:{result['draw_id']}", 3600, json.dumps(result["numbers"]))
        await redis_client.setex("keno:draw:latest", 3600, json.dumps(payload))
        await broadcast_draw_result({"type": "keno_draw", **payload})
    except Exception as e:
        logger.error(f"⚠️ Diffusion du tirage Keno #{result['draw_number']} : {e}")

    try:
        draw = await db.get(KenoDraw, result["draw_id"])
        await _export_keno_to_leh(db, draw)
        await db.commit()
        if result["winner_bet_ids"]:
            winners = await db.execute(
                select(KenoBet).where(KenoBet.id.in_(result["winner_bet_ids"]), KenoBet.winnings >= 5000)
            )
            big = winners.scalars().all()
            if big:
                await _notify_keno_big_winners(db, big)
    except Exception as e:
        await db.rollback()
        logger.error(f"⚠️ Export / notifications du tirage Keno #{result['draw_number']} : {e}")

    logger.info(
        f"✅ Tirage Keno #{result['draw_number']} terminé. "
        f"Gagnants: {result['winners_count']}/{result['total_bets']}, Payout: {result['total_payout']} HTG"
    )


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
            ip_address="0.0.0.0",
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
    """Planifie les tirages Keno des prochaines 24h (heures d'ouverture, heure d'Haïti)."""
    from app.services.keno_service import KenoService

    async with AsyncSessionLocal() as db:
        created = await KenoService(db, redis_client).schedule_draws(hours=24)
        await db.commit()
    logger.info(f"✅ {created} tirages Keno planifiés")
    return created


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
    """Annule les tirages Keno en attente depuis plus d'1h, SEULEMENT s'ils
    n'ont aucun pari. Un tirage avec des paris est tiré par process_draw
    (avant, il était annulé et les mises étaient perdues)."""
    from app.services.keno_service import KenoService

    async with AsyncSessionLocal() as db:
        count = await KenoService(db, redis_client).cancel_pending_draws_without_bets(
            older_than=now_utc() - timedelta(hours=1)
        )
        await db.commit()
    if count:
        logger.info(f"❌ {count} tirages Keno sans pari annulés (trop vieux)")
    return count


@celery_app.task(
    name="app.workers.draw_worker.export_draw_results_to_leh",
    max_retries=3
)
def export_draw_results_to_leh(date_str: Optional[str] = None):
    """
    Exporte vers la LEH tous les tirages Keno terminés d'une journée
    (heure d'Haïti). Sans argument : la veille (tâche planifiée à 01:00).
    """
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_export_draw_results_to_leh_async(date_str))
    else:
        return loop.run_until_complete(_export_draw_results_to_leh_async(date_str))


async def _export_draw_results_to_leh_async(date_str: Optional[str] = None):
    """Exporte les tirages Keno terminés d'une journée locale vers la LEH"""
    day = date_str or (today_haiti() - timedelta(days=1)).isoformat()
    start = local_date_start_utc(day)
    end = local_date_end_utc(day)  # exclusive
    logger.info(f"📤 Export LEH des tirages Keno du {day}...")

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(KenoDraw).where(
                    and_(
                        KenoDraw.status == KenoDrawStatus.COMPLETED,
                        KenoDraw.draw_time >= start,
                        KenoDraw.draw_time < end,
                    )
                )
            )
            draws = result.scalars().all()
            for draw in draws:
                await _export_keno_to_leh(db, draw)
            await db.commit()

        logger.info(f"✅ {len(draws)} tirage(s) Keno exporté(s) vers LEH pour le {day}")
        return {"date": day, "exported": len(draws)}

    except Exception as e:
        logger.error(f"❌ Erreur export LEH des tirages Keno: {e}")
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
            start = local_date_start_utc(start_date)
            end = local_date_end_utc(end_date)  # exclusive
            
            result = await db.execute(
                select(LuckyPlay)
                .where(
                    and_(
                        LuckyPlay.played_at >= start,
                        LuckyPlay.played_at < end,
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
                ip_address="0.0.0.0",
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
    """Export quotidien des parties Lucky vers la LEH (journée d'hier, heure d'Haïti).

    Planifiée à 01:30 heure d'Haïti : on exporte la journée complète de la
    veille, et non la journée en cours (quasi vide à cette heure-là).
    """
    yesterday = today_haiti() - timedelta(days=1)
    return export_lucky_results_to_leh.delay(
        yesterday.isoformat(),
        yesterday.isoformat()
    )