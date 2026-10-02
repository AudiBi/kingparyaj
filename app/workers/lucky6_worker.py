# app/workers/lucky6_worker.py
"""Cycle automatique Lucky6 : manches partagées toutes les N minutes
(ouverture / fermeture des paris, tirage, règlement, manche suivante).
Même mécanisme que Horse Races (voir horse_race_worker.tick_round_game)."""

from app.workers.celery import celery_app
from app.workers.horse_race_worker import _run, tick_round_game


async def _tick_async():
    from app.services.lucky6_service import Lucky6Service

    return await tick_round_game(Lucky6Service, "lock:lucky6:tick", "🎱 Lucky6")


@celery_app.task(name="app.workers.lucky6_worker.lucky6_tick", max_retries=0)
def lucky6_tick():
    """Fait avancer les manches Lucky6 (appelée par Celery beat)."""
    return _run(_tick_async())
