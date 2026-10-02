# tests/test_services/test_keno_service.py
"""Keno : prise de paris, tirage et règlement, sur de vraies lignes en base.

Les tables keno_draws / keno_bets sont créées dans la base SQLite de test
(listes d'entiers stockées en JSON sous SQLite, ARRAY sous PostgreSQL).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.exceptions import AppException, GameException, InsufficientBalanceException, ValidationException
from app.core.timezone import now_utc
from app.models.enums import KenoBetStatus, KenoDrawStatus, TicketStatus
from app.models.keno import KenoBet, KenoDraw
from app.models.ticket import Ticket
from app.models.transaction import Transaction
from app.schemas.keno import KenoBetCreate
from app.services.keno_service import KenoService
from app.services.rng_service import RNGService
from app.services.wallet_service import WalletService

WINNING = list(range(1, 21))  # numéros gagnants imposés dans les tests de règlement


@pytest.fixture
def service(db_session, fake_redis):
    return KenoService(db_session, fake_redis)


async def _draw(db_session, minutes=5, status=KenoDrawStatus.PENDING, number=None) -> KenoDraw:
    count = (await db_session.execute(select(func.count(KenoDraw.id)))).scalar() or 0
    draw = KenoDraw(
        draw_number=number or count + 1,
        draw_time=now_utc() + timedelta(minutes=minutes),
        status=status,
    )
    db_session.add(draw)
    await db_session.flush()
    return draw


async def _balance(db_session, fake_redis, user_id):
    return await WalletService(db_session, fake_redis).get_balance(user_id)


def _fixed_numbers(service, numbers=WINNING):
    service.rng.generate_keno_numbers = lambda: list(numbers)


# ============================================================
# Tirage (RNG)
# ============================================================

def test_rng_draws_exactly_20_unique_numbers_between_1_and_80():
    rng = RNGService()
    seen = set()
    for _ in range(3000):
        numbers = rng.generate_keno_numbers()
        assert len(numbers) == 20
        assert len(set(numbers)) == 20
        assert all(1 <= n <= 80 for n in numbers)
        seen.update(numbers)
    assert seen == set(range(1, 81))  # tous les numéros finissent par sortir


@pytest.mark.parametrize("numbers", [
    list(range(1, 20)),               # 19 numéros
    list(range(1, 22)),               # 21 numéros
    [1] * 2 + list(range(2, 20)),     # doublon
    list(range(0, 20)),               # 0
    list(range(62, 82)),              # 81
])
def test_invalid_draw_result_is_never_paid(numbers):
    with pytest.raises(GameException):
        KenoService.validate_draw_numbers(numbers)


# ============================================================
# Validation des numéros joués et de la mise
# ============================================================

@pytest.mark.parametrize("picks", [
    [], [0], [81], [5, 5], [1.5], ["x"], [True], list(range(1, 12)), "1,2,3", None,
])
def test_invalid_picks_are_rejected(picks):
    with pytest.raises(ValidationException):
        KenoService.validate_picks(picks)


def test_valid_picks_are_sorted_and_normalised():
    assert KenoService.validate_picks([80, 1, "40"]) == [1, 40, 80]
    assert KenoService.validate_picks(list(range(1, 11))) == list(range(1, 11))


@pytest.mark.parametrize("stake", ["9.99", "100000.01", "abc", "10.001", "NaN", None, -10])
def test_invalid_stakes_are_rejected(stake):
    with pytest.raises(ValidationException):
        KenoService.validate_stake(stake)


# ============================================================
# Table de paiement / correspondances
# ============================================================

@pytest.mark.parametrize(
    "picks_count,hits,expected_multiplier",
    [
        (1, 1, Decimal("2.5")), (1, 0, Decimal("0")),
        (3, 3, Decimal("12")), (3, 2, Decimal("1.5")), (3, 0, Decimal("0")),
        (6, 6, Decimal("120")),
        (10, 10, Decimal("5000")), (10, 4, Decimal("0.5")), (10, 3, Decimal("0")),
    ],
)
def test_calculate_winnings_matches_paytable(service, picks_count, hits, expected_multiplier):
    picks = list(range(1, picks_count + 1))
    draw_numbers = list(range(1, hits + 1)) + list(range(61, 61 + (20 - hits)))
    winnings, computed_hits = service._calculate_winnings(picks, draw_numbers, Decimal("100"))
    assert computed_hits == hits
    assert winnings == Decimal("100") * expected_multiplier


def test_every_paytable_entry_is_paid(service):
    for picks_count, row in KenoService.PAYTABLE.items():
        for hits, multiplier in row.items():
            picks = list(range(1, picks_count + 1))
            numbers = list(range(1, hits + 1)) + list(range(61, 61 + 20 - hits))
            assert service._calculate_winnings(picks, numbers, Decimal("10")) == (Decimal("10") * multiplier, hits)


def test_get_multiplier_unknown_picks_count_returns_zero(service):
    assert service._get_multiplier(picks_count=15, hits=5) == Decimal("0")


# ============================================================
# Prise de paris
# ============================================================

@pytest.mark.asyncio
async def test_account_bet_debits_wallet_with_unique_reference(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("500"))
    draw = await _draw(db_session)

    bet = await service.place_bet(user.id, KenoBetCreate(draw_id=draw.id, picks=[7, 3, 15], stake=50))

    assert bet.picks == [3, 7, 15]
    assert bet.status == KenoBetStatus.PENDING
    assert await _balance(db_session, fake_redis, user.id) == Decimal("450")
    tx = (await db_session.execute(select(Transaction).where(Transaction.bet_id == bet.id))).scalar_one()
    assert tx.reference == f"BET-KENO-{bet.id}"
    assert tx.draw_id == draw.id


@pytest.mark.asyncio
@pytest.mark.parametrize("picks,stake", [([0, 5], 50), ([5, 5], 50), (list(range(1, 12)), 50), ([1, 2], 5)])
async def test_invalid_bet_is_rejected_without_debit(service, db_session, fake_redis, make_user, picks, stake):
    user = await make_user(balance=Decimal("500"))
    draw = await _draw(db_session)
    with pytest.raises(ValidationException):
        await service.create_bet(draw_id=draw.id, picks=picks, stake=stake, user_id=user.id)
    assert await _balance(db_session, fake_redis, user.id) == Decimal("500")


@pytest.mark.asyncio
async def test_bet_rejected_when_balance_is_insufficient(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("20"))
    draw = await _draw(db_session)
    with pytest.raises((InsufficientBalanceException, AppException)):
        await service.place_bet(user.id, KenoBetCreate(draw_id=draw.id, picks=[1, 2], stake=50))
    assert await _balance(db_session, fake_redis, user.id) == Decimal("20")


@pytest.mark.asyncio
async def test_bet_rejected_when_betting_is_closed(service, db_session, make_user):
    user = await make_user(balance=Decimal("500"))
    closing = await _draw(db_session, minutes=0)  # heure du tirage atteinte
    with pytest.raises(GameException, match="fermés"):
        await service.create_bet(draw_id=closing.id, picks=[1], stake=10, user_id=user.id)

    almost = await _draw(db_session, minutes=0)
    almost.draw_time = now_utc() + timedelta(seconds=KenoService.BETTING_CLOSE_SECONDS - 2)
    with pytest.raises(GameException, match="fermés"):
        await service.create_bet(draw_id=almost.id, picks=[1], stake=10, user_id=user.id)


@pytest.mark.asyncio
async def test_bet_rejected_on_finished_or_cancelled_draw(service, db_session, make_user):
    user = await make_user(balance=Decimal("500"))
    for status in (KenoDrawStatus.COMPLETED, KenoDrawStatus.CANCELLED):
        draw = await _draw(db_session, status=status)
        with pytest.raises(GameException, match="plus disponible"):
            await service.create_bet(draw_id=draw.id, picks=[1], stake=10, user_id=user.id)


@pytest.mark.asyncio
async def test_bet_must_be_funded_by_exactly_one_source(service, db_session, make_user):
    user = await make_user(balance=Decimal("500"))
    draw = await _draw(db_session)
    with pytest.raises(ValidationException):
        await service.create_bet(draw_id=draw.id, picks=[1], stake=10)
    with pytest.raises(ValidationException):
        await service.create_bet(draw_id=draw.id, picks=[1], stake=10, user_id=user.id, ticket_number="X")


@pytest.mark.asyncio
async def test_ticket_bet_debits_ticket(service, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    number = ticket["ticket_number"] if isinstance(ticket, dict) else ticket.ticket_number
    draw = await _draw(db_session)

    bet = await service.place_bet_with_ticket(number, draw.id, [4, 9], Decimal("30"), agent.id)

    row = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == number))).scalar_one()
    assert row.balance == Decimal("70")
    assert bet.ticket_id == row.id and bet.agent_id == agent.id

    with pytest.raises(InsufficientBalanceException):
        await service.place_bet_with_ticket(number, draw.id, [4, 9], Decimal("80"), agent.id)
    row.status = TicketStatus.PAID if hasattr(TicketStatus, "PAID") else TicketStatus.EXPIRED
    with pytest.raises(GameException, match="inactif"):
        await service.place_bet_with_ticket(number, draw.id, [4, 9], Decimal("10"), agent.id)


@pytest.mark.asyncio
async def test_many_bettors_on_the_same_draw(service, db_session, fake_redis, make_user):
    draw = await _draw(db_session)
    users = [await make_user(balance=Decimal("100")) for _ in range(25)]
    for i, user in enumerate(users):
        await service.create_bet(draw_id=draw.id, picks=[i % 80 + 1], stake=10, user_id=user.id)
    totals = await service.live_totals([draw.id])
    assert totals[draw.id]["total_bets"] == 25
    assert totals[draw.id]["total_amount"] == Decimal("250")


# ============================================================
# Règlement
# ============================================================

@pytest.mark.asyncio
async def test_execute_draw_settles_winners_losers_and_tickets(service, db_session, fake_redis, make_user, make_agent, make_ticket):
    winner = await make_user(balance=Decimal("100"))
    loser = await make_user(balance=Decimal("100"))
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    number = ticket["ticket_number"] if isinstance(ticket, dict) else ticket.ticket_number
    draw = await _draw(db_session)

    win_bet = await service.create_bet(draw_id=draw.id, picks=[1, 2, 3], stake=100, user_id=winner.id)
    lose_bet = await service.create_bet(draw_id=draw.id, picks=[21, 22, 23], stake=50, user_id=loser.id)
    ticket_bet = await service.create_bet(draw_id=draw.id, picks=[5], stake=40, ticket_number=number, agent_id=agent.id)

    _fixed_numbers(service)
    result = await service.execute_draw(draw.id)

    # 3 numéros joués, 3 trouvés -> x12 ; 1 numéro, 1 trouvé -> x2.5
    assert (win_bet.status, win_bet.hits, win_bet.winnings) == (KenoBetStatus.WON, 3, Decimal("1200"))
    assert (lose_bet.status, lose_bet.hits, lose_bet.winnings) == (KenoBetStatus.LOST, 0, Decimal("0"))
    assert (ticket_bet.status, ticket_bet.winnings) == (KenoBetStatus.WON, Decimal("100"))

    assert await _balance(db_session, fake_redis, winner.id) == Decimal("1200")
    assert await _balance(db_session, fake_redis, loser.id) == Decimal("50")
    row = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == number))).scalar_one()
    assert row.balance == Decimal("160")  # 100 - 40 + 100

    win_tx = (await db_session.execute(select(Transaction).where(Transaction.reference == f"WIN-KENO-{win_bet.id}"))).scalar_one()
    assert win_tx.amount == Decimal("1200")

    assert draw.status == KenoDrawStatus.COMPLETED and draw.numbers == WINNING
    assert (draw.total_bets, draw.total_amount, draw.total_payout) == (3, Decimal("190"), Decimal("1300"))
    assert result["winners_count"] == 2 and result["total_payout"] == 1300.0
    assert "winners" not in result  # résultat diffusable tel quel (JSON)


@pytest.mark.asyncio
async def test_settlement_is_never_paid_twice(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("100"))
    draw = await _draw(db_session)
    await service.create_bet(draw_id=draw.id, picks=[1], stake=100, user_id=user.id)
    _fixed_numbers(service)

    await service.execute_draw(draw.id)
    assert await _balance(db_session, fake_redis, user.id) == Decimal("250")

    with pytest.raises(GameException):
        await service.execute_draw(draw.id)            # déjà tiré
    again = await service.settle_bets_for_draw(draw.id)  # relance du règlement
    assert again["settled_bets"] == 0
    assert await _balance(db_session, fake_redis, user.id) == Decimal("250")
    wins = (await db_session.execute(select(func.count(Transaction.id)).where(Transaction.reference.like("WIN-KENO-%")))).scalar()
    assert wins == 1


@pytest.mark.asyncio
async def test_two_winners_are_both_paid(service, db_session, fake_redis, make_user):
    """Régression : l'ancien worker utilisait une référence WIN-<seconde> ;
    deux gagnants dans la même seconde faisaient échouer tout le tirage."""
    users = [await make_user(balance=Decimal("10")) for _ in range(2)]
    draw = await _draw(db_session)
    for user in users:
        await service.create_bet(draw_id=draw.id, picks=[1], stake=10, user_id=user.id)
    _fixed_numbers(service)
    result = await service.execute_draw(draw.id)
    assert result["winners_count"] == 2
    for user in users:
        assert await _balance(db_session, fake_redis, user.id) == Decimal("25")


@pytest.mark.asyncio
async def test_settlement_failure_rolls_back_everything(service, db_session, fake_redis, make_user):
    user = await make_user(balance=Decimal("100"))
    draw = await _draw(db_session)
    bet = await service.create_bet(draw_id=draw.id, picks=[1], stake=100, user_id=user.id)
    await db_session.commit()
    user_id = user.id

    service.rng.generate_keno_numbers = lambda: [1] * 20  # résultat corrompu
    with pytest.raises(GameException):
        await service.execute_draw(draw.id)
    await db_session.rollback()
    await db_session.refresh(draw)
    await db_session.refresh(bet)
    assert draw.status == KenoDrawStatus.PENDING and bet.status == KenoBetStatus.PENDING
    assert await _balance(db_session, fake_redis, user_id) == Decimal("0")


# ============================================================
# Planification / annulation
# ============================================================

@pytest.mark.asyncio
async def test_cancelled_draw_refunds_accounts_and_tickets_once(service, db_session, fake_redis, make_user, make_agent, make_ticket):
    user = await make_user(balance=Decimal("100"))
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))
    draw = await _draw(db_session)
    empty = await _draw(db_session)
    b1 = await service.create_bet(draw_id=draw.id, picks=[1], stake=40, user_id=user.id)
    b2 = await service.create_bet(draw_id=draw.id, picks=[2], stake=30, ticket_number=ticket["ticket_number"], agent_id=agent.id)

    result = await service.cancel_draw(draw.id, by="admin-1")
    again = await service.cancel_draw(draw.id)

    assert result["refunded_bets"] == 2 and again["already_cancelled"] is True
    assert (b1.status, b2.status) == (KenoBetStatus.REFUNDED, KenoBetStatus.REFUNDED)
    assert await _balance(db_session, fake_redis, user.id) == Decimal("100")
    row = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == ticket["ticket_number"]))).scalar_one()
    assert row.balance == Decimal("100")
    refunds = (await db_session.execute(select(Transaction.reference).where(Transaction.reference.like("REFUND-KENO-%")))).scalars().all()
    assert refunds == [f"REFUND-KENO-{b1.id}"]
    assert draw.status == KenoDrawStatus.CANCELLED and draw.total_bets == 0
    with pytest.raises(GameException):
        await service.settle_bets_for_draw(draw.id)  # jamais tiré après annulation

    assert await service.cancel_pending_draws_without_bets() == 1
    assert empty.status == KenoDrawStatus.CANCELLED


@pytest.mark.asyncio
async def test_finished_draw_cannot_be_cancelled(service, db_session):
    draw = await _draw(db_session)
    await service.execute_draw(draw.id)
    with pytest.raises(GameException, match="déjà eu lieu"):
        await service.cancel_draw(draw.id)


@pytest.mark.asyncio
async def test_schedule_draws_only_in_scheduled_mode_aligned_and_in_haiti_hours(service, db_session, fake_redis):
    import json

    from app.core.timezone import to_haiti
    from app.services import keno_engine

    await fake_redis.set("settings:keno", json.dumps(keno_engine.validate_config({"mode": "instant"})))
    assert await service.schedule_draws(hours=24) == 0  # mode instantané : rien à planifier
    await fake_redis.set("settings:keno", json.dumps(keno_engine.validate_config({})))  # partagé (défaut)
    created = await service.schedule_draws(hours=24)
    assert await service.schedule_draws(hours=24) == 0
    draws = (await db_session.execute(select(KenoDraw))).scalars().all()
    assert len(draws) == created > 0
    assert len({d.draw_number for d in draws}) == created
    for d in draws:
        assert d.draw_time.minute % 5 == 0 and d.draw_time.second == 0
        assert KenoService.OPEN_HOUR <= to_haiti(d.draw_time).hour < KenoService.CLOSE_HOUR
        assert d.mode == "scheduled" and d.server_seed_hash
