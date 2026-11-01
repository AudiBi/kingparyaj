# app/services/game_loop.py
"""Cycle automatique des jeux, intégré à l'application.

Fait avancer, toutes les 2 secondes, les jeux à manches partagées :
Keno (tirages partagés), Lucky6 et Horse Races — création automatique des
parties, fin de l'animation, règlement.

Le tirage / départ n'est PAS déclenché ici par défaut : un agent lance chaque
partie à la main (réglage `manual_launch`, voir launch_race / launch_draw) pour
vendre autant de fiches qu'il veut. Seul le mode « à l'heure » (option) ferme
les paris et tire tout seul.

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


MAX_BACKOFF_SECONDS = 30


def next_delay(consecutive_errors: int) -> float:
    """Pause avant le prochain cycle : 2 s normalement ; si la base (ou Redis) est en panne, 4, 8, 16 puis
    30 s (au lieu de la solliciter toutes les 2 s pendant sa récupération) ; retour à 2 s dès que ça repasse."""
    if consecutive_errors <= 0:
        return INTERVAL_SECONDS
    return min(INTERVAL_SECONDS * (2 ** consecutive_errors), MAX_BACKOFF_SECONDS)


async def run_once() -> int:
    """Un cycle pour chaque jeu ; l'erreur d'un jeu n'arrête pas les autres. Renvoie le nombre d'erreurs."""
    from app.services.horse_race_service import HorseRaceService
    from app.services.lucky6_service import Lucky6Service
    from app.workers.horse_race_worker import tick_round_game
    from app.workers.keno_worker import _tick_async as keno_tick

    errors = 0
    for label, call in (
        ("Keno", lambda: keno_tick()),
        ("Lucky6", lambda: tick_round_game(Lucky6Service, "lock:lucky6:tick", "🎱 Lucky6")),
        ("Horse Races", lambda: tick_round_game(HorseRaceService, "lock:horse_races:tick", "🐎 Horse Races")),
    ):
        try:
            await call()
        except Exception as e:
            errors += 1
            logger.error(f"Cycle {label} en erreur : {e}")
    return errors


async def _loop() -> None:
    logger.info(f"✅ Cycle automatique des jeux démarré (toutes les {INTERVAL_SECONDS} s)")
    failures = 0
    while True:
        errors = await run_once()
        failures = failures + 1 if errors else 0
        await asyncio.sleep(next_delay(failures))


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
