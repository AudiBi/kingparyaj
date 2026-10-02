# app/workers/keno_worker.py
"""Cycle automatique du Keno partagé : une tâche Celery toutes les 3 secondes
(tirage à l'heure, règlement, tirages suivants, diffusion aux écrans).
Même boucle d'événements que les autres jeux à manches (client Redis async)."""

from app.core.logger import get_logger
from app.core.redis_client import redis_client
from app.workers.celery import celery_app
from app.workers.horse_race_worker import _run

logger = get_logger(__name__)


async def _tick_async():
    from app.workers.draw_worker import _process_draw_async

    lock_key = "lock:keno:tick"
    try:
        if not await redis_client.set(lock_key, "1", nx=True, ex=30):
            return {"skipped": True}
    except Exception as e:
        logger.warning(f"Verrou Redis indisponible, tick exécuté quand même : {e}")
        lock_key = None
    try:
        return await _process_draw_async()
    finally:
        if lock_key:
            try:
                await redis_client.delete(lock_key)
            except Exception:
                pass


@celery_app.task(name="app.workers.keno_worker.keno_tick", max_retries=0)
def keno_tick():
    """Fait avancer les tirages Keno partagés (appelée par Celery beat)."""
    return _run(_tick_async())
