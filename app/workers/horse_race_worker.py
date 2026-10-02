# app/workers/horse_race_worker.py
"""Cycle automatique Horse Races : une tâche Celery appelée toutes les
quelques secondes fait avancer les courses (ouverture/fermeture des paris,
départ, arrivée, règlement, création de la course suivante)."""

import asyncio

from app.core.database import AsyncSessionLocal
from app.core.logger import get_logger
from app.core.redis_client import redis_client
from app.workers.celery import celery_app

logger = get_logger(__name__)

# Une boucle d'événements par processus worker (le client Redis async y reste attaché)
_loop = None


def _run(coro):
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(coro)


async def _tick_async():
    from app.services.horse_race_service import HorseRaceService

    return await tick_round_game(HorseRaceService, "lock:horse_races:tick", "🐎 Horse Races")


async def tick_round_game(service_cls, lock_key: str, label: str):
    """Tick commun aux jeux à manches (Horse Races, Lucky6). Même boucle
    d'événements pour toutes les tâches du processus (client Redis async)."""
    # Verrou court (best effort) : un seul tick à la fois, même avec plusieurs workers
    try:
        if not await redis_client.set(lock_key, "1", nx=True, ex=30):
            return {"skipped": True}
    except Exception as e:
        logger.warning(f"Verrou Redis indisponible, tick exécuté quand même : {e}")
        lock_key = None

    try:
        async with AsyncSessionLocal() as db:
            service = service_cls(db, redis_client)
            try:
                counts = await service.tick()
                await service.commit_and_publish()
            except Exception:
                await db.rollback()
                raise
        if any(counts.values()):
            logger.info(f"{label} tick : {counts}")
        return counts
    finally:
        if lock_key:
            try:
                await redis_client.delete(lock_key)
            except Exception:
                pass


@celery_app.task(name="app.workers.horse_race_worker.horse_race_tick", max_retries=0)
def horse_race_tick():
    """Fait avancer le cycle des courses (appelée par Celery beat)."""
    return _run(_tick_async())
