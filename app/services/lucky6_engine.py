# app/services/lucky6_engine.py
"""
Lucky6 — calculs purs (aucun accès base / réseau).

Règles :
- 48 boules (1 à 48), 8 couleurs de 6 boules ;
- le joueur choisit 6 numéros différents (pari « SIX ») ;
- 35 boules sont tirées, dans un ordre précis ;
- si les 6 numéros du joueur sortent, le gain dépend de la POSITION de sortie
  du 6e numéro trouvé : plus il sort tôt, plus le multiplicateur est élevé
  (table de paiement réglable, positions 6 à 35) ;
- pari annexe : parité de la 1re boule (FIRST_ODD / FIRST_EVEN), cote fixe.

Hasard : Fisher-Yates piloté par HMAC-SHA256(seed, "lucky6:<manche>:<i>")
(même principe que le Keno, module app.services.fairness). Le seed est engagé
par son empreinte avant les paris et révélé après le tirage.

Probabilités exactes : les positions des 6 numéros du joueur dans la
permutation complète forment une partie uniforme à 6 éléments de {1..48}.
P(6e trouvé exactement à la position k) = C(k-1, 5) / C(48, 6).
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from math import comb
from typing import Any, Dict, List, Optional, Sequence

from app.core.exceptions import ValidationException
from app.services.fairness import hmac_uniform, seed_hash

TOTAL_NUMBERS = 48
PICKS = 6
DRAWN_COUNT = 35
FIRST_PAID_POSITION = PICKS          # au plus tôt, le 6e numéro sort en 6e position
POSITIONS = list(range(FIRST_PAID_POSITION, DRAWN_COUNT + 1))   # 6..35 (30 positions)
MAX_BET_LIMIT = 100000
BET_TYPES = ("SIX", "FIRST_ODD", "FIRST_EVEN")
PARITY_TYPES = ("FIRST_ODD", "FIRST_EVEN")

# Couleurs des boules : 8 couleurs × 6 boules (numéro n -> COLORS[(n-1) % 8])
COLORS = ["red", "green", "blue", "purple", "brown", "yellow", "orange", "black"]

_TOTAL_COMBOS = comb(TOTAL_NUMBERS, PICKS)
POSITION_PROBABILITIES: List[float] = [comb(k - 1, PICKS - 1) / _TOTAL_COMBOS for k in POSITIONS]
WIN_PROBABILITY = sum(POSITION_PROBABILITIES)   # ≈ 13,23 %

# Forme classique (position 6 -> 35) : taux 101,94 % (référence pour « ajuster au taux cible »)
CLASSIC_SHAPE = [10000, 7500, 5000, 2500, 1000, 500, 300, 200, 150, 100,
                 90, 80, 70, 60, 50, 40, 30, 25, 20, 15,
                 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]


def ball_color(number: int) -> str:
    return COLORS[(int(number) - 1) % len(COLORS)]


# ============================================================
# Tirage
# ============================================================

def draw_order(server_seed: str, round_number: int) -> List[int]:
    """Les 35 boules dans l'ordre de sortie (déterministe, vérifiable)."""
    numbers = list(range(1, TOTAL_NUMBERS + 1))
    order: List[int] = []
    for i in range(TOTAL_NUMBERS - 1, TOTAL_NUMBERS - 1 - DRAWN_COUNT, -1):
        j = int(hmac_uniform(server_seed, f"lucky6:{round_number}:{i}") * (i + 1))
        numbers[i], numbers[j] = numbers[j], numbers[i]
        order.append(numbers[i])
    return order


def verify_draw(server_seed: str, expected_hash: str, round_number: int, published: Sequence[int]) -> Dict[str, Any]:
    recomputed = draw_order(server_seed, round_number)
    return {
        "seed_hash_valid": seed_hash(server_seed) == expected_hash,
        "result_valid": [int(n) for n in published] == recomputed,
        "recomputed": recomputed,
    }


# ============================================================
# Sélection et résultat d'un pari
# ============================================================

def validate_picks(selection: Any) -> List[int]:
    """6 numéros entiers différents entre 1 et 48 (lève ValueError)."""
    if not isinstance(selection, (list, tuple)):
        raise ValueError("Sélection invalide")
    try:
        numbers = [int(n) for n in selection]
    except (TypeError, ValueError):
        raise ValueError("Numéros invalides")
    if len(numbers) != PICKS:
        raise ValueError(f"Il faut choisir exactement {PICKS} numéros")
    if len(set(numbers)) != PICKS:
        raise ValueError("Un numéro ne peut être choisi qu'une fois")
    if any(not 1 <= n <= TOTAL_NUMBERS for n in numbers):
        raise ValueError(f"Les numéros vont de 1 à {TOTAL_NUMBERS}")
    return sorted(numbers)


def sixth_match_position(picks: Sequence[int], balls: Sequence[int]) -> Optional[int]:
    """Position (1 à 35) où sort le 6e numéro du joueur, None s'il en manque."""
    wanted = set(int(n) for n in picks)
    found = 0
    for position, ball in enumerate(balls, start=1):
        if ball in wanted:
            found += 1
            if found == len(wanted):
                return position
    return None


