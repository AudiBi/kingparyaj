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
    # lancement manuel (réglage par défaut) : un seul tirage ouvert, sans heure, qui attend un agent
    assert len(draws) == 1 and draws[0].mode == "scheduled" and draws[0].config["manual_launch"] is True
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


# ---------------------------------------------------------------- base indisponible : on ralentit au lieu d'insister

def test_delay_grows_while_the_database_is_down_and_resets():
    assert [game_loop.next_delay(n) for n in range(0, 7)] == [2, 4, 8, 16, 30, 30, 30]
    assert game_loop.next_delay(0) == game_loop.INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_loop_backs_off_on_errors_then_recovers(monkeypatch):
    import asyncio

    results = iter([1, 3, 2, 0, 0])  # erreurs, puis retour à la normale
    pauses = []

    async def fake_run_once():
        return next(results)

    async def fake_sleep(seconds):
        pauses.append(seconds)
        if len(pauses) >= 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(game_loop, "run_once", fake_run_once)
    monkeypatch.setattr(game_loop.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await game_loop._loop()
    assert pauses == [4, 8, 16, 2, 2]  # 1re, 2e, 3e erreur consécutives, puis 2 s dès que ça repasse


@pytest.mark.asyncio
async def test_run_once_counts_failures_per_game(monkeypatch):
    import app.workers.horse_race_worker as horse_worker
    import app.workers.keno_worker as keno_worker

    async def broken(*args, **kwargs):
        raise RuntimeError("the database system is in recovery mode")

    monkeypatch.setattr(keno_worker, "_tick_async", broken)
    monkeypatch.setattr(horse_worker, "tick_round_game", broken)
    assert await game_loop.run_once() == 3  # Keno, Lucky6, Horse Races : l'erreur d'un jeu n'arrête pas les autres


def test_keno_worker_shares_the_application_connection_pool():
    """Un 2e pool par processus multipliait les connexions ouvertes (base de 256 Mo : crash en recovery mode)."""
    import app.core.database as shared
    import app.workers.draw_worker as draw_worker

    assert draw_worker.engine is shared.engine and draw_worker.AsyncSessionLocal is shared.AsyncSessionLocal
