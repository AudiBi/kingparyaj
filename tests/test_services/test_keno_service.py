# tests/test_services/test_keno_service.py
"""Tests critiques du règlement des paris Keno : calcul des gains (table de
paiement) puis règlement complet d'un tirage (execute_draw).

Note d'implémentation : KenoDraw/KenoBet utilisent des colonnes ARRAY,
spécifiques à Postgres, qui ne peuvent pas être créées sur la base SQLite de
test (cf. tests/conftest.py). Les tirages et paris sont donc construits comme
de simples objets Python en mémoire (jamais ajoutés à la session), et les
deux requêtes de lecture faites par KenoService (le tirage, puis ses paris en
attente) sont interceptées pour renvoyer ces objets. Le crédit des gagnants,
lui, passe par un vrai WalletService adossé à la base de test : le test
vérifie donc un règlement de bout en bout, pas seulement le calcul.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.core.exceptions import GameException, InsufficientBalanceException
from app.models.keno import KenoBet, KenoDraw
from app.models.enums import KenoBetStatus, KenoDrawStatus
from app.schemas.keno import KenoBetCreate
from app.services.keno_service import KenoService
from app.services.wallet_service import WalletService


# ========== Calcul des gains (table de paiement) — logique pure ==========

@pytest.mark.parametrize(
    "picks_count,hits,expected_multiplier",
    [
        (1, 1, Decimal("2.5")),
        (1, 0, Decimal("0")),
        (3, 3, Decimal("12")),
        (3, 2, Decimal("1.5")),
        (3, 0, Decimal("0")),
        (6, 6, Decimal("120")),
        (10, 10, Decimal("5000")),
        (10, 4, Decimal("0.5")),
        (10, 3, Decimal("0")),  # 3 hits sur 10 picks : hors table -> aucun gain
    ],
)
def test_calculate_winnings_matches_paytable(db_session, fake_redis, picks_count, hits, expected_multiplier):
    service = KenoService(db_session, fake_redis)
    picks = list(range(1, picks_count + 1))
    draw_numbers = list(range(1, hits + 1)) + list(range(61, 61 + (20 - hits)))
    stake = Decimal("100")

    winnings, computed_hits = service._calculate_winnings(picks, draw_numbers, stake)

    assert computed_hits == hits
    assert winnings == stake * expected_multiplier


def test_get_multiplier_unknown_picks_count_returns_zero(db_session, fake_redis):
    service = KenoService(db_session, fake_redis)
    assert service._get_multiplier(picks_count=15, hits=5) == Decimal("0")


# ========== Règlement d'un tirage (execute_draw) ==========

def _install_fake_bet_queries(db_session, draw: KenoDraw, bets: list[KenoBet]):
    """Redirige les SELECT keno_draws / keno_bets de la session vers des
    objets en mémoire, sans toucher aux autres requêtes (Wallet, Transaction,
    AuditLog) qui continuent d'utiliser la vraie base SQLite de test."""
    original_execute = db_session.execute

    async def patched_execute(statement, *args, **kwargs):
        compiled = str(statement)
        if "keno_draws" in compiled:
            result = MagicMock()
            result.scalar_one_or_none.return_value = draw
            return result
        if "keno_bets" in compiled:
            result = MagicMock()
            result.scalars.return_value.all.return_value = bets
            return result
        return await original_execute(statement, *args, **kwargs)

    db_session.execute = patched_execute


@pytest.mark.asyncio
async def test_execute_draw_settles_winners_and_losers(db_session, fake_redis, make_user):
    winner = await make_user()
    loser = await make_user()

    draw = KenoDraw(
        id="draw-1",
        draw_number=1,
        draw_time=datetime.utcnow(),
        status=KenoDrawStatus.PENDING,
    )
    winning_bet = KenoBet(
        id="bet-winner",
        user_id=winner.id,
        draw_id=draw.id,
        picks=[1, 2, 3],
        stake=Decimal("100"),
        status=KenoBetStatus.PENDING,
    )
    losing_bet = KenoBet(
        id="bet-loser",
        user_id=loser.id,
        draw_id=draw.id,
        picks=[21, 22, 23],
        stake=Decimal("50"),
        status=KenoBetStatus.PENDING,
    )

    service = KenoService(db_session, fake_redis)
    _install_fake_bet_queries(db_session, draw, [winning_bet, losing_bet])
    # Numéros gagnants déterministes : 1..20. picks du gagnant (1,2,3) => 3 hits.
    service.rng.generate_keno_numbers = lambda: list(range(1, 21))

    result = await service.execute_draw(draw.id)

    # picks=3, hits=3 -> multiplicateur 12 (cf. PAYTABLE)
    assert winning_bet.status == KenoBetStatus.WON
    assert winning_bet.hits == 3
    assert winning_bet.winnings == Decimal("1200")

    assert losing_bet.status == KenoBetStatus.LOST
    assert losing_bet.hits == 0
    assert losing_bet.winnings == Decimal("0")

    wallet_service = WalletService(db_session, fake_redis)
    assert await wallet_service.get_balance(winner.id) == Decimal("1200")
    assert await wallet_service.get_balance(loser.id) == Decimal("0")

    assert result["winners_count"] == 1
    assert result["total_payout"] == 1200.0
    assert draw.total_bets == 2
    assert draw.total_amount == Decimal("150")
    assert draw.status == KenoDrawStatus.COMPLETED


@pytest.mark.asyncio
async def test_execute_draw_rejects_already_settled_draw(db_session, fake_redis):
    draw = KenoDraw(
        id="draw-2",
        draw_number=2,
        draw_time=datetime.utcnow(),
        status=KenoDrawStatus.COMPLETED,
    )
    service = KenoService(db_session, fake_redis)
    _install_fake_bet_queries(db_session, draw, [])

    with pytest.raises(GameException):
        await service.execute_draw(draw.id)


# ========== Validation à la mise (place_bet) ==========

@pytest.mark.asyncio
async def test_place_bet_rejects_stake_below_minimum(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    draw = KenoDraw(
        id="draw-3",
        draw_number=3,
        draw_time=datetime.utcnow() + timedelta(minutes=10),
        status=KenoDrawStatus.PENDING,
    )
    service = KenoService(db_session, fake_redis)
    _install_fake_bet_queries(db_session, draw, [])

    with pytest.raises(GameException):
        await service.place_bet(
            user.id, KenoBetCreate(draw_id=draw.id, picks=[1, 2], stake=1)
        )


@pytest.mark.asyncio
async def test_place_bet_rejects_expired_draw(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("1000"))
    draw = KenoDraw(
        id="draw-4",
        draw_number=4,
        draw_time=datetime.utcnow() - timedelta(minutes=5),
        status=KenoDrawStatus.PENDING,
    )
    service = KenoService(db_session, fake_redis)
    _install_fake_bet_queries(db_session, draw, [])

    with pytest.raises(GameException):
        await service.place_bet(
            user.id, KenoBetCreate(draw_id=draw.id, picks=[1, 2], stake=50)
        )


@pytest.mark.asyncio
async def test_place_bet_rejects_insufficient_balance(db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("10"))
    draw = KenoDraw(
        id="draw-5",
        draw_number=5,
        draw_time=datetime.utcnow() + timedelta(minutes=10),
        status=KenoDrawStatus.PENDING,
    )
    service = KenoService(db_session, fake_redis)
    _install_fake_bet_queries(db_session, draw, [])

    with pytest.raises(InsufficientBalanceException):
        await service.place_bet(
            user.id, KenoBetCreate(draw_id=draw.id, picks=[1, 2], stake=500)
        )