def matches(picks: Sequence[int], balls: Sequence[int]) -> List[int]:
    drawn = set(balls)
    return [n for n in picks if n in drawn]


def multiplier_for_position(paytable: Sequence[float], position: Optional[int]) -> Decimal:
    if position is None or not FIRST_PAID_POSITION <= position <= DRAWN_COUNT:
        return Decimal("0")
    return Decimal(str(paytable[position - FIRST_PAID_POSITION]))


def first_ball_parity_wins(bet_type: str, balls: Sequence[int]) -> bool:
    if not balls:
        return False
    odd = int(balls[0]) % 2 == 1
    return odd if bet_type == "FIRST_ODD" else not odd


def payout(stake: Decimal, multiplier: Decimal, max_payout: Any) -> Decimal:
    amount = (Decimal(stake) * Decimal(multiplier)).quantize(Decimal("0.01"), ROUND_DOWN)
    return min(amount, Decimal(str(max_payout)))


# ============================================================
# Table de paiement : taux de redistribution exact
# ============================================================

def rtp(paytable: Sequence[float]) -> float:
    return sum(float(m) * p for m, p in zip(paytable, POSITION_PROBABILITIES))


def rtp_table(paytable: Sequence[float]) -> List[Dict[str, Any]]:
    return [
        {
            "position": k,
            "multiplier": float(m),
            "probability": p,
            "contribution": float(m) * p,
        }
        for k, m, p in zip(POSITIONS, paytable, POSITION_PROBABILITIES)
    ]


def _nice_floor(value: float) -> float:
    """Arrondi vers le bas : entier à partir de 10, dixième en dessous, minimum 1."""
    if value >= 10:
        return float(int(value))
    return max(1.0, int(value * 10) / 10.0)


def scale_paytable(paytable: Sequence[float], target_rtp: float) -> List[float]:
    """Garde les proportions de la table et l'ajuste au taux cible sans jamais
    le dépasser (arrondis vers le bas ; une position à 0 reste à 0)."""
    base = [float(m) for m in paytable]
    if len(base) != len(POSITIONS) or not any(base):
        raise ValidationException("Table de paiement invalide")
    floor_rtp = rtp([1.0 if m > 0 else 0.0 for m in base])
    if target_rtp < floor_rtp:
        raise ValidationException(f"Taux cible trop bas : minimum {floor_rtp * 100:.2f} % (multiplicateurs à 1)")

    def build(factor: float) -> List[float]:
        return [_nice_floor(m * factor) if m > 0 else 0.0 for m in base]

    low, high = 0.0, 1.0
    while rtp(build(high)) <= target_rtp and high < 1e6:
        low, high = high, high * 2
    for _ in range(80):
        mid = (low + high) / 2
        if rtp(build(mid)) <= target_rtp:
            low = mid
        else:
            high = mid
    return build(low)


# Table par défaut : forme classique, valeurs rondes, taux exact 85,06 %
DEFAULT_PAYTABLE = [10000, 7500, 5000, 2500, 1000, 500, 300, 200, 150, 100,
                    80, 70, 60, 50, 40, 30, 25, 20, 15, 12,
                    9, 8, 7, 6, 5, 4, 3, 2, 1.5, 1]

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "auto_enabled": True,
    "interval_minutes": 5,          # une manche partagée toutes les N minutes
    "betting_close_seconds": 15,    # paris fermés N secondes avant le tirage
    "intro_ms": 4000,               # présentation avant la 1re boule
    "ball_interval_ms": 2000,       # une boule toutes les N ms
    "outro_ms": 6000,               # affichage du résultat avant le règlement
    "open_hour": 8,                 # heures d'ouverture (heure d'Haïti)
    "close_hour": 23,
    "min_bet": 10,
    "max_bet": 10000,
    "stake_options": [10, 25, 50, 100, 250, 500],
    "max_payout": 1000000,          # gain maximum par pari
    "max_rtp": 0.95,                # une table au-dessus est refusée
    "paytable": DEFAULT_PAYTABLE,   # multiplicateurs des positions 6 à 35
    "parity_enabled": True,
    "parity_odds": 1.90,            # 1re boule paire / impaire (taux 95 %)
}


def draw_duration_ms(config: Dict[str, Any]) -> int:
    return int(config.get("intro_ms", 4000)) + DRAWN_COUNT * int(config.get("ball_interval_ms", 2000)) + int(config.get("outro_ms", 6000))


