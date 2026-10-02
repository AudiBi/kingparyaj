# app/services/horse_race_service.py
"""
Service Horse Races : 6 concurrents, cotes figées, classement tiré du seed.

Le cycle de vie (manches, paris, règlement, remboursements, cycle automatique,
diffusion) est commun à tous les jeux à manches : voir round_game_service.
Ce module ne contient que ce qui est propre aux courses.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Dict, List, Sequence, Tuple

from app.core.exceptions import GameException, ValidationException
from app.models.game import GameBet, GameRound
from app.services import horse_race_engine as engine
from app.services.round_game_service import (  # noqa: F401  (ré-exportés)
    ACTIVE_STATUSES,
    RESULT_VISIBLE_STATUSES,
    SEED_REVEALED_STATUSES,
    RoundGameService,
    _iso,
    _mark,
)

GAME_TYPE = "horse_races"
CONFIG_KEY = "settings:horse_races"

# Exemple fourni : cotes indicatives 3.20 / 4.10 / 6.50 / 2.80 / 7.00 / 10.00
# -> poids = 1 / cote. Modifiable par l'admin (réglage Redis).
DEFAULT_CONFIG: Dict[str, Any] = {
    "runners": [
        {"player_number": 10, "player_name": "Messi", "weight": 0.3125, "assets": {}},
        {"player_number": 7, "player_name": "Ronaldo", "weight": 0.2439, "assets": {}},
        {"player_number": 11, "player_name": "Neymar", "weight": 0.1538, "assets": {}},
        {"player_number": 9, "player_name": "Mbappé", "weight": 0.3571, "assets": {}},
        {"player_number": 8, "player_name": "De Bruyne", "weight": 0.1429, "assets": {}},
        {"player_number": 6, "player_name": "Kimmich", "weight": 0.1000, "assets": {}},
    ],
    "margin": 0.15,               # marge maison (taux de redistribution théorique 85 %)
    "place_positions": 1,         # PLACE gagne si un des chevaux choisis finit dans le top N
    "min_bet": 10,
    "max_bet": 10000,
    "auto_enabled": True,         # cycle automatique (comme le Keno)
    "interval_minutes": 5,
    "betting_close_seconds": 15,  # paris fermés N secondes avant le départ
    "race_duration_ms": 25000,
    "open_hour": 8,               # heures d'ouverture (heure d'Haïti)
    "close_hour": 23,
    "strength_variation": 0.15,   # (ancien mode par poids, non utilisé)
    "odds_min": 4.14,             # cotes gagnant tirées au hasard dans cette fourchette
    "odds_max": 7.79,
}

def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Valide et complète une configuration (lève ValidationException)."""
    merged = {**DEFAULT_CONFIG, **(config or {})}
    runners = merged.get("runners") or []
    if len(runners) != engine.RUNNERS_PER_RACE:
        raise ValidationException("Une course doit avoir exactement 6 concurrents")
    numbers = []
    clean_runners = []
    for runner in runners:
        try:
            number = int(runner["player_number"])
            weight = float(runner.get("weight", 1))
        except (KeyError, TypeError, ValueError):
            raise ValidationException("Numéro de joueur ou poids invalide")
        name = str(runner.get("player_name") or "").strip()
        if not 1 <= number <= 99:
            raise ValidationException(f"Numéro de joueur invalide : {number} (1 à 99)")
        if not name or len(name) > 30:
            raise ValidationException("Nom de joueur requis (30 caractères maximum)")
        if weight <= 0:
            raise ValidationException(f"Poids invalide pour №{number}")
        numbers.append(number)
        clean_runners.append({
            "player_number": number,
            "player_name": name,
            "weight": weight,
            "assets": runner.get("assets") or {},
        })
    if len(set(numbers)) != len(numbers):
        raise ValidationException("Deux concurrents ne peuvent pas avoir le même numéro")
    merged["runners"] = clean_runners

    if not 0 <= float(merged["margin"]) < 0.5:
        raise ValidationException("Marge invalide (0 à 0.5)")
    try:
        odds_min, odds_max = float(merged["odds_min"]), float(merged["odds_max"])
    except (TypeError, ValueError):
        raise ValidationException("Cotes min/max invalides")
    if not 1.01 <= odds_min < odds_max <= 1000:
        raise ValidationException("Cotes invalides : il faut 1.01 ≤ cote minimale < cote maximale")
    low, high = engine.odds_margin_bounds(odds_min, odds_max)
    if not low <= float(merged["margin"]) <= high:
        raise ValidationException(
            f"Marge incompatible avec les cotes {odds_min:.2f}–{odds_max:.2f} : "
            f"elle doit être entre {max(low, 0):.2f} et {high:.2f}"
        )
    merged["odds_min"], merged["odds_max"] = round(odds_min, 2), round(odds_max, 2)
    if not 1 <= int(merged["place_positions"]) <= 3:
        raise ValidationException("PLACE : 1 à 3 positions payées")
    if not 0 < float(merged["min_bet"]) <= float(merged["max_bet"]):
        raise ValidationException("Mises min/max invalides")
    if int(merged["interval_minutes"]) < 1:
        raise ValidationException("Intervalle minimum : 1 minute")
    if int(merged["betting_close_seconds"]) < 0:
        raise ValidationException("Délai de fermeture des paris invalide")
    return merged


