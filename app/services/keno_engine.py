# app/services/keno_engine.py
"""
Moteur Keno : calculs purs, sans base de données.

- Tirage de 20 numéros uniques parmi 1-80, déterministe à partir d'un seed
  secret (mécanisme commun app.services.fairness, comme Horse Races)
- Correspondances, multiplicateur, gain (plafonné)
- Taux de redistribution théorique (RTP) exact d'une table de paiement
  (loi hypergéométrique) et ajustement d'une table à un taux cible
- Validation de la configuration modifiable par l'admin
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from math import comb
from typing import Any, Dict, Iterable, List, Sequence

from app.services.fairness import hmac_uniform, seed_hash

TOTAL_NUMBERS = 80
DRAWN_COUNT = 20
HARD_MAX_PICKS = 10          # limite de la base (contrainte keno_bets.picks)
HARD_MAX_STAKE = Decimal("100000")       # contrainte keno_bets.stake <= 100000
HARD_MAX_PAYOUT = Decimal("99999999")    # capacité de keno_bets.winnings (Numeric 10,2)
MAX_MULTIPLIER = Decimal("100000")

# Table de paiement d'origine du projet (multiplicateur de la mise)
DEFAULT_PAYTABLE: Dict[int, Dict[int, Decimal]] = {
    1: {1: Decimal("2.5")},
    2: {2: Decimal("6")},
    3: {3: Decimal("12"), 2: Decimal("1.5")},
    4: {4: Decimal("30"), 3: Decimal("3"), 2: Decimal("1")},
    5: {5: Decimal("60"), 4: Decimal("6"), 3: Decimal("2"), 2: Decimal("0.5")},
    6: {6: Decimal("120"), 5: Decimal("15"), 4: Decimal("4"), 3: Decimal("1.5"), 2: Decimal("0.5")},
    7: {7: Decimal("300"), 6: Decimal("30"), 5: Decimal("8"), 4: Decimal("2"), 3: Decimal("1"), 2: Decimal("0.5")},
    8: {8: Decimal("600"), 7: Decimal("60"), 6: Decimal("15"), 5: Decimal("4"), 4: Decimal("1.5"), 3: Decimal("0.5")},
    9: {9: Decimal("1200"), 8: Decimal("120"), 7: Decimal("30"), 6: Decimal("8"), 5: Decimal("3"), 4: Decimal("1")},
    10: {10: Decimal("5000"), 9: Decimal("500"), 8: Decimal("60"), 7: Decimal("15"), 6: Decimal("5"), 5: Decimal("2"), 4: Decimal("0.5")},
}


class KenoConfigError(ValueError):
    """Configuration invalide (message lisible pour l'admin)."""


# ============================================================
# Tirage déterministe
# ============================================================

def draw_order(server_seed: str, draw_number: int) -> List[int]:
    """Les 20 numéros dans l'ordre où ils sortent (ordre de l'animation).

    Mélange de Fisher-Yates piloté par HMAC-SHA256(seed, "keno:<tirage>:<i>") :
    chaque étape fixe définitivement la case i ; les 20 premières étapes
    donnent les 20 numéros tirés. Même seed + même numéro de tirage = même
    résultat : n'importe qui peut recalculer le tirage une fois le seed révélé."""
    numbers = list(range(1, TOTAL_NUMBERS + 1))
    order: List[int] = []
    for i in range(TOTAL_NUMBERS - 1, TOTAL_NUMBERS - 1 - DRAWN_COUNT, -1):
        j = int(hmac_uniform(server_seed, f"keno:{draw_number}:{i}") * (i + 1))
        numbers[i], numbers[j] = numbers[j], numbers[i]
        order.append(numbers[i])
    return order


def draw_numbers(server_seed: str, draw_number: int) -> List[int]:
    """Les 20 numéros gagnants, triés."""
    return sorted(draw_order(server_seed, draw_number))


def verify_draw(server_seed: str, expected_hash: str, draw_number: int, published: Sequence[int]) -> Dict[str, Any]:
    recomputed = draw_numbers(server_seed, draw_number)
    return {
        "seed_hash_valid": seed_hash(server_seed) == expected_hash,
        "result_valid": sorted(int(n) for n in published) == recomputed,
        "recomputed_numbers": recomputed,
    }


def is_valid_draw(numbers: Sequence[int]) -> bool:
    return (
        len(numbers) == DRAWN_COUNT
        and len(set(numbers)) == DRAWN_COUNT
        and all(1 <= n <= TOTAL_NUMBERS for n in numbers)
    )


# ============================================================
# Gains
# ============================================================

def matches(picks: Iterable[int], numbers: Iterable[int]) -> List[int]:
    return sorted(set(picks) & set(numbers))


def multiplier(paytable: Dict[int, Dict[int, Decimal]], picks_count: int, hits: int) -> Decimal:
    return Decimal(str(paytable.get(picks_count, {}).get(hits, 0)))


def payout(stake: Decimal, mult: Decimal, max_payout: Decimal) -> Decimal:
    """Gain = mise × multiplicateur, arrondi au centime inférieur, plafonné."""
    value = (Decimal(str(stake)) * Decimal(str(mult))).quantize(Decimal("0.01"), ROUND_DOWN)
    return min(value, Decimal(str(max_payout)))


# ============================================================
# Taux de redistribution (RTP)
# ============================================================

def hit_probability(picks_count: int, hits: int) -> float:
    """P(trouver exactement `hits` numéros parmi `picks_count` joués) — 20 tirés sur 80."""
    if hits > picks_count or hits > DRAWN_COUNT:
        return 0.0
    return comb(DRAWN_COUNT, hits) * comb(TOTAL_NUMBERS - DRAWN_COUNT, picks_count - hits) / comb(TOTAL_NUMBERS, picks_count)


def rtp(row: Dict[int, Decimal], picks_count: int) -> float:
    """Taux de redistribution théorique (0.85 = 85 %) d'une ligne de la table."""
    return sum(float(m) * hit_probability(picks_count, h) for h, m in row.items())


def rtp_table(paytable: Dict[int, Dict[int, Decimal]]) -> Dict[int, float]:
    return {spots: round(rtp(row, spots) * 100, 2) for spots, row in sorted(paytable.items())}


def scale_paytable(paytable: Dict[int, Dict[int, Decimal]], target_percent: float) -> Dict[int, Dict[int, Decimal]]:
    """Ajuste chaque ligne au taux cible en gardant ses proportions.
    Multiplicateurs arrondis au dixième inférieur : le taux obtenu est au
    plus égal à la cible (jamais au-dessus)."""
    if not 1 <= target_percent <= 100:
        raise KenoConfigError("Taux cible invalide (1 à 100 %)")
    scaled: Dict[int, Dict[int, Decimal]] = {}
    for spots, row in paytable.items():
        current = rtp(row, spots)
        if current <= 0:
            scaled[spots] = dict(row)
            continue
        factor = Decimal(str(round(target_percent / 100 / current, 9)))
        new_row = {}
        for hits, mult in row.items():
            value = (Decimal(str(mult)) * factor).quantize(Decimal("0.1"), ROUND_DOWN)
            if value > 0:
                new_row[hits] = min(value, MAX_MULTIPLIER)
        scaled[spots] = new_row
    return scaled


# ============================================================
# Configuration
# ============================================================

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    # « scheduled » (par défaut) : tirages PARTAGÉS par tous les bureaux, toutes les N minutes
    # « instant » : un tirage par ticket, au bureau (option)
    "mode": "scheduled",
    "interval_minutes": 5,      # un tirage partagé toutes les N minutes (calé sur l'horloge)
    "betting_close_seconds": 10,  # paris fermés N secondes avant le tirage
    "open_hour": 8,             # heures des tirages partagés (heure d'Haïti)
    "close_hour": 23,
    "intro_ms": 3000,           # écrans : présentation avant la 1re boule
    "ball_interval_ms": 1500,   # écrans : une boule toutes les N ms
    "outro_ms": 8000,           # écrans : résultat affiché avant le tirage suivant
    "min_picks": 1,
    "max_picks": 10,
    "min_bet": 10,
    "max_bet": 100000,
    "stake_options": [10, 25, 50, 100, 500],
    "max_payout": 10000000,     # gain maximum par ticket (HTG)
    "max_rtp": 100,             # aucune ligne au-dessus de ce taux (%)
    "paytable": {str(s): {str(h): str(m) for h, m in row.items()} for s, row in DEFAULT_PAYTABLE.items()},
}


