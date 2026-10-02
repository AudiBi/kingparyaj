# app/workers/tasks.py
"""Tâches Celery partagées (diagnostic)."""

from app.core.timezone import now_utc
from app.workers.celery import celery_app


@celery_app.task(name="app.workers.tasks.health_check")
def health_check():
    """Vérifie qu'un worker Celery répond."""
    return {"status": "ok", "timestamp": now_utc().isoformat() + "Z"}


@celery_app.task(name="app.workers.tasks.test_task")
def test_task(x: int = 1, y: int = 1):
    """Tâche de test : additionne deux nombres."""
    return x + y
