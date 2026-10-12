# tests/test_services/test_manual_launch.py
"""Lancement MANUEL des jeux (Keno, Lucky6, Horse Races) : le tirage n'a pas d'heure,
il reste ouvert tant qu'un agent ne le lance pas, pour vendre autant de fiches que voulu."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.exceptions import GameException
from app.core.timezone import now_utc
from app.models.enums import KenoBetStatus, KenoDrawStatus
from app.models.game import GameBet, GameRound
from app.models.keno import KenoBet, KenoDraw
from app.services import keno_engine, lucky6_engine
from app.services.horse_race_service import DEFAULT_CONFIG as HR_DEFAULT, HorseRaceService
from app.services.keno_service import CONFIG_KEY as KENO_KEY, KenoService
from app.services.lucky6_service import Lucky6Service
from app.services.wallet_service import WalletService

AGENT = "agent-guichet"
PICKS = [3, 11, 17, 24, 30, 45]


def test_manual_launch_is_the_default_for_every_game():
    assert keno_engine.validate_config({})["manual_launch"] is True
    assert lucky6_engine.validate_config({})["manual_launch"] is True
    assert HR_DEFAULT["manual_launch"] is True
    # le mode « à l'heure » reste possible (option)
    assert keno_engine.validate_config({"manual_launch": False})["manual_launch"] is False


def test_draw_longer_than_interval_only_matters_for_clock_draws():
    long_draw = {"interval_minutes": 2, "ball_interval_ms": 9000}
    with pytest.raises(keno_engine.KenoConfigError, match="plus longtemps"):
        keno_engine.validate_config({**long_draw, "manual_launch": False})
    assert keno_engine.validate_config(long_draw)["manual_launch"] is True  # sans heure : l'intervalle ne sert à rien


# ======================================================================
# Jeux à manches : Lucky6 et Horse Races
# ======================================================================

@pytest.fixture(params=["lucky6", "horse"])
def game(request, db_session, fake_redis, monkeypatch):
    """(service, mise à jouer) pour chaque jeu à manches."""
    if request.param == "lucky6":
        monkeypatch.setattr(Lucky6Service, "_within_opening_hours", staticmethod(lambda config: True))
        return Lucky6Service(db_session, fake_redis), ("SIX", PICKS)
    monkeypatch.setattr(HorseRaceService, "_within_opening_hours", staticmethod(lambda config: True))
    return HorseRaceService(db_session, fake_redis), ("WIN", [10])


async def _bet(service, race, play, user, stake="10"):
    kind, selection = play
    return await service.place_bet(race.id, kind, selection, stake, user_id=user.id, agent_id=AGENT)


@pytest.mark.asyncio
async def test_round_has_no_time_and_waits_for_an_agent(game, make_user):
    service, play = game
    user = await make_user(balance=Decimal("1000"))
    assert (await service.tick())["created"] == 1
    race = await service.get_open_race()
    assert race.status == "BETTING_OPEN" and race.betting_closes_at is None
    assert race.config["manual_launch"] is True and service.serialize_race(race)["manual"] is True

    # des heures plus tard : ni fermeture, ni départ automatiques, et on vend toujours
    later = now_utc() + timedelta(hours=6)
    counts = await service.tick(later)
    assert counts["closed"] == 0 and counts["started"] == 0 and counts["created"] == 0
    assert race.status == "BETTING_OPEN"
    for _ in range(5):
        await _bet(service, race, play, user)
    assert (await service.bet_totals([race.id]))[race.id]["total_bets"] == 5
    assert "total_bets" not in (await service.live_state())["betting_race"]  # écrans publics
    assert (await service.live_state(with_totals=True))["betting_race"]["total_bets"] == 5  # panel agent


@pytest.mark.asyncio
async def test_launch_closes_betting_draws_and_pays_once(game, make_user, db_session, fake_redis):
    service, play = game
    user = await make_user(balance=Decimal("100"))
    race = await service.create_race()
    bet = await _bet(service, race, play, user)

    launched = await service.launch_race(race.id, by=AGENT)
    assert launched.status == "RUNNING" and launched.result and launched.started_at is not None
    assert launched.betting_closes_at is not None and launched.scheduled_at == launched.started_at
    with pytest.raises(GameException, match="ferm|lanc|Tirage en cours"):
        await _bet(service, race, play, user)

    with pytest.raises(GameException):  # deuxième clic (autre agent)
        await service.launch_race(race.id, by="autre-agent")

    # fin de l'animation puis règlement par le cycle ; la manche suivante s'ouvre
    done = launched.started_at + timedelta(milliseconds=service.round_duration_ms(launched) + 1000)
    counts = await service.tick(done)
    assert counts["finished"] == 1 and counts["settled"] == 1 and counts["created"] == 1
    await db_session.refresh(bet)
    assert bet.status in ("WON", "LOST")
    nxt = await service.get_open_race()
    assert nxt.id != race.id and nxt.betting_closes_at is None
    assert await service.tick(done + timedelta(seconds=3)) == {k: 0 for k in counts}  # rien de plus


@pytest.mark.asyncio
async def test_cannot_launch_while_previous_draw_is_still_running(game, make_user):
    service, play = game
    user = await make_user(balance=Decimal("1000"))
    first = await service.create_race()
    await _bet(service, first, play, user)
    await service.launch_race(first.id, by=AGENT)
    second = await service.create_race()
    await _bet(service, second, play, user)
    with pytest.raises(GameException, match="encore en cours"):
        await service.launch_race(second.id, by=AGENT)
    assert second.status == "BETTING_OPEN"  # rien n'a bougé : on peut relancer plus tard
    await service.tick(first.started_at + timedelta(milliseconds=service.round_duration_ms(first) + 1000))
    assert (await service.launch_race(second.id, by=AGENT)).status == "RUNNING"


@pytest.mark.asyncio
async def test_only_an_agent_who_sold_can_launch_the_round(game, make_user):
    service, play = game
    user = await make_user(balance=Decimal("1000"))
    race = await service.create_race()
    with pytest.raises(GameException, match="a vendu"):  # personne n'a vendu
        await service.launch_race(race.id, by=AGENT)
    await _bet(service, race, play, user)  # vendu par AGENT
    with pytest.raises(GameException, match="a vendu"):  # un autre agent, qui n'a rien vendu ici
        await service.launch_race(race.id, by="autre-agent")
    assert race.status == "BETTING_OPEN"
    # un second vendeur peut lui aussi lancer
    kind, selection = play
    await service.place_bet(race.id, kind, selection, "10", user_id=user.id, agent_id="autre-agent")
    assert (await service.launch_race(race.id, by="autre-agent")).status == "RUNNING"
    my = (await service.live_state(with_totals=True, agent_id="autre-agent"))["race"]
    assert my["my_bets"] == 1


@pytest.mark.asyncio
async def test_admin_timed_round_still_starts_on_the_clock(game):
    service, _ = game
    race = await service.create_race(scheduled_at=now_utc() + timedelta(minutes=3))
    assert race.config["manual_launch"] is False and race.betting_closes_at is not None
    counts = await service.tick(race.scheduled_at)
    assert counts["started"] == 1 and race.status == "RUNNING"


@pytest.mark.asyncio
async def test_clock_mode_is_an_option(game, fake_redis):
    service, _ = game
    config = await service.get_config()
    await fake_redis.set(service.CONFIG_KEY, json.dumps({**config, "manual_launch": False}))
    await service.tick()
    race = await service.get_open_race()
    assert race.config["manual_launch"] is False and race.betting_closes_at is not None


# ======================================================================
# Keno partagé
# ======================================================================

@pytest.fixture
async def keno(db_session, fake_redis):
    await fake_redis.set(KENO_KEY, json.dumps(keno_engine.validate_config({"open_hour": 0, "close_hour": 24})))
    return KenoService(db_session, fake_redis)


async def _ticket(keno, draw, user, picks=(1, 2, 3), stake=10):
    return await keno.create_bet(draw_id=draw.id, picks=list(picks), stake=stake, user_id=user.id, agent_id=AGENT)


@pytest.mark.asyncio
async def test_keno_keeps_one_open_draw_without_time(keno, make_user, db_session):
    user = await make_user(balance=Decimal("1000"))
    assert (await keno.tick())["created"] == 1
    assert (await keno.tick())["created"] == 0  # un seul tirage ouvert à la fois
    draw = (await db_session.execute(select(KenoDraw).where(KenoDraw.status == KenoDrawStatus.PENDING))).scalar_one()
    assert draw.config["manual_launch"] is True and keno.betting_closes_at(draw) is None

    # des heures plus tard : jamais tiré tout seul, on vend toujours
    later = now_utc() + timedelta(hours=6)
    outcome = await keno.tick(later)
    assert outcome["settled"] == [] and outcome["created"] == 0
    for _ in range(4):
        await _ticket(keno, draw, user)
    public = await keno.live_state(later)  # écrans publics : jamais les totaux de la maison
    assert "total_bets" not in public["betting_draw"] and "total_amount" not in public["betting_draw"]
    state = await keno.live_state(later, with_totals=True)  # panel agent
    assert state["betting_draw"]["draw_id"] == draw.id
    assert state["betting_draw"]["manual"] is True and state["betting_draw"]["betting_closes_at"] is None
    assert state["betting_draw"]["total_bets"] == 4 and state["betting_draw"]["total_amount"] == 40


@pytest.mark.asyncio
async def test_keno_launch_settles_tickets_once(keno, make_user, db_session, fake_redis):
    user = await make_user(balance=Decimal("100"))
    await keno.tick()
    draw = (await db_session.execute(select(KenoDraw).where(KenoDraw.status == KenoDrawStatus.PENDING))).scalar_one()
    bet = await _ticket(keno, draw, user, stake=20)
    await db_session.commit()

    result = await keno.launch_draw(draw.id, by=AGENT)
    assert result["just_drawn"] and result["total_bets"] == 1 and len(result["numbers"]) == 20
    await db_session.refresh(draw)
    await db_session.refresh(bet)
    assert draw.status == KenoDrawStatus.COMPLETED and abs(draw.draw_time - draw.closed_at) < timedelta(seconds=2)  # heure réelle du tirage
    assert bet.status in (KenoBetStatus.WON, KenoBetStatus.LOST)
    balance = await WalletService(db_session, fake_redis).get_balance(user.id)
    assert balance == Decimal("80") + (bet.winnings or 0)

    with pytest.raises(GameException, match="déjà"):  # deuxième clic
        await keno.launch_draw(draw.id, by=AGENT)
    assert await WalletService(db_session, fake_redis).get_balance(user.id) == balance  # payé une seule fois

    # le cycle ouvre le tirage suivant et ne retire jamais le tirage fini
    outcome = await keno.tick()
    assert outcome["created"] == 1 and outcome["settled"] == []


@pytest.mark.asyncio
async def test_keno_only_an_agent_who_sold_can_launch(keno, make_user, db_session):
    user = await make_user(balance=Decimal("1000"))
    draw = await keno.generate_draw(config=await keno.get_config())
    with pytest.raises(GameException, match="a vendu"):  # personne n'a vendu
        await keno.launch_draw(draw.id, by=AGENT)
    await _ticket(keno, draw, user)  # vendu par AGENT
    with pytest.raises(GameException, match="a vendu"):  # un autre agent
        await keno.launch_draw(draw.id, by="autre-agent")
    await db_session.refresh(draw)
    assert draw.status == KenoDrawStatus.PENDING
    assert (await keno.live_state(with_totals=True, agent_id=AGENT))["betting_draw"]["my_bets"] == 1
    assert (await keno.live_state(agent_id="autre-agent"))["betting_draw"]["my_bets"] == 0
    assert (await keno.launch_draw(draw.id, by=AGENT))["just_drawn"]


@pytest.mark.asyncio
async def test_keno_cannot_launch_while_animation_is_running(keno, make_user, db_session):
    user = await make_user(balance=Decimal("1000"))
    first = await keno.generate_draw(config=await keno.get_config())
    await _ticket(keno, first, user)
    await keno.launch_draw(first.id, by=AGENT)
    second = await keno.generate_draw(config=await keno.get_config())
    await _ticket(keno, second, user)
    with pytest.raises(GameException, match="encore en cours"):
        await keno.launch_draw(second.id, by=AGENT)
    await db_session.refresh(second)
    assert second.status == KenoDrawStatus.PENDING

    first.closed_at = first.closed_at - timedelta(minutes=5)  # animation terminée
    await db_session.flush()
    assert (await keno.launch_draw(second.id, by=AGENT))["just_drawn"]


@pytest.mark.asyncio
async def test_keno_stale_cleanup_keeps_the_open_draw(keno, db_session):
    draw = await keno.generate_draw(draw_time=now_utc() - timedelta(hours=3), config=await keno.get_config())
    assert await keno.cancel_pending_draws_without_bets(older_than=now_utc() - timedelta(hours=1)) == 0
    await db_session.refresh(draw)
    assert draw.status == KenoDrawStatus.PENDING


@pytest.mark.asyncio
async def test_keno_admin_change_does_not_turn_open_draw_into_a_clock_draw(keno, db_session):
    await keno.tick()
    draw = (await db_session.execute(select(KenoDraw).where(KenoDraw.status == KenoDrawStatus.PENDING))).scalar_one()
    await keno.save_config({**await keno.get_config(), "manual_launch": False})
    await db_session.refresh(draw)
    assert draw.config["manual_launch"] is True
    assert (await keno.tick(now_utc() + timedelta(hours=1)))["settled"] == []  # ne part pas seul


@pytest.mark.asyncio
async def test_keno_clock_mode_is_an_option(db_session, fake_redis):
    await fake_redis.set(KENO_KEY, json.dumps(keno_engine.validate_config({"open_hour": 0, "close_hour": 24, "manual_launch": False})))
    service = KenoService(db_session, fake_redis)
    assert (await service.tick())["created"] == 2  # deux prochains créneaux calés sur l'horloge
    draws = (await db_session.execute(select(KenoDraw))).scalars().all()
    assert all(d.config["manual_launch"] is False and service.betting_closes_at(d) is not None for d in draws)
