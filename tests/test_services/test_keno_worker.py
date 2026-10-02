# tests/test_services/test_keno_worker.py
"""Worker Celery Keno : il passe par le règlement sécurisé de KenoService."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timezone import now_utc
from app.models.enums import KenoBetStatus, KenoDrawStatus
from app.models.keno import KenoBet, KenoDraw
from app.models.transaction import Transaction
from app.services.keno_service import KenoService
from app.services.wallet_service import WalletService


@pytest.fixture
def worker(db_session, fake_redis, monkeypatch):
    import app.api.websockets.manager as manager
    import app.workers.draw_worker as draw_worker

    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(draw_worker, "AsyncSessionLocal", factory)
    monkeypatch.setattr(draw_worker, "redis_client", fake_redis)
    sent = []

    async def fake_broadcast(payload):
        sent.append(payload)

    monkeypatch.setattr(manager, "broadcast_draw_result", fake_broadcast)
    monkeypatch.setattr("app.services.rng_service.RNGService.generate_keno_numbers", lambda self: list(range(1, 21)))
    draw_worker.sent = sent
    return draw_worker


async def _due_draw_with_bets(db_session, fake_redis, make_user, minutes_ago=1):
    draw = KenoDraw(draw_number=1, draw_time=now_utc() + timedelta(minutes=5), status=KenoDrawStatus.PENDING)
    db_session.add(draw)
    await db_session.flush()
    service = KenoService(db_session, fake_redis)
    users = [await make_user(balance=Decimal("10")) for _ in range(3)]
    for user in users:
        await service.create_bet(draw_id=draw.id, picks=[1], stake=10, user_id=user.id)
    draw.draw_time = now_utc() - timedelta(minutes=minutes_ago)  # échéance atteinte
    await db_session.commit()
    return draw, users


@pytest.mark.asyncio
async def test_worker_settles_due_draw_once_with_unique_win_references(worker, db_session, fake_redis, make_user):
    draw, users = await _due_draw_with_bets(db_session, fake_redis, make_user)

    first = await worker._process_draw_async()
    second = await worker._process_draw_async()  # rien à refaire

    assert first["settled"] == 1 and second["settled"] == 0
    for user in users:
        assert await WalletService(db_session, fake_redis).get_balance(user.id) == Decimal("25")
    refs = (await db_session.execute(select(Transaction.reference).where(Transaction.reference.like("WIN-KENO-%")))).scalars().all()
    assert len(refs) == 3 and len(set(refs)) == 3
    await db_session.refresh(draw)
    assert draw.status == KenoDrawStatus.COMPLETED and draw.total_bets == 3
    assert worker.sent and worker.sent[0]["numbers"] == list(range(1, 21))
    assert "winner_bet_ids" not in worker.sent[0]


@pytest.mark.asyncio
async def test_stale_draw_with_bets_is_drawn_not_cancelled(worker, db_session, fake_redis, make_user):
    """Avant : un tirage en attente depuis plus d'1h était annulé et les mises perdues."""
    draw, users = await _due_draw_with_bets(db_session, fake_redis, make_user, minutes_ago=120)
    empty = KenoDraw(draw_number=2, draw_time=now_utc() - timedelta(minutes=90), status=KenoDrawStatus.PENDING)
    db_session.add(empty)
    await db_session.commit()

    assert await worker._cancel_stale_draws_async() == 1
    await db_session.refresh(draw)
    await db_session.refresh(empty)
    assert draw.status == KenoDrawStatus.PENDING
    assert empty.status == KenoDrawStatus.CANCELLED

    await worker._process_draw_async()
    pending = (await db_session.execute(
        select(func.count(KenoBet.id)).where(KenoBet.status == KenoBetStatus.PENDING)
    )).scalar()
    assert pending == 0


@pytest.mark.asyncio
async def test_worker_has_no_private_settlement_copy(worker):
    for name in ("generate_draw_numbers", "_credit_user_wallet", "_credit_ticket"):
        assert not hasattr(worker, name), f"copie du règlement encore présente : {name}"