def validate_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Valide et complète une configuration (lève ValidationException)."""
    merged = {**DEFAULT_CONFIG, **(config or {})}

    def as_int(key: str, low: int, high: int, label: str) -> int:
        try:
            value = int(merged[key])
        except (TypeError, ValueError):
            raise ValidationException(f"{label} invalide")
        if not low <= value <= high:
            raise ValidationException(f"{label} : entre {low} et {high}")
        return value

    for key in ("enabled", "auto_enabled", "parity_enabled"):
        merged[key] = bool(merged[key])
    merged["interval_minutes"] = as_int("interval_minutes", 2, 120, "Intervalle (minutes)")
    merged["betting_close_seconds"] = as_int("betting_close_seconds", 0, 300, "Fermeture des paris (secondes)")
    merged["intro_ms"] = as_int("intro_ms", 0, 30000, "Présentation (ms)")
    merged["ball_interval_ms"] = as_int("ball_interval_ms", 500, 10000, "Intervalle entre boules (ms)")
    merged["outro_ms"] = as_int("outro_ms", 0, 60000, "Affichage du résultat (ms)")
    merged["open_hour"] = as_int("open_hour", 0, 23, "Heure d'ouverture")
    merged["close_hour"] = as_int("close_hour", 1, 24, "Heure de fermeture")
    if merged["open_hour"] >= merged["close_hour"]:
        raise ValidationException("L'heure d'ouverture doit précéder l'heure de fermeture")
    if draw_duration_ms(merged) + merged["betting_close_seconds"] * 1000 >= merged["interval_minutes"] * 60000:
        raise ValidationException("Le tirage dure plus longtemps que l'intervalle entre deux manches")

    try:
        min_bet, max_bet = Decimal(str(merged["min_bet"])), Decimal(str(merged["max_bet"]))
        max_payout = Decimal(str(merged["max_payout"]))
        max_rtp = float(merged["max_rtp"])
    except Exception:
        raise ValidationException("Montants invalides")
    if not 0 < min_bet <= max_bet <= MAX_BET_LIMIT:
        raise ValidationException(f"Mises : 0 < minimum ≤ maximum ≤ {MAX_BET_LIMIT} HTG")
    if max_payout < max_bet:
        raise ValidationException("Le gain maximum doit être au moins égal à la mise maximum")
    if not 0.5 <= max_rtp <= 1.0:
        raise ValidationException("Taux de redistribution maximum : entre 50 % et 100 %")
    merged["min_bet"], merged["max_bet"] = float(min_bet), float(max_bet)
    merged["max_payout"], merged["max_rtp"] = float(max_payout), max_rtp

    options = []
    for value in merged.get("stake_options") or []:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            raise ValidationException("Mises proposées invalides")
        if not min_bet <= Decimal(str(amount)) <= max_bet:
            raise ValidationException(f"Mise proposée {amount:g} hors des limites min / max")
        if amount not in options:
            options.append(amount)
    if len(options) > 8:
        raise ValidationException("8 mises proposées au maximum")
    merged["stake_options"] = sorted(options)

    table = merged.get("paytable")
    if not isinstance(table, (list, tuple)) or len(table) != len(POSITIONS):
        raise ValidationException(f"La table de paiement doit avoir {len(POSITIONS)} lignes (positions 6 à 35)")
    clean: List[float] = []
    for position, value in zip(POSITIONS, table):
        try:
            m = round(float(value), 2)
        except (TypeError, ValueError):
            raise ValidationException(f"Multiplicateur invalide en position {position}")
        if m != 0 and not 1 <= m <= 100000:
            raise ValidationException(f"Position {position} : multiplicateur 0 (non payé) ou entre 1 et 100000")
        clean.append(m)
    if not any(clean):
        raise ValidationException("Au moins une position doit être payée")
    for earlier, later in zip(clean, clean[1:]):
        if later > earlier:
            raise ValidationException("Un 6e numéro sorti plus tard ne peut pas payer davantage qu'une position plus tôt")
    merged["paytable"] = clean
    table_rtp = rtp(clean)
    if table_rtp > max_rtp + 1e-9:
        raise ValidationException(
            f"Taux de redistribution de la table : {table_rtp * 100:.2f} % (maximum autorisé {max_rtp * 100:.2f} %)"
        )

    try:
        parity_odds = round(float(merged["parity_odds"]), 2)
    except (TypeError, ValueError):
        raise ValidationException("Cote pair / impair invalide")
    if parity_odds < 1.01 or parity_odds * 0.5 > max_rtp + 1e-9:
        raise ValidationException(
            f"Cote pair / impair : entre 1.01 et {int(max_rtp * 2 * 100) / 100:.2f} (taux maximum {max_rtp * 100:.0f} %)"
        )
    merged["parity_odds"] = parity_odds
    return merged


def public_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "enabled": config["enabled"],
        "total_numbers": TOTAL_NUMBERS,
        "picks": PICKS,
        "drawn_count": DRAWN_COUNT,
        "colors": COLORS,
        "min_bet": config["min_bet"],
        "max_bet": config["max_bet"],
        "stake_options": config["stake_options"],
        "max_payout": config["max_payout"],
        "paytable": [{"position": k, "multiplier": m} for k, m in zip(POSITIONS, config["paytable"])],
        "rtp": rtp(config["paytable"]),
        "win_probability": WIN_PROBABILITY,
        "parity_enabled": config["parity_enabled"],
        "parity_odds": config["parity_odds"],
        "interval_minutes": config["interval_minutes"],
    }
