# app/services/game_loop.py
"""Cycle automatique des jeux, intégré à l'application.

Fait avancer, toutes les 2 secondes, les jeux à manches partagées :
Keno (tirages partagés), Lucky6 et Horse Races — création automatique des
parties, fermeture des paris, tirage / départ, fin, règlement.

Pourquoi dans l'application : sans worker Celery + Celery beat démarrés
(cas fréquent sur un poste Windows), aucune partie n'était créée ni lancée.
Les tâches Celery restent utilisables : chaque cycle prend le même verrou
Redis que la tâche Celery correspondante, deux processus ne font donc
jamais le même travail en même temps, et chaque transition est de toute
façon protégée en base (verrou + contrôle du statut).

Désactivable avec GAME_LOOP_ENABLED=false (ex. si seul Celery doit le faire).
"""

import asyncio
from typing import Optional

from app.core.logger import get_logger

logger = get_logger("GameLoop")

INTERVAL_SECONDS = 2
_task: Optional[asyncio.Task] = None


async def run_once() -> None:
    """Un cycle pour chaque jeu ; l'erreur d'un jeu n'arrête pas les autres."""
    from app.services.horse_race_service import HorseRaceService
    from app.services.lucky6_service import Lucky6Service
    from app.workers.horse_race_worker import tick_round_game
    from app.workers.keno_worker import _tick_async as keno_tick

    for label, call in (
        ("Keno", lambda: keno_tick()),
        ("Lucky6", lambda: tick_round_game(Lucky6Service, "lock:lucky6:tick", "🎱 Lucky6")),
        ("Horse Races", lambda: tick_round_game(HorseRaceService, "lock:horse_races:tick", "🐎 Horse Races")),
    ):
        try:
            await call()
        except Exception as e:
            logger.error(f"Cycle {label} en erreur : {e}")


async def _loop() -> None:
    logger.info(f"✅ Cycle automatique des jeux démarré (toutes les {INTERVAL_SECONDS} s)")
    while True:
        await run_once()
        await asyncio.sleep(INTERVAL_SECONDS)


def start() -> asyncio.Task:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
    return _task


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except BaseException:
            pass
        _task = None
