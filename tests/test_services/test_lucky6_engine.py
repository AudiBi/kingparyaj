# tests/test_services/test_lucky6_engine.py
"""Lucky6 : tirage vérifiable, position du 6e numéro, taux exact, table, configuration."""

import random
from math import comb

import pytest

from app.core.exceptions import ValidationException
from app.services import lucky6_engine as engine
from app.services.fairness import seed_hash

SEED = "ab" * 32


def test_draw_is_35_unique_balls_deterministic_and_per_round():
    order = engine.draw_order(SEED, 7)
    assert len(order) == 35 and len(set(order)) == 35
    assert all(1 <= n <= 48 for n in order)
    assert engine.draw_order(SEED, 7) == order
    assert engine.draw_order(SEED, 8) != order


def test_verify_draw():
    order = engine.draw_order(SEED, 3)
    ok = engine.verify_draw(SEED, seed_hash(SEED), 3, order)
    assert ok["seed_hash_valid"] and ok["result_valid"]
    swapped = order[:]
    swapped[0], swapped[1] = swapped[1], swapped[0]
    assert not engine.verify_draw(SEED, seed_hash(SEED), 3, swapped)["result_valid"]  # l'ordre compte
    assert not engine.verify_draw(SEED, "0" * 64, 3, order)["seed_hash_valid"]


def test_draw_is_uniform_enough():
    counts = [0] * 49
    for r in range(3000):
        for n in engine.draw_order(SEED, r)[:1]:
            counts[n] += 1
    # 1re boule : 3000 / 48 ≈ 62,5 par numéro
    assert min(counts[1:]) > 25 and max(counts[1:]) < 110


def test_exact_probabilities():
    assert len(engine.POSITIONS) == 30 and engine.POSITIONS[0] == 6 and engine.POSITIONS[-1] == 35
    assert engine.WIN_PROBABILITY == pytest.approx(comb(35, 6) / comb(48, 6))
    assert engine.WIN_PROBABILITY == pytest.approx(0.13227, abs=1e-5)
    assert engine.POSITION_PROBABILITIES[0] == pytest.approx(1 / comb(48, 6))


def test_probabilities_match_simulation():
    rng = random.Random(1)
    wins = 0
    for _ in range(40000):
        balls = rng.sample(range(1, 49), 35)
        if engine.sixth_match_position([1, 2, 3, 4, 5, 6], balls):
            wins += 1
    assert wins / 40000 == pytest.approx(engine.WIN_PROBABILITY, abs=0.006)


def test_default_and_classic_rtp():
    assert engine.rtp(engine.DEFAULT_PAYTABLE) == pytest.approx(0.8506, abs=1e-4)
    assert engine.rtp(engine.CLASSIC_SHAPE) == pytest.approx(1.0194, abs=1e-4)


@pytest.mark.parametrize("balls,expected", [
    ([1, 2, 3, 4, 5, 6] + list(range(7, 36)), 6),
    ([40, 1, 2, 3, 41, 4, 5, 42, 6] + list(range(7, 33)), 9),
    (list(range(7, 42)), None),
    ([1, 2, 3, 4, 5] + list(range(7, 37)), None),
])
def test_sixth_match_position(balls, expected):
    assert engine.sixth_match_position([1, 2, 3, 4, 5, 6], balls) == expected


def test_multiplier_and_payout_cap():
    from decimal import Decimal

    table = engine.DEFAULT_PAYTABLE
    assert engine.multiplier_for_position(table, 6) == Decimal("10000")
    assert engine.multiplier_for_position(table, 35) == Decimal("1")
    assert engine.multiplier_for_position(table, None) == 0
    assert engine.payout(Decimal("100"), Decimal("10000"), 50000) == Decimal("50000")
    assert engine.payout(Decimal("10"), Decimal("1.5"), 50000) == Decimal("15.00")


def test_parity():
    assert engine.first_ball_parity_wins("FIRST_ODD", [7, 2])
    assert not engine.first_ball_parity_wins("FIRST_EVEN", [7, 2])
    assert engine.first_ball_parity_wins("FIRST_EVEN", [48])


@pytest.mark.parametrize("picks,error", [
    ([1, 2, 3, 4, 5], "exactement"), ([1, 2, 3, 4, 5, 5], "qu'une fois"), ([1, 2, 3, 4, 5, 49], "1 à 48"),
    ([0, 1, 2, 3, 4, 5], "1 à 48"), (["a", 1, 2, 3, 4, 5], "invalides"), ("123456", "invalide"),
])
def test_invalid_picks(picks, error):
    with pytest.raises(ValueError, match=error):
        engine.validate_picks(picks)


def test_picks_are_sorted():
    assert engine.validate_picks([48, 3, 17, 1, 9, 22]) == [1, 3, 9, 17, 22, 48]


@pytest.mark.parametrize("target", [0.5, 0.75, 0.85, 0.92])
def test_scale_paytable_never_exceeds_target(target):
    table = engine.scale_paytable(engine.CLASSIC_SHAPE, target)
    assert engine.rtp(table) <= target + 1e-12
    assert engine.rtp(table) >= target - 0.01
    assert all(a >= b for a, b in zip(table, table[1:]))
    assert min(table) >= 1


def test_scale_paytable_too_low_target():
    with pytest.raises(ValidationException, match="trop bas"):
        engine.scale_paytable(engine.CLASSIC_SHAPE, 0.05)


def test_default_config_is_valid():
    config = engine.validate_config({})
    assert config["paytable"] == engine.DEFAULT_PAYTABLE
    assert engine.draw_duration_ms(config) == 4000 + 35 * 2000 + 6000


@pytest.mark.parametrize("change,error", [
    ({"paytable": [1] * 29}, "30 lignes"),
    ({"paytable": [1] * 29 + [2]}, "davantage"),
    ({"paytable": list(engine.CLASSIC_SHAPE)}, "maximum autorisé"),
    ({"paytable": [0] * 30}, "Au moins une"),
    ({"paytable": [0.5] * 30}, "0 \\(non payé\\)"),
    ({"parity_odds": 1.95}, "pair / impair"),
    ({"parity_odds": 1.0}, "pair / impair"),
    ({"max_bet": 200000}, "100000"),
    ({"min_bet": 500, "max_bet": 100}, "Mises"),
    ({"max_payout": 5}, "gain maximum"),
    ({"stake_options": [5]}, "hors des limites"),
    ({"interval_minutes": 2, "ball_interval_ms": 5000}, "plus longtemps"),
    ({"open_hour": 20, "close_hour": 8}, "ouverture"),
    ({"max_rtp": 1.2}, "maximum"),
])
def test_config_validation(change, error):
    with pytest.raises(ValidationException, match=error):
        engine.validate_config(change)


def test_public_config():
    data = engine.public_config(engine.validate_config({}))
    assert data["picks"] == 6 and data["drawn_count"] == 35 and data["total_numbers"] == 48
    assert data["paytable"][0] == {"position": 6, "multiplier": 10000}
    assert "max_rtp" not in data
