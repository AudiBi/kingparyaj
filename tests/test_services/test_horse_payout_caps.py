# tests/test_services/test_horse_payout_caps.py
"""Courses : gain maximum par pari et mise maximum des paris exotiques (EXACTA / TRIFECTA)."""

import json
from decimal import Decimal

import pytest

from app.core.exceptions import GameException, ValidationException
from app.models.game import GameBet
from app.services.horse_race_service import CONFIG_KEY, DEFAULT_CONFIG, HorseRaceService, validate_config

AGENT = "agent-guichet"


@pytest.fixture
def service(db_session, fake_redis):
    return HorseRaceService(db_session, fake_redis)


def test_defaults_and_validation():
    config = validate_config({})
    assert config["max_payout"] == 50000 and config["exotic_max_bet"] == 1000
    with pytest.raises(ValidationException, match="gain maximum"):
        validate_config({"max_payout": 5000, "max_bet": 10000})
    with pytest.raises(ValidationException, match="EXACTA / TRIFECTA"):
        validate_config({"exotic_max_bet": 20000})
    with pytest.raises(ValidationException, match="EXACTA / TRIFECTA"):
        validate_config({"exotic_max_bet": 5, "min_bet": 10})


@pytest.mark.asyncio
async def test_round_freezes_the_caps(service):
    race = await service.create_race()
    assert race.config["max_payout"] == 50000 and race.config["exotic_max_bet"] == 1000
    data = service.serialize_race(race)
    assert data["max_payout"] == 50000 and data["exotic_max_bet"] == 1000


@pytest.mark.asyncio
async def test_exotic_bets_have_a_lower_maximum_stake(service, make_user):
    user = await make_user(balance=Decimal("100000"))
    race = await service.create_race()
    first, second, third = [p["player_number"] for p in race.participants[:3]]
    await service.place_bet(race.id, "WIN", [first], "5000", user_id=user.id, agent_id=AGENT)  # WIN : jusqu'à 10 000
    with pytest.raises(GameException, match="Max : 1000"):
        await service.place_bet(race.id, "TRIFECTA", [first, second, third], "1500", user_id=user.id, agent_id=AGENT)
    with pytest.raises(GameException, match="Max : 1000"):
        await service.place_bet(race.id, "EXACTA", [first, second], "1001", user_id=user.id, agent_id=AGENT)
    bet = await service.place_bet(race.id, "EXACTA", [first, second], "1000", user_id=user.id, agent_id=AGENT)
    assert bet.stake == 1000


@pytest.mark.asyncio
async def test_payout_is_capped_for_potential_and_settlement(service, make_user, db_session):
    user = await make_user(balance=Decimal("100000"))
    await service.save_config({**DEFAULT_CONFIG, "max_payout": 12000, "max_bet": 10000})
    race = await service.create_race()
    first = race.participants[0]["player_number"]
    bet = await service.place_bet(race.id, "WIN", [first], "10000", user_id=user.id, agent_id=AGENT)
    assert bet.odds > Decimal("1.2")
    assert bet.potential_payout == Decimal("12000.00")          # 10 000 x cote > 12 000 : plafonné

    race.result = [first] + [p["player_number"] for p in race.participants if p["player_number"] != first]
    assert service.bet_winnings(bet, race) == Decimal("12000.00")  # le règlement applique le même plafond


@pytest.mark.asyncio
async def test_rounds_created_before_the_caps_keep_their_rules(service, make_user):
    user = await make_user(balance=Decimal("100000"))
    race = await service.create_race()
    race.config = {k: v for k, v in race.config.items() if k not in ("max_payout", "exotic_max_bet")}  # ancienne manche
    first, second, third = [p["player_number"] for p in race.participants[:3]]
    bet = await service.place_bet(race.id, "TRIFECTA", [first, second, third], "5000", user_id=user.id, agent_id=AGENT)
    assert bet.stake == 5000                                    # pas de plafond sur une manche déjà ouverte
    assert bet.potential_payout == (Decimal("5000") * bet.odds).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