# Réglages figés dans chaque tirage partagé (keno_draws.config) au moment de sa
# création : un changement de l'admin ne touche jamais un tirage qui a des paris.
FROZEN_KEYS = (
    "min_picks", "max_picks", "min_bet", "max_bet", "max_payout", "paytable",
    "betting_close_seconds", "intro_ms", "ball_interval_ms", "outro_ms",
)


def snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    return {k: config[k] for k in FROZEN_KEYS}


def draw_duration_ms(config: Dict[str, Any]) -> int:
    """Durée de l'animation d'un tirage partagé sur les écrans."""
    return int(config.get("intro_ms", 3000)) + DRAWN_COUNT * int(config.get("ball_interval_ms", 1500)) + int(config.get("outro_ms", 8000))


def parse_paytable(raw: Any) -> Dict[int, Dict[int, Decimal]]:
    if not isinstance(raw, dict):
        raise KenoConfigError("Table de paiement invalide")
    table: Dict[int, Dict[int, Decimal]] = {}
    for spots_key, row in raw.items():
        try:
            spots = int(spots_key)
        except (TypeError, ValueError):
            raise KenoConfigError(f"Nombre de numéros invalide : {spots_key}")
        if not 1 <= spots <= HARD_MAX_PICKS:
            raise KenoConfigError(f"Nombre de numéros invalide : {spots} (1 à {HARD_MAX_PICKS})")
        if not isinstance(row, dict):
            raise KenoConfigError(f"Ligne {spots} invalide")
        clean = {}
        for hits_key, mult in row.items():
            try:
                hits = int(hits_key)
                value = Decimal(str(mult))
            except Exception:
                raise KenoConfigError(f"Valeur invalide pour {spots} numéros / {hits_key} trouvés")
            if not 0 <= hits <= spots:
                raise KenoConfigError(f"{spots} numéros joués : {hits} trouvés est impossible")
            if not value.is_finite() or value < 0 or value > MAX_MULTIPLIER:
                raise KenoConfigError(f"Multiplicateur invalide ({spots} numéros / {hits} trouvés)")
            if value != value.quantize(Decimal("0.01")):
                raise KenoConfigError("Multiplicateurs : 2 décimales maximum")
            if value > 0:
                clean[hits] = value
        table[spots] = clean
    return table


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Complète et contrôle une configuration ; renvoie la version stockée
    (JSON) — lève KenoConfigError avec un message lisible."""
    merged = {**DEFAULT_CONFIG, **(config or {})}
    try:
        min_picks, max_picks = int(merged["min_picks"]), int(merged["max_picks"])
        min_bet, max_bet = Decimal(str(merged["min_bet"])), Decimal(str(merged["max_bet"]))
        max_payout = Decimal(str(merged["max_payout"]))
        max_rtp = float(merged["max_rtp"])
    except Exception:
        raise KenoConfigError("Valeur numérique invalide")

    if merged.get("mode") not in ("instant", "scheduled"):
        raise KenoConfigError("Mode de jeu invalide")
    timing = {}
    for key, low, high, label in (
        ("interval_minutes", 2, 120, "Intervalle entre tirages (minutes)"),
        ("betting_close_seconds", 0, 300, "Fermeture des paris (secondes)"),
        ("open_hour", 0, 23, "Heure d'ouverture"),
        ("close_hour", 1, 24, "Heure de fermeture"),
        ("intro_ms", 0, 30000, "Présentation (ms)"),
        ("ball_interval_ms", 300, 10000, "Intervalle entre boules (ms)"),
        ("outro_ms", 0, 60000, "Affichage du résultat (ms)"),
    ):
        try:
            value = int(merged[key])
        except (TypeError, ValueError):
            raise KenoConfigError(f"{label} invalide")
        if not low <= value <= high:
            raise KenoConfigError(f"{label} : entre {low} et {high}")
        timing[key] = value
    if timing["open_hour"] >= timing["close_hour"]:
        raise KenoConfigError("L'heure d'ouverture doit précéder l'heure de fermeture")
    if draw_duration_ms(timing) + timing["betting_close_seconds"] * 1000 >= timing["interval_minutes"] * 60000:
        raise KenoConfigError("Le tirage dure plus longtemps que l'intervalle entre deux tirages")
    if not 1 <= min_picks <= max_picks <= HARD_MAX_PICKS:
        raise KenoConfigError(f"Numéros : il faut 1 ≤ minimum ≤ maximum ≤ {HARD_MAX_PICKS}")
    if not Decimal("1") <= min_bet <= max_bet <= HARD_MAX_STAKE:
        raise KenoConfigError(f"Mises : il faut 1 ≤ minimum ≤ maximum ≤ {HARD_MAX_STAKE} HTG")
    if not Decimal("1") <= max_payout <= HARD_MAX_PAYOUT:
        raise KenoConfigError(f"Gain maximum : 1 à {HARD_MAX_PAYOUT} HTG")
    if not 1 <= max_rtp <= 100:
        raise KenoConfigError("Taux maximum : 1 à 100 %")

    options = merged.get("stake_options") or []
    try:
        options = sorted({Decimal(str(o)) for o in options})
    except Exception:
        raise KenoConfigError("Mises proposées invalides")
    if any(o < min_bet or o > max_bet for o in options):
        raise KenoConfigError("Les mises proposées doivent être entre la mise minimum et la mise maximum")
    if len(options) > 8:
        raise KenoConfigError("8 mises proposées au maximum")

    paytable = parse_paytable(merged.get("paytable"))
    for spots in range(min_picks, max_picks + 1):
        if not paytable.get(spots):
            raise KenoConfigError(f"Table de paiement manquante pour {spots} numéro(s) joué(s)")
    for spots, value in rtp_table(paytable).items():
        if value > max_rtp:
            raise KenoConfigError(
                f"{spots} numéro(s) joué(s) : taux de redistribution {value} % supérieur au maximum autorisé ({max_rtp:g} %)"
            )

    return {
        "enabled": bool(merged.get("enabled", True)),
        "mode": merged["mode"],
        **timing,
        "min_picks": min_picks,
        "max_picks": max_picks,
        "min_bet": float(min_bet),
        "max_bet": float(max_bet),
        "stake_options": [float(o) for o in options],
        "max_payout": float(max_payout),
        "max_rtp": max_rtp,
        "paytable": {str(s): {str(h): str(m) for h, m in sorted(row.items())} for s, row in sorted(paytable.items())},
    }


def public_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Ce qui est affiché au bureau / sur l'écran (avec le taux réel)."""
    paytable = parse_paytable(config["paytable"])
    return {
        **{k: config[k] for k in ("enabled", "mode", "min_picks", "max_picks", "min_bet", "max_bet", "stake_options", "max_payout",
                                  "interval_minutes", "betting_close_seconds", "intro_ms", "ball_interval_ms", "outro_ms")},
        "total_numbers": TOTAL_NUMBERS,
        "drawn_count": DRAWN_COUNT,
        "paytable": {s: {h: float(m) for h, m in row.items()} for s, row in config["paytable"].items()},
        "rtp": {str(k): v for k, v in rtp_table(paytable).items()},
    }
