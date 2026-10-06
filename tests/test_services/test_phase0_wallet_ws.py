# tests/test_services/test_phase0_wallet_ws.py
"""Phase 0 (prérequis Horse Races) : traçabilité des transactions, compteurs
journaliers, jeu responsable à la mise, idempotence des gains, règlement Keno
idempotent (settle_bets_for_draw) et relais WebSocket."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import AppException, InsufficientBalanceException
from app.models.enums import ExclusionReason, ExclusionType, KenoBetStatus, KenoDrawStatus
from app.models.keno import KenoBet, KenoDraw
from app.models.responsible import SelfExclusion
from app.models.transaction import Transaction
from app.services.keno_service import KenoService
from app.services.wallet_service import WalletService


# ========== Traçabilité ==========

@pytest.mark.asyncio
async def test_debit_for_bet_records_bet_and_draw_ids(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("500"))
    service = WalletService(db_session, fake_redis)

    tx = await service.debit_for_bet(user.id, Decimal("50"), bet_id="bet-1", draw_id="draw-1")

    assert tx.bet_id == "bet-1"
    assert tx.draw_id == "draw-1"
    assert tx.transaction_type == "BET"
    assert await service.get_balance(user.id) == Decimal("450")


@pytest.mark.asyncio
async def test_credit_reference_id_is_kept_as_bet_id(db_session, fake_redis, make_user):
    user = await make_user()
    service = WalletService(db_session, fake_redis)
    tx = await service.credit(user.id, Decimal("10"), "WIN", "bet-legacy")
    assert tx.bet_id == "bet-legacy"


# ========== Compteurs journaliers ==========

@pytest.mark.asyncio
async def test_daily_counters_bets_wins_and_withdrawals(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    service = WalletService(db_session, fake_redis)

    await service.debit_for_bet(user.id, Decimal("100"))
    await service.credit_for_win(user.id, Decimal("30"))
    await service.debit(user.id, Decimal("200"), "WITHDRAWAL")

    wallet = await service.get_by_user_id(user.id)
    assert wallet.today_bets == Decimal("100")
    assert wallet.today_losses == Decimal("70")       # 100 misés - 30 gagnés
    # un retrait n'est ni une mise ni une perte


@pytest.mark.asyncio
async def test_daily_counters_reset_on_new_haiti_day(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    service = WalletService(db_session, fake_redis)
    wallet = await service.get_by_user_id(user.id)
    wallet.today_losses = Decimal("900")
    wallet.today_bets = Decimal("900")
    wallet.last_reset_date = datetime.utcnow() - timedelta(days=2)
    wallet.daily_loss_limit = Decimal("1000")

    await service.debit_for_bet(user.id, Decimal("200"))   # passerait après remise à zéro

    assert wallet.today_losses == Decimal("200")


# ========== Jeu responsable ==========

@pytest.mark.asyncio
async def test_single_bet_limit_blocks_bet(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    service = WalletService(db_session, fake_redis)
    (await service.get_by_user_id(user.id)).single_bet_limit = Decimal("100")

    with pytest.raises(AppException) as exc:
        await service.debit_for_bet(user.id, Decimal("150"))
    assert exc.value.code == "BET_LIMIT"
    assert await service.get_balance(user.id) == Decimal("1000")


@pytest.mark.asyncio
async def test_single_bet_limit_does_not_block_withdrawal(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    service = WalletService(db_session, fake_redis)
    (await service.get_by_user_id(user.id)).single_bet_limit = Decimal("100")

    await service.debit(user.id, Decimal("500"), "WITHDRAWAL")
    assert await service.get_balance(user.id) == Decimal("500")


@pytest.mark.asyncio
async def test_daily_loss_limit_blocks_bet(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    service = WalletService(db_session, fake_redis)
    (await service.get_by_user_id(user.id)).daily_loss_limit = Decimal("150")

    await service.debit_for_bet(user.id, Decimal("100"))
    with pytest.raises(AppException) as exc:
        await service.debit_for_bet(user.id, Decimal("100"))
    assert exc.value.code == "LOSS_LIMIT"


@pytest.mark.asyncio
async def test_active_self_exclusion_blocks_bet(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    now = datetime.utcnow()
    db_session.add(SelfExclusion(
        user_id=user.id,
        exclusion_type=ExclusionType.TEMPORARY,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=30),
        reason=ExclusionReason.SELF_REQUEST,
        is_active=True,
        activated_at=now - timedelta(days=1),
    ))
    await db_session.flush()

    with pytest.raises(AppException) as exc:
        await WalletService(db_session, fake_redis).debit_for_bet(user.id, Decimal("10"))
    assert exc.value.code == "SELF_EXCLUDED"


@pytest.mark.asyncio
async def test_locked_account_cannot_bet(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    user.is_locked = True
    await db_session.flush()

    with pytest.raises(AppException) as exc:
        await WalletService(db_session, fake_redis).debit_for_bet(user.id, Decimal("10"))
    assert exc.value.code == "ACCOUNT_LOCKED"


@pytest.mark.asyncio
async def test_insufficient_balance_still_raises_400(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("5"))
    with pytest.raises(InsufficientBalanceException):
        await WalletService(db_session, fake_redis).debit_for_bet(user.id, Decimal("10"))


# ========== Idempotence des gains ==========

@pytest.mark.asyncio
async def test_same_win_reference_cannot_be_paid_twice(db_session, fake_redis, make_user):
    user = await make_user()
    user_id = user.id  # lu avant le rollback (qui expire les objets chargés)
    service = WalletService(db_session, fake_redis)
    await service.credit_for_win(user_id, Decimal("100"), bet_id="b1", reference="WIN-TEST-b1")
    await db_session.commit()

    with pytest.raises(IntegrityError):
        await service.credit_for_win(user_id, Decimal("100"), bet_id="b1", reference="WIN-TEST-b1")
    await db_session.rollback()

    from app.models.wallet import Wallet
    balance = (await db_session.execute(select(Wallet.balance).where(Wallet.user_id == user_id))).scalar_one()
    assert balance == Decimal("100")


# ========== Keno : settle_bets_for_draw ==========

def _fake_keno_queries(db_session, draw, bets):
    """Comme test_keno_service, mais respecte le filtre « PENDING » des paris
    pour pouvoir tester l'idempotence d'un second règlement."""
    original_execute = db_session.execute

    async def patched_execute(statement, *args, **kwargs):
        compiled = str(statement)
        if "keno_draws" in compiled:
            result = MagicMock()
            result.scalar_one_or_none.return_value = draw
            return result
        if "keno_bets" in compiled:
            result = MagicMock()
            result.scalars.return_value.all.return_value = [b for b in bets if b.status == KenoBetStatus.PENDING]
            return result
        return await original_execute(statement, *args, **kwargs)

    db_session.execute = patched_execute


@pytest.mark.asyncio
async def test_settle_bets_for_draw_is_idempotent(db_session, fake_redis, make_user):
    winner = await make_user(balance=Decimal("100"))
    draw = KenoDraw(id="draw-s", draw_number=7, draw_time=datetime.utcnow() + timedelta(minutes=5), status=KenoDrawStatus.PENDING)
    db_session.add(draw)
    await db_session.flush()

    service = KenoService(db_session, fake_redis)
    bet = await service.create_bet(draw_id=draw.id, picks=[1, 2, 3], stake=100, user_id=winner.id)
    service.rng.generate_keno_numbers = lambda: list(range(1, 21))

    first = await service.settle_bets_for_draw(draw.id)
    second = await service.settle_bets_for_draw(draw.id)

    assert first["winners_count"] == 1 and first["total_payout"] == 1200.0
    assert second["settled_bets"] == 0 and second["winners_count"] == 0
    assert draw.status == KenoDrawStatus.COMPLETED
    assert await WalletService(db_session, fake_redis).get_balance(winner.id) == Decimal("1200")

    txs = (await db_session.execute(
        select(Transaction).where(Transaction.bet_id == bet.id, Transaction.reference.like("WIN-%"))
    )).scalars().all()
    assert [t.reference for t in txs] == [f"WIN-KENO-{bet.id}"]
    assert txs[0].draw_id == "draw-s"


@pytest.mark.asyncio
async def test_settle_bets_for_cancelled_draw_is_rejected(db_session, fake_redis):
    from app.core.exceptions import GameException

    draw = KenoDraw(id="draw-c", draw_number=8, draw_time=datetime.utcnow(), status=KenoDrawStatus.CANCELLED)
    service = KenoService(db_session, fake_redis)
    _fake_keno_queries(db_session, draw, [])
    with pytest.raises(GameException):
        await service.settle_bets_for_draw(draw.id)


# ========== WebSocket ==========

class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)


@pytest.mark.asyncio
async def test_broadcast_without_redis_pubsub_falls_back_to_local_clients(monkeypatch):
    from app.api.websockets import draws, manager as ws

    class NoPubSub:
        async def publish(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr(ws, "redis_client", NoPubSub())
    socket = _FakeSocket()
    draws.manager.all_connections.add(socket)
    try:
        await ws.broadcast_draw_result({"draw_number": 1})   # ne doit plus planter (bug datetime)
    finally:
        draws.manager.all_connections.discard(socket)

    assert socket.sent and socket.sent[0]["type"] == "draw_completed"
    assert socket.sent[0]["timestamp"].endswith("Z")


@pytest.mark.asyncio
async def test_broadcast_goes_through_redis_channel(monkeypatch):
    from app.api.websockets import manager as ws

    published = []

    class RecordingRedis:
        async def publish(self, channel, data):
            published.append((channel, data))

    monkeypatch.setattr(ws, "redis_client", RecordingRedis())
    await ws.broadcast_draw_result({"draw_id": "d1", "numbers": [1, 2]})

    assert len(published) == 1
    assert published[0][0] == ws.WS_BROADCAST_CHANNEL
    assert '"draw_completed"' in published[0][1]


def test_legacy_manager_import_points_to_real_connection_manager():
    from app.api.websockets import draws
    from app.api.websockets.manager import manager

    assert manager.all_connections is draws.manager.all_connections
