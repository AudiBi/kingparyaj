# app/services/draw_scheduler.py
"""Ancien planificateur de tirages Keno : INUTILISÉ.

Les tirages sont gérés par le cycle automatique (app/services/game_loop.py, tâches Celery
`keno_tick`…) et lancés par les agents (launch_draw). Ce module n'est plus démarré par
l'application ; il reste pour compatibilité, mais sa boucle ne fait plus rien et laisse
toujours la main à la boucle d'événements (l'ancienne version la bloquait sur la moindre erreur).
"""
import asyncio

from app.core.logger import logger


class DrawScheduler:
    """Planificateur désactivé."""

    def __init__(self, *args, **kwargs):  # args : (db, redis) du conteneur de services, inutilisés
        self.is_running = False
        self.task = None

    async def start(self):
        self.is_running = True
        self.task = asyncio.create_task(self._run())
        logger.info("Draw scheduler (inactif) démarré")

    async def stop(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
        logger.info("Draw scheduler stopped")

    async def _run(self):
        while self.is_running:
            await asyncio.sleep(60)  # aucune logique : les tirages passent par le cycle des jeux


scheduler = DrawScheduler()


async def start_scheduler():
    await scheduler.start()


async def stop_scheduler():
    await scheduler.stop()
