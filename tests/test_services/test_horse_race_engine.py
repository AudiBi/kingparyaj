# tests/test_services/test_horse_race_engine.py
"""Moteur Horse Races : probabilités, cotes, tirage, scénario, vérification."""

import itertools
from collections import Counter
from decimal import Decimal

import pytest

from app.services import horse_race_engine as engine

NUMBERS = [10, 7, 11, 9, 8, 6]
WEIGHTS = [1 / 3.2, 1 / 4.1, 1 / 6.5, 1 / 2.8, 1 / 7.0, 1 / 10.0]
SEED = "a" * 64


def _probs():
    p = engine.race_probabilities(WEIGHTS, SEED, 1)
    return p, dict(zip(NUMBERS, p))


def test_probabilities_sum_to_one_and_follow_weights():
    p, probs = _probs()
    assert sum(p) == pytest.approx(1.0)
    assert probs[9] > probs[11] > probs[6]  # 2.80 favori, 10.00 outsider


def test_probabilities_need_six_positive_weights():
    with pytest.raises(ValueError):
        engine.race_probabilities(WEIGHTS[:5], SEED, 1)
    with pytest.raises(ValueError):
        engine.race_probabilities([1, 1, 1, 1, 1, 0], SEED, 1)


@pytest.mark.parametrize("bet_type,size", [("EXACTA", 2), ("TRIFECTA", 3)])
def test_ordered_bets_cover_every_outcome_exactly_once(bet_type, size):
    _, probs = _probs()
    total = sum(engine.selection_probability(probs, bet_type, s) for s in itertools.permutations(NUMBERS, size))
    assert total == pytest.approx(1.0)


def test_place_one_position_is_sum_of_win_probabilities():
    _, probs = _probs()
    assert engine.selection_probability(probs, "PLACE", [10, 7]) == pytest.approx(probs[10] + probs[7])
    assert engine.selection_probability(probs, "PLACE", [10]) == pytest.approx(probs[10])


def test_place_two_positions_singles_sum_to_two():
    _, probs = _probs()
    assert sum(engine.selection_probability(probs, "PLACE", [n], 2) for n in NUMBERS) == pytest.approx(2.0)


def test_odds_apply_margin_and_round_down():
    assert engine.odds_from_probability(0.25, 0.15) == Decimal("3.40")
    assert engine.odds_from_probability(1 / 3, 0.15) == Decimal("2.55")  # 2.5500…01 -> 2.55, jamais arrondi au-dessus


@pytest.mark.parametrize("bet_type,selection,error", [
    ("WIN", [10, 7], "1 concurrent"),
    ("EXACTA", [10], "2 concurrent"),
    ("TRIFECTA", [10, 7, 7], "deux fois"),
    ("PLACE", NUMBERS, "1 à 5"),
    ("WIN", [99], "inconnu"),
    ("SUPERFECTA", [10], "invalide"),
])
def test_invalid_selections_are_rejected(bet_type, selection, error):
    with pytest.raises(ValueError, match=error):
        engine.validate_selection(bet_type, selection, NUMBERS)


def test_place_selection_is_normalised_unordered():
    assert engine.validate_selection("PLACE", [7, 10], NUMBERS) == [7, 10]
    assert engine.validate_selection("PLACE", [10, 7], NUMBERS) == [7, 10]
    assert engine.validate_selection("EXACTA", [10, 7], NUMBERS) == [10, 7]  # ordre conservé


@pytest.mark.parametrize("bet_type,selection,expected", [
    ("WIN", [10], True), ("WIN", [7], False),
    ("PLACE", [6, 10], True), ("PLACE", [7, 11], False),
    ("EXACTA", [10, 7], True), ("EXACTA", [7, 10], False),
    ("TRIFECTA", [10, 7, 11], True), ("TRIFECTA", [10, 11, 7], False),
])
def test_is_winning(bet_type, selection, expected):
    result = [10, 7, 11, 9, 8, 6]
    assert engine.is_winning(bet_type, selection, result, place_positions=1) is expected


def test_finishing_order_is_deterministic_and_complete():
    p, _ = _probs()
    first = engine.draw_finishing_order(NUMBERS, p, SEED, 1, 0)
    assert first == engine.draw_finishing_order(NUMBERS, p, SEED, 1, 0)
    assert sorted(first) == sorted(NUMBERS)
    others = {tuple(engine.draw_finishing_order(NUMBERS, p, f"{i:064x}", 1, 0)) for i in range(20)}
    assert len(others) > 1  # un autre seed donne une autre course


def test_finishing_order_follows_probabilities():
    p, probs = _probs()
    n = 6000
    winners = Counter(engine.draw_finishing_order(NUMBERS, p, SEED, 1, nonce)[0] for nonce in range(n))
    for number in NUMBERS:
        assert winners[number] / n == pytest.approx(probs[number], abs=0.03)


def test_race_script_matches_finishing_order():
    p, _ = _probs()
    order = engine.draw_finishing_order(NUMBERS, p, SEED, 3, 0)
    script = engine.build_race_script(order, SEED, 3, 0, duration_ms=25000)

    finish = script["finish_ms"]
    assert [int(k) for k in sorted(finish, key=finish.get)] == order  # le 1er franchit la ligne en premier
    assert all(v == 1.0 for v in script["frames"][-1]["progress"].values())
    assert all(v == 0.0 for v in script["frames"][0]["progress"].values())  # départ identique
    for number in map(str, NUMBERS):  # jamais de recul
        values = [f["progress"][number] for f in script["frames"]]
        assert values == sorted(values)


def test_verify_detects_tampering():
    p, _ = _probs()
    order = engine.draw_finishing_order(NUMBERS, p, SEED, 5, 0)
    ok = engine.verify_race(SEED, engine.seed_hash(SEED), NUMBERS, p, 5, 0, order)
    assert ok["seed_hash_valid"] and ok["result_valid"]

    tampered = list(reversed(order))
    bad = engine.verify_race(SEED, engine.seed_hash(SEED), NUMBERS, p, 5, 0, tampered)
    assert not bad["result_valid"]
    assert not engine.verify_race("c" * 64, engine.seed_hash(SEED), NUMBERS, p, 5, 0, order)["seed_hash_valid"]


# ------------------------------------------------------------
# Cotes gagnant aléatoires dans une fourchette (4.14 – 7.79)
# ------------------------------------------------------------

def test_random_win_odds_stay_in_range_keep_margin_and_vary():
    from decimal import Decimal

    seen = set()
    for round_number in range(1, 301):
        seed = engine.new_server_seed()
        odds, probs = engine.random_win_odds(seed, round_number, 0, 4.14, 7.79, 0.15)
        assert len(odds) == 6
        assert all(Decimal("4.14") <= o <= Decimal("7.79") for o in odds)
        assert abs(sum(probs) - 1) < 1e-9
        # marge maison identique pour chaque cheval (arrondi à l'inférieur : ≥ 15 %)
        for o, p in zip(odds, probs):
            assert 0.15 - 1e-9 <= 1 - p * float(o) < 0.16
        # la cote la plus basse correspond au favori
        assert probs[odds.index(min(odds))] == max(probs)
        seen.update(odds)
    assert len(seen) > 150  # les cotes changent d'une course à l'autre


def test_random_win_odds_are_deterministic_from_seed():
    a = engine.random_win_odds("abc", 7, 0, 4.14, 7.79, 0.15)
    b = engine.random_win_odds("abc", 7, 0, 4.14, 7.79, 0.15)
    assert a == b


def test_random_win_odds_reject_impossible_margin():
    with pytest.raises(ValueError):
        engine.random_win_odds("abc", 1, 0, 4.14, 7.79, 0.40)  # max 0.31