class HorseRaceService(RoundGameService):
    GAME_TYPE = GAME_TYPE
    CONFIG_KEY = CONFIG_KEY
    REF = "HR"
    WS_TYPE = "horse_race"
    AUDIT_RESOURCE = "horse_race"
    BET_RESOURCE = "horse_race_bet"
    MSG = {
        **RoundGameService.MSG,
        "not_found": "Course",
        "closed": "Les paris sont fermés pour cette course",
        "agent_only": "Les paris Horse Races se prennent uniquement chez un agent",
        "already_result": "Le résultat de cette course a déjà été généré",
        "finished": "Course déjà terminée",
        "settled": "Course déjà réglée",
        "cancelled": "Course annulée",
        "running": "Course déjà lancée",
        "status": "Action impossible : course au statut {status}",
    }

    # ------------------------------------------------------------
    # Règles propres aux courses
    # ------------------------------------------------------------

    def validate_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return validate_config(config)

    def build_round(self, config: Dict[str, Any], server_seed: str, round_number: int, nonce: int) -> Tuple[list, dict]:
        """6 concurrents, probabilités et cotes WIN tirées du seed (figées)."""
        runners = config["runners"]
        margin = float(config["margin"])
        place_n = int(config["place_positions"])
        win_odds, probabilities = engine.random_win_odds(
            server_seed, round_number, nonce,
            float(config["odds_min"]), float(config["odds_max"]), margin,
        )
        probabilities = [round(p, 10) for p in probabilities]
        probs = {r["player_number"]: p for r, p in zip(runners, probabilities)}

        participants = []
        for lane, (runner, probability, odds) in enumerate(zip(runners, probabilities, win_odds), start=1):
            number = runner["player_number"]
            participants.append({
                "lane": lane,
                "player_number": number,
                "player_name": runner["player_name"],
                "probability": probability,
                "odds": {
                    "WIN": float(odds),
                    "PLACE": float(odds) if place_n == 1 else float(engine.bet_odds(probs, "PLACE", [number], margin, place_n)),
                },
                "assets": runner.get("assets") or {},
            })
        race_config = {
            k: config[k]
            for k in ("margin", "place_positions", "min_bet", "max_bet", "race_duration_ms", "betting_close_seconds",
                      "odds_min", "odds_max")
        }
        return participants, race_config

    def generate_result(self, race: GameRound) -> None:
        numbers = [p["player_number"] for p in race.participants]
        probabilities = [p["probability"] for p in race.participants]
        order = engine.draw_finishing_order(numbers, probabilities, race.server_seed, race.round_number, race.nonce)
        race.result = order
        race.race_script = engine.build_race_script(
            order, race.server_seed, race.round_number, race.nonce,
            duration_ms=int(race.config.get("race_duration_ms", 25000)),
        )

    def round_duration_ms(self, race: GameRound) -> int:
        return int(race.config.get("race_duration_ms", 25000))

    def quote(self, race: GameRound, bet_type: str, selection: Sequence[int]) -> Tuple[List[int], Decimal]:
        """Normalise la sélection et calcule la cote serveur (jamais celle du client)."""
        bet_type = (bet_type or "").upper()
        numbers = [p["player_number"] for p in race.participants]
        try:
            chosen = engine.validate_selection(bet_type, selection, numbers)
        except ValueError as e:
            raise GameException(str(e))
        place_n = int(race.config.get("place_positions", 1))
        published = {p["player_number"]: p.get("odds") or {} for p in race.participants}
        if bet_type == "WIN" or (bet_type == "PLACE" and len(chosen) == 1 and place_n == 1):
            # cote affichée sur l'écran / le panel : c'est celle-là qui est figée
            odds = Decimal(str(published[chosen[0]]["WIN"]))
        else:
            probs = {p["player_number"]: p["probability"] for p in race.participants}
            odds = engine.bet_odds(probs, bet_type, chosen, float(race.config.get("margin", 0.15)), place_n)
        if odds < engine.MIN_ODDS:
            raise GameException("Cette sélection couvre trop de chances : cote inférieure à 1.01, pari refusé")
        return chosen, odds

    def bet_winnings(self, bet: GameBet, race: GameRound) -> Decimal:
        place_n = int(race.config.get("place_positions", 1))
        if not engine.is_winning(bet.bet_type, bet.selection, race.result, place_n):
            return Decimal("0")
        return (Decimal(bet.stake) * Decimal(bet.odds)).quantize(Decimal("0.01"), ROUND_DOWN)

    def serialize_details(self, race: GameRound, data: Dict[str, Any], result_visible: bool) -> None:
        data.update({
            "participants": [
                {
                    "lane": p["lane"],
                    "player_number": p["player_number"],
                    "player_name": p["player_name"],
                    "odds": p["odds"],
                    "assets": p.get("assets") or {},
                }
                for p in race.participants
            ],
            "bet_types": ["WIN", "PLACE", "EXACTA", "TRIFECTA"],
            "place_positions": race.config.get("place_positions", 1),
            "race_duration_ms": race.config.get("race_duration_ms"),
            "results": None,
            "race_script": None,
        })
        if result_visible:
            names = {p["player_number"]: p["player_name"] for p in race.participants}
            data["results"] = [
                {"position": i + 1, "player_number": n, "player_name": names.get(n)}
                for i, n in enumerate(race.result)
            ]
            data["race_script"] = race.race_script

    # ============================================================
    # Écran du joueur au guichet (affichage seulement)
    # ============================================================

    SCREEN_GAME = "hr"

    def _screen_selection(self, race: GameRound, numbers: Sequence[int]) -> List[Dict[str, Any]]:
        lanes = {p["player_number"]: (i, p) for i, p in enumerate(race.participants)}
        return [
            {"number": n, "name": lanes[n][1]["player_name"], "lane": lanes[n][0], "odds_win": lanes[n][1]["odds"]["WIN"]}
            for n in numbers if n in lanes
        ]

    async def screen_preview(self, agent_id: str, race_id: str, bet_type: Any, selection: Any, stake: Any) -> Dict[str, Any]:
        """Ticket en cours de saisie : type de pari, chevaux choisis, mise, cote
        et gain potentiel calculés par le serveur. Rien n'est joué ici."""
        from app.services import counter_screen

        race = await self.get_race(race_id)
        bet_type = str(bet_type or "WIN").upper()
        if bet_type not in engine.BET_SELECTION_SIZE:
            bet_type = "WIN"
        numbers: List[int] = []
        for value in selection if isinstance(selection, (list, tuple)) else []:
            try:
                n = int(value)
            except (TypeError, ValueError):
                continue
            if n not in numbers and len(numbers) < engine.BET_SELECTION_SIZE[bet_type][1]:
                numbers.append(n)
        odds = None
        try:
            _, quoted = self.quote(race, bet_type, numbers)
            odds = quoted
        except Exception:
            odds = None  # sélection incomplète : pas encore de cote
        try:
            amount = Decimal(str(stake)) if stake not in (None, "") else None
            if amount is not None and (not amount.is_finite() or amount <= 0):
                amount = None
        except (InvalidOperation, ValueError, TypeError):
            amount = None
        preview = {
            "race_id": race.id,
            "race_number": race.round_number,
            "bet_type": bet_type,
            "ordered": bet_type in ("EXACTA", "TRIFECTA"),
            "selection": self._screen_selection(race, numbers),
            "stake": float(amount) if amount is not None else None,
            "odds": float(odds) if odds is not None else None,
            "potential_payout": float((amount * odds).quantize(Decimal("0.01"), ROUND_DOWN)) if amount is not None and odds else None,
        }
        previous = await self.screen_state(self.screen_code(agent_id)) or {}
        return await counter_screen.publish(
            self.redis, self.SCREEN_GAME, agent_id,
            {"event": "preview", "preview": preview, "tickets": previous.get("tickets", []), "last_ticket": previous.get("last_ticket")},
        )

    async def screen_ticket(self, agent_id: str, bet: GameBet) -> Dict[str, Any]:
        """Ticket enregistré : ajouté à la liste « Vos tickets » de l'écran."""
        from app.services import counter_screen

        race = await self.get_race(bet.round_id)
        previous = await self.screen_state(self.screen_code(agent_id)) or {}  # statuts à jour
        ticket = {
            "bet_id": bet.id,
            "race_id": race.id,
            "race_number": race.round_number,
            "bet_type": bet.bet_type,
            "ordered": bet.bet_type in ("EXACTA", "TRIFECTA"),
            "selection": self._screen_selection(race, bet.selection),
            "stake": float(bet.stake),
            "odds": float(bet.odds),
            "potential_payout": float(bet.potential_payout),
            "status": bet.status,
            "winnings": float(bet.winnings or 0),
        }
        tickets = [t for t in previous.get("tickets", []) if t.get("bet_id") != bet.id] + [ticket]
        return await counter_screen.publish(
            self.redis, self.SCREEN_GAME, agent_id,
            {"event": "ticket", "preview": None, "tickets": tickets[-8:], "last_ticket": ticket},
        )
