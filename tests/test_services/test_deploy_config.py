# tests/test_services/test_deploy_config.py
"""Déploiement (Render…) : adresse de base fournie par l'hébergeur, scheduler inoffensif,
migrations qui lisent l'adresse de l'application."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.config import Settings, async_database_url

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("given,expected", [
    ("postgres://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
    ("postgresql://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
    ("postgresql+asyncpg://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
    ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
    (" postgres://u:p@host/db ", "postgresql+asyncpg://u:p@host/db"),
])
def test_database_url_is_made_async(given, expected):
    assert async_database_url(given) == expected


def test_settings_normalize_database_url_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://render:secret@dpg-abc:5432/kp")
    assert Settings().DATABASE_URL == "postgresql+asyncpg://render:secret@dpg-abc:5432/kp"


@pytest.mark.asyncio
async def test_draw_scheduler_never_blocks_the_event_loop():
    """L'ancienne version tournait en boucle serrée sur une erreur et gelait l'application."""
    from app.services.draw_scheduler import DrawScheduler

    scheduler = DrawScheduler()
    await scheduler.start()
    beats = 0
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        beats += 1
    await scheduler.stop()
    assert beats >= 5


def test_application_does_not_start_the_old_scheduler():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "start_scheduler" not in source


def test_alembic_uses_the_application_database_url():
    """`alembic` doit viser DATABASE_URL (et non l'adresse en dur de alembic.ini)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "DATABASE_URL": "postgres://u:p@hote-render-introuvable.invalid:5432/kp"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0  # hôte injoignable
    assert "hote-render-introuvable" in output or "getaddrinfo" in output
    assert "127.0.0.1" not in output
