# app/services/draw_scheduler.py
import asyncio
from datetime import datetime, timedelta
from app.core.logger import logger
from app.services.keno_service import KenoService
from app.api.websockets.manager import broadcast_draw_result


class DrawScheduler:
    """Planificateur de tirages automatiques"""
    
    def __init__(self):
        self.is_running = False
        self.task = None
    
    async def start(self):
        """Démarre le scheduler"""
        self.is_running = True
        self.task = asyncio.create_task(self._run())
        logger.info("Draw scheduler started")
    
    async def stop(self):
        """Arrête le scheduler"""
        self.is_running = False
        if self.task:
            self.task.cancel()
        logger.info("Draw scheduler stopped")
    
    async def _run(self):
        """Boucle principale"""
        while self.is_running:
            try:
                # Vérifier si un tirage est dû
                now = datetime.utcnow()
                next_draw_time = self._get_next_draw_time()
                
                if now >= next_draw_time:
                    await self._execute_draw()
                
                await asyncio.sleep(1)  # Vérifier chaque seconde
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
    
    async def _execute_draw(self):
        """Exécute un tirage"""
        logger.info("Executing draw...")
        # Logique de tirage à implémenter
        pass


# Instance globale
scheduler = DrawScheduler()


async def start_scheduler():
    await scheduler.start()


async def stop_scheduler():
    await scheduler.stop()