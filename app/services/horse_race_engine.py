# app/services/horse_race_engine.py
"""
Moteur Horse Races : calculs purs, sans base de données.

- Probabilités des concurrents (poids de configuration + variation par course)
- Cotes de chaque type de pari (modèle de Harville), marge appliquée
- Tirage du classement (Plackett-Luce) à partir d'un seed secret (HMAC-SHA256)
- Scénario d'animation (points de passage) cohérent avec le classement
- Vérification (« provably fair ») : seed révélé -> même résultat

Tout est déterministe à partir de (server_seed, round_number, nonce) :
le serveur publie sha256(server_seed) à l'ouverture des paris et révèle le
seed après l'arrivée ; n'importe qui peut alors recalculer la course.
"""

from __future__ import annotations

import itertools
from decimal import ROUND_DOWN, Decimal
from typing import Dict, Iterable, List, Sequence

RUNNERS_PER_RACE = 6

BET_SELECTION_SIZE = {
    "WIN": (1, 1),
    "PLACE": (1, RUNNERS_PER_RACE - 1),  # plusieurs chevaux possibles, pas tous
    "EXACTA": (2, 2),
    "TRIFECTA": (3, 3),
}

MIN_ODDS = Decimal("1.01")


# ============================================================
# Seed / hasard déterministe
# ============================================================

from app.services.fairness import hmac_uniform, new_server_seed, seed_hash  # noqa: E402,F401  (mécanisme commun)


def _uniform(server_seed: str, round_number: int, nonce: int, label: str, index: int) -> float:
    """Nombre uniforme dans [0, 1) dérivé du seed (inchangé : mêmes résultats qu'avant)."""
    return hmac_uniform(server_seed, f"{round_number}:{nonce}:{label}:{index}")


# ============================================================
# Probabilités
# ============================================================

def race_probabilities(
    weights: Sequence[float],
    server_seed: str,
    round_number: int,
    nonce: int = 0,
    variation: float = 0.15,
) -> List[float]:
    """Probabilités de victoire de la course : poids configurés, variés de
    ±`variation` (déterministe, pour que chaque course diffère), normalisés."""
    if len(weights) != RUNNERS_PER_RACE or any(w <= 0 for w in weights):
        raise ValueError("6 poids strictement positifs sont requis")
    varied = [
        w * (1 + variation * (2 * _uniform(server_seed, round_number, nonce, "strength", i) - 1))
        for i, w in enumerate(weights)
    ]
    total = sum(varied)
    return [v / total for v in varied]


def odds_margin_bounds(odds_min: float, odds_max: float, runners: int = RUNNERS_PER_RACE) -> tuple[float, float]:
    """Marges atteignables quand chaque cote gagnant reste dans [odds_min, odds_max] :
    toutes au minimum -> marge max, toutes au maximum -> marge min."""
    return 1 - odds_max / runners, 1 - odds_min / runners


def random_win_odds(
    server_seed: str,
    round_number: int,
    nonce: int,
    odds_min: float,
    odds_max: float,
    margin: float,
    runners: int = RUNNERS_PER_RACE,
) -> tuple[List[Decimal], List[float]]:
    """Cotes gagnant aléatoires dans [odds_min, odds_max] et probabilités associées.

    1. Une cote uniforme par concurrent dans la fourchette (déterministe : seed).
    2. Ajustement en gardant la fourchette, pour que la somme des 1/cote vaille
       1 / (1 - marge) : la marge maison est la même pour chaque cheval et pour
       chaque course (sans cela, des cotes au hasard pourraient faire perdre la maison).
    3. Probabilités de victoire = (1 / cote) normalisées : ce sont elles qui
       servent au tirage du classement, donc cotes et résultat restent cohérents.
    """
    if not 1 < odds_min < odds_max:
        raise ValueError("Fourchette de cotes invalide")
    low, high = odds_margin_bounds(odds_min, odds_max, runners)
    if not low <= margin <= high:
        raise ValueError(f"Marge impossible avec ces cotes (entre {max(low, 0):.2f} et {high:.2f})")
    span = odds_max - odds_min
    base = [odds_min + span * _uniform(server_seed, round_number, nonce, "odds", i) for i in range(runners)]
    target = 1 / (1 - margin)

    def inv_sum(values: Sequence[float]) -> float:
        return sum(1 / v for v in values)

    if inv_sum(base) < target:
        # cotes trop généreuses : on les rapproche du minimum
        shift = lambda k: [odds_min + (o - odds_min) * k for o in base]  # k=0 -> tout au min
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if inv_sum(shift(mid)) >= target:
                lo = mid
            else:
                hi = mid
        values = shift(lo)
    else:
        # cotes trop basses : on les rapproche du maximum
        shift = lambda k: [o + (odds_max - o) * k for o in base]  # k=1 -> tout au max
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if inv_sum(shift(mid)) >= target:
                lo = mid
            else:
                hi = mid
        values = shift(lo)

    floor, ceil = Decimal(str(odds_min)), Decimal(str(odds_max))
    odds = [
        min(max(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_DOWN), floor), ceil)
        for v in values
    ]
    inverse = [1 / float(o) for o in odds]
    total = sum(inverse)
    return odds, [x / total for x in inverse]


def _harville_order_probability(probs: Dict[int, float], order: Sequence[int]) -> float:
    """P(les concurrents de `order` arrivent exactement dans cet ordre en tête)."""
    remaining = 1.0
    p = 1.0
    for runner in order:
        if remaining <= 0:
            return 0.0
        p *= probs[runner] / remaining
        remaining -= probs[runner]
    return p


def selection_probability(
    probs: Dict[int, float],
    bet_type: str,
    selection: Sequence[int],
    place_positions: int = 1,
) -> float:
    """Probabilité de gain d'un pari, selon le modèle de Harville.

    probs : {player_number: probabilité de victoire}
    """
    if bet_type == "WIN":
        return probs[selection[0]]
    if bet_type in ("EXACTA", "TRIFECTA"):
        return _harville_order_probability(probs, selection)
    if bet_type == "PLACE":
        chosen = set(selection)
        runners = list(probs)
        if place_positions <= 1:
            return sum(probs[r] for r in chosen)
        # P(au moins un choisi dans les `place_positions` premiers) =
        # 1 - P(aucun choisi dans le top N) (énumération exacte, 6 partants)
        miss = 0.0
        others = [r for r in runners if r not in chosen]
        for top in itertools.permutations(others, place_positions):
            miss += _harville_order_probability(probs, top)
        return 1.0 - miss
    raise ValueError(f"Type de pari inconnu : {bet_type}")


def odds_from_probability(probability: float, margin: float) -> Decimal:
    """Cote = (1 - marge) / probabilité, arrondie à l'inférieur au centième."""
    if probability <= 0:
        raise ValueError("Probabilité nulle")
    raw = Decimal(str((1 - margin) / probability))
    return raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def bet_odds(
    probs: Dict[int, float],
    bet_type: str,
    selection: Sequence[int],
    margin: float,
    place_positions: int = 1,
) -> Decimal:
    return odds_from_probability(selection_probability(probs, bet_type, selection, place_positions), margin)


def validate_selection(bet_type: str, selection: Iterable[int], runner_numbers: Iterable[int]) -> List[int]:
    """Contrôle et normalise une sélection. Lève ValueError avec un message
    lisible (traduit en GameException par le service)."""
    if bet_type not in BET_SELECTION_SIZE:
        raise ValueError(f"Type de pari invalide : {bet_type}")
    try:
        chosen = [int(n) for n in selection]
    except (TypeError, ValueError):
        raise ValueError("Sélection invalide")
    low, high = BET_SELECTION_SIZE[bet_type]
    if not low <= len(chosen) <= high:
        expected = str(low) if low == high else f"{low} à {high}"
        raise ValueError(f"{bet_type} : {expected} concurrent(s) attendu(s)")
    if len(set(chosen)) != len(chosen):
        raise ValueError("Un même concurrent ne peut être choisi deux fois")
    valid = set(runner_numbers)
    unknown = [n for n in chosen if n not in valid]
    if unknown:
        raise ValueError(f"Concurrent(s) inconnu(s) dans cette course : {unknown}")
    if bet_type == "PLACE":
        chosen = sorted(chosen)  # l'ordre n'a pas d'importance
    return chosen


def is_winning(bet_type: str, selection: Sequence[int], result: Sequence[int], place_positions: int = 1) -> bool:
    """Le pari est-il gagnant pour ce classement (liste des numéros, 1er en tête) ?"""
    if bet_type == "WIN":
        return result[0] == selection[0]
    if bet_type == "PLACE":
        return bool(set(selection) & set(result[:max(1, place_positions)]))
    if bet_type == "EXACTA":
        return list(result[:2]) == list(selection)
    if bet_type == "TRIFECTA":
        return list(result[:3]) == list(selection)
    raise ValueError(f"Type de pari inconnu : {bet_type}")


# ============================================================
# Classement
# ============================================================

def draw_finishing_order(
    runner_numbers: Sequence[int],
    probabilities: Sequence[float],
    server_seed: str,
    round_number: int,
    nonce: int = 0,
) -> List[int]:
    """Classement complet (Plackett-Luce) : on tire le 1er selon les
    probabilités, puis le 2e parmi les restants, etc."""
    remaining = list(zip(runner_numbers, probabilities))
    order: List[int] = []
    position = 0
    while remaining:
        total = sum(p for _, p in remaining)
        target = _uniform(server_seed, round_number, nonce, "finish", position) * total
        cumulative = 0.0
        chosen_index = len(remaining) - 1
        for i, (_, p) in enumerate(remaining):
            cumulative += p
            if target < cumulative:
                chosen_index = i
                break
        order.append(remaining.pop(chosen_index)[0])
        position += 1
    return order


# ============================================================
# Scénario d'animation
# ============================================================

def build_race_script(
    finishing_order: Sequence[int],
    server_seed: str,
    round_number: int,
    nonce: int = 0,
    duration_ms: int = 25000,
    steps: int = 20,
) -> Dict:
    """Points de passage de chaque concurrent, cohérents avec le classement.

    Chaque concurrent a un temps d'arrivée (le 1er arrive le premier) ; sa
    progression suit une courbe bruitée (accélérations, ralentissements,
    dépassements possibles en cours de course) mais atteint exactement 1.0 à
    son temps d'arrivée. Le navigateur se contente d'interpoler.

    Format : {"duration_ms", "finish_ms": {numéro: ms},
              "frames": [{"t": 0..1, "progress": {numéro: 0..1}}]}
    """
    winner_time = 0.86  # le gagnant franchit la ligne à 86 % de l'animation
    finish_fraction: Dict[int, float] = {}
    t = winner_time
    for position, runner in enumerate(finishing_order):
        if position > 0:
            t += 0.012 + 0.025 * _uniform(server_seed, round_number, nonce, "gap", position)
        finish_fraction[runner] = min(t, 0.995)

    # Bruit propre à chaque concurrent : quelques bosses lissées
    def noise(runner: int, x: float) -> float:
        value = 0.0
        for k in range(1, 4):
            amp = (_uniform(server_seed, round_number, nonce, f"amp{runner}", k) - 0.5) * 0.10 / k
            phase = _uniform(server_seed, round_number, nonce, f"phase{runner}", k)
            # s'annule au départ (x=0) et à l'arrivée (x=1)
            value += amp * _sin_bump(k * x + phase) * x * (1 - x) * 4
        return value

    frames = []
    previous = {r: 0.0 for r in finishing_order}
    for step in range(steps + 1):
        tt = step / steps
        progress = {}
        for runner in finishing_order:
            f = finish_fraction[runner]
            if tt >= f:
                value = 1.0
            else:
                x = tt / f
                value = min(max(x + noise(runner, x), 0.0), 0.999)
            value = max(value, previous[runner])  # jamais de recul
            previous[runner] = value
            progress[str(runner)] = round(value, 4)
        frames.append({"t": round(tt, 4), "progress": progress})

    return {
        "duration_ms": duration_ms,
        "finish_ms": {str(r): int(finish_fraction[r] * duration_ms) for r in finishing_order},
        "frames": frames,
    }


def _sin_bump(x: float) -> float:
    import math
    return math.sin(2 * math.pi * x)


# ============================================================
# Vérification
# ============================================================

def verify_race(
    server_seed: str,
    expected_hash: str,
    runner_numbers: Sequence[int],
    probabilities: Sequence[float],
    round_number: int,
    nonce: int,
    published_result: Sequence[int],
) -> Dict:
    """Recalcule une course terminée à partir du seed révélé."""
    hash_ok = seed_hash(server_seed) == expected_hash
    recomputed = draw_finishing_order(runner_numbers, probabilities, server_seed, round_number, nonce)
    return {
        "seed_hash_valid": hash_ok,
        "result_valid": list(recomputed) == list(published_result),
        "recomputed_result": recomputed,
    }
