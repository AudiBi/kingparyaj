# tests/test_services/test_game_loop.py
"""Cycle automatique intégré à l'application : sans Celery, les parties sont
créées puis lancées (Keno partagé, Lucky6, Horse Races)."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.game import GameRound
from app.models.keno import KenoDraw
from app.services import game_loop, keno_engine, lucky6_engine
from app.services.horse_race_service import HorseRaceService
from app.services.lucky6_service import Lucky6Service


@pytest.fixture
def wired(db_session, fake_redis, monkeypatch):
    import app.workers.draw_worker as draw_worker
    import app.workers.horse_race_worker as horse_worker
    import app.workers.keno_worker as keno_worker

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    for module in (draw_worker, horse_worker):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    for module in (draw_worker, horse_worker, keno_worker):
        monkeypatch.setattr(module, "redis_client", fake_redis)
    monkeypatch.setattr(HorseRaceService, "_within_opening_hours", staticmethod(lambda config: True))
    monkeypatch.setattr(Lucky6Service, "_within_opening_hours", staticmethod(lambda config: True))
    return fake_redis


@pytest.mark.asyncio
async def test_run_once_creates_games_without_celery(wired, db_session, monkeypatch):
    import app.services.keno_service as ks

    class _Haiti:  # heure d'ouverture du Keno (8 h - 23 h) toujours vraie dans ce test
        hour = 12

    monkeypatch.setattr("app.core.timezone.now_haiti", lambda: _Haiti)
    await wired.set("settings:keno", json.dumps(keno_engine.validate_config({"open_hour": 0, "close_hour": 24})))
    await wired.set("settings:lucky6", json.dumps(lucky6_engine.validate_config({})))

    await game_loop.run_once()

    rounds = (await db_session.execute(select(GameRound.game_type, GameRound.status))).all()
    assert ("lucky6", "BETTING_OPEN") in rounds and ("horse_races", "BETTING_OPEN") in rounds
    draws = (await db_session.execute(select(KenoDraw))).scalars().all()
    assert len(draws) == 2 and all(d.mode == "scheduled" for d in draws)
    assert ks  # module importé


@pytest.mark.asyncio
async def test_round_started_by_admin_is_finished_and_settled(wired, db_session, fake_redis):
    service = Lucky6Service(db_session, fake_redis)
    race = await service.create_race()
    await service.start_race(race.id)
    race.started_at = race.started_at - timedelta(minutes=5)  # animation terminée
    await db_session.commit()

    await game_loop.run_once()
    await db_session.refresh(race)
    assert race.status == "SETTLED"


@pytest.mark.asyncio
async def test_loop_starts_and_stops(wired, monkeypatch):
    calls = []

    async def fake_run_once():
        calls.append(1)

    monkeypatch.setattr(game_loop, "run_once", fake_run_once)
    monkeypatch.setattr(game_loop, "INTERVAL_SECONDS", 0.01)
    import asyncio

    game_loop.start()
    await asyncio.sleep(0.05)
    await game_loop.stop()
    assert len(calls) >= 2
