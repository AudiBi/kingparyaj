# app/services/lucky6_service.py
"""
Service Lucky6 : manches partagées toutes les N minutes, paris au bureau.

Tout le cycle (paris sous verrou partagé, transitions sous verrou exclusif,
règlement idempotent, remboursements, cycle automatique, diffusion) vient de
RoundGameService, déjà éprouvé par Horse Races. Ce module ne contient que les
règles Lucky6 (calculs purs dans lucky6_engine).

Données : game_rounds / game_bets (game_type = "lucky6")
- game_rounds.config  : réglages FIGÉS à la création (table de paiement, cote
  pair/impair, mises, gain max, rythme du tirage) -> une modification de
  l'admin ne touche jamais une manche déjà ouverte ;
- game_rounds.result  : les 35 boules dans l'ordre de sortie ;
- game_bets.selection : 6 numéros triés (SIX) ou [] (FIRST_ODD / FIRST_EVEN) ;
- game_bets.odds      : SIX -> meilleur multiplicateur (position 6) ;
                        parité -> cote fixe.
Références de transaction : BET-L6-, WIN-L6-, REFUND-L6-<pari>.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.exceptions import GameException
from app.core.timezone import now_utc
from app.models.game import GameBet, GameRound
from app.services import lucky6_engine as engine
from app.services.round_game_service import RoundGameService, _iso

GAME_TYPE = "lucky6"
CONFIG_KEY = "settings:lucky6"
DEFAULT_CONFIG = engine.DEFAULT_CONFIG
validate_config = engine.validate_config

FROZEN_KEYS = (
    "paytable", "parity_enabled", "parity_odds", "min_bet", "max_bet", "max_payout",
    "intro_ms", "ball_interval_ms", "outro_ms", "betting_close_seconds", "interval_minutes",
)


class Lucky6Service(RoundGameService):
    GAME_TYPE = GAME_TYPE
    CONFIG_KEY = CONFIG_KEY
    REF = "L6"
    WS_TYPE = "lucky6"
    AUDIT_RESOURCE = "lucky6_round"
    BET_RESOURCE = "lucky6_bet"
    SCREEN_GAME = "l6"
    MSG = {
        **RoundGameService.MSG,
        "not_found": "Manche Lucky6",
        "agent_only": "Les paris Lucky6 se prennent uniquement chez un agent",
        "already_result": "Le tirage de cette manche a déjà été effectué",
        "running": "Tirage en cours",
    }

    # ------------------------------------------------------------
    # Règles Lucky6
    # ------------------------------------------------------------

    def validate_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return engine.validate_config(config)

    def auto_create_allowed(self, config: Dict[str, Any]) -> bool:
        return bool(config.get("enabled")) and super().auto_create_allowed(config)

    def next_scheduled_at(self, config: Dict[str, Any], now: datetime) -> datetime:
        """Manches calées sur l'horloge (ex. toutes les 5 min : 10:00, 10:05…),
        avec au moins une minute de paris ouverts."""
        step = int(config["interval_minutes"]) * 60
        earliest = now + timedelta(seconds=int(config["betting_close_seconds"]) + 60)
        midnight = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = (earliest - midnight).total_seconds()
        slots = -(-int(elapsed) // step)  # arrondi au créneau supérieur
        return midnight + timedelta(seconds=slots * step)

    def build_round(self, config: Dict[str, Any], server_seed: str, round_number: int, nonce: int) -> Tuple[list, dict]:
        return [], {k: config[k] for k in FROZEN_KEYS}

    def generate_result(self, race: GameRound) -> None:
        race.result = engine.draw_order(race.server_seed, race.round_number)

    def round_duration_ms(self, race: GameRound) -> int:
        return engine.draw_duration_ms(race.config)

    def quote(self, race: GameRound, bet_type: str, selection: Sequence[Any]) -> Tuple[List[int], Decimal]:
        bet_type = (bet_type or "").upper()
        if bet_type == "SIX":
            try:
                picks = engine.validate_picks(selection)
            except ValueError as e:
                raise GameException(str(e))
            return picks, Decimal(str(race.config["paytable"][0]))
        if bet_type in engine.PARITY_TYPES:
            if not race.config.get("parity_enabled"):
                raise GameException("Le pari pair / impair n'est pas proposé sur cette manche")
            return [], Decimal(str(race.config["parity_odds"]))
        raise GameException("Type de pari Lucky6 invalide")

    def potential_payout(self, race: GameRound, bet_type: str, stake: Decimal, odds: Decimal) -> Decimal:
        return engine.payout(stake, odds, race.config.get("max_payout", engine.DEFAULT_CONFIG["max_payout"]))

    def bet_winnings(self, bet: GameBet, race: GameRound) -> Decimal:
        balls = race.result or []
        max_payout = race.config.get("max_payout", engine.DEFAULT_CONFIG["max_payout"])
        if bet.bet_type == "SIX":
            position = engine.sixth_match_position(bet.selection, balls)
            multiplier = engine.multiplier_for_position(race.config["paytable"], position)
            return engine.payout(Decimal(bet.stake), multiplier, max_payout) if multiplier > 0 else Decimal("0")
        if bet.bet_type in engine.PARITY_TYPES and engine.first_ball_parity_wins(bet.bet_type, balls):
            return engine.payout(Decimal(bet.stake), Decimal(bet.odds), max_payout)
        return Decimal("0")

    async def place_bet(self, *args, **kwargs) -> GameBet:
        if not (await self.get_config()).get("enabled"):
            raise GameException("Lucky6 est fermé pour le moment")
        return await super().place_bet(*args, **kwargs)

    def serialize_details(self, race: GameRound, data: Dict[str, Any], result_visible: bool) -> None:
        cfg = race.config
        data.update({
            "total_numbers": engine.TOTAL_NUMBERS,
            "picks": engine.PICKS,
            "drawn_count": engine.DRAWN_COUNT,
            "paytable": [{"position": k, "multiplier": m} for k, m in zip(engine.POSITIONS, cfg["paytable"])],
            "parity_enabled": cfg.get("parity_enabled", False),
            "parity_odds": cfg.get("parity_odds"),
            "max_payout": cfg.get("max_payout"),
            "intro_ms": cfg.get("intro_ms"),
            "ball_interval_ms": cfg.get("ball_interval_ms"),
            "outro_ms": cfg.get("outro_ms"),
            "duration_ms": engine.draw_duration_ms(cfg),
            "balls": None,
            "first_ball_parity": None,
        })
        if result_visible:
            data["balls"] = list(race.result)
            data["first_ball_parity"] = "ODD" if race.result[0] % 2 else "EVEN"

    def bet_details(self, bet: GameBet, race: Optional[GameRound]) -> Dict[str, Any]:
        """Pari + ce que le tirage lui a donné (numéros trouvés, position)."""
        data = self.serialize_bet(bet)
        data["race_number"] = race.round_number if race else None
        balls = (race.result or []) if race is not None and race.status in ("RUNNING", "FINISHED", "SETTLED") else []
        if bet.bet_type == "SIX":
            position = engine.sixth_match_position(bet.selection, balls) if balls else None
            data.update({
                "matched": engine.matches(bet.selection, balls) if balls else [],
                "sixth_position": position,
                "multiplier": float(engine.multiplier_for_position(race.config["paytable"], position)) if race and position else 0.0,
            })
        return data

    # ------------------------------------------------------------
    # Sélection aléatoire (serveur)
    # ------------------------------------------------------------

    @staticmethod
    def quick_pick() -> List[int]:
        return sorted(secrets.SystemRandom().sample(range(1, engine.TOTAL_NUMBERS + 1), engine.PICKS))

    # ------------------------------------------------------------
    # Vérification publique
    # ------------------------------------------------------------

    async def verify(self, race_id: str) -> Dict[str, Any]:
        race = await self.get_race(race_id)
        data = {
            "race_id": race.id,
            "race_number": race.round_number,
            "status": race.status,
            "server_seed_hash": race.server_seed_hash,
            "server_seed": None,
            "balls": None,
            "verification": None,
        }
        if race.status in ("FINISHED", "SETTLED") and race.result:
            data["server_seed"] = race.server_seed
            data["balls"] = list(race.result)
            data["verification"] = engine.verify_draw(race.server_seed, race.server_seed_hash, race.round_number, race.result)
        elif race.status == "CANCELLED":
            data["server_seed"] = race.server_seed
        return data

    # ------------------------------------------------------------
    # Écran du joueur au guichet (affichage seulement)
    # ------------------------------------------------------------

    async def screen_preview(self, agent_id: str, race_id: str, bet_type: Any, selection: Any, stake: Any) -> Dict[str, Any]:
        """Ticket en cours de saisie, valeurs calculées par le serveur. Rien n'est joué."""
        from app.services import counter_screen

        race = await self.get_race(race_id)
        bet_type = str(bet_type or "SIX").upper()
        if bet_type not in engine.BET_TYPES:
            bet_type = "SIX"
        numbers: List[int] = []
        if bet_type == "SIX":
            for value in selection if isinstance(selection, (list, tuple)) else []:
                try:
                    n = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= engine.TOTAL_NUMBERS and n not in numbers and len(numbers) < engine.PICKS:
                    numbers.append(n)
        try:
            amount = Decimal(str(stake)) if stake not in (None, "") else None
            if amount is not None and (not amount.is_finite() or amount <= 0):
                amount = None
        except (InvalidOperation, ValueError, TypeError):
            amount = None
        odds = None
        if bet_type == "SIX":
            odds = Decimal(str(race.config["paytable"][0]))
        elif race.config.get("parity_enabled"):
            odds = Decimal(str(race.config["parity_odds"]))
        preview = {
            "race_id": race.id,
            "race_number": race.round_number,
            "bet_type": bet_type,
            "selection": sorted(numbers),
            "stake": float(amount) if amount is not None else None,
            "odds": float(odds) if odds is not None else None,
            "potential_payout": float(self.potential_payout(race, bet_type, amount, odds)) if amount is not None and odds else None,
        }
        previous = await self.screen_state(self.screen_code(agent_id)) or {}
        return await counter_screen.publish(
            self.redis, self.SCREEN_GAME, agent_id,
            {"event": "preview", "preview": preview, "tickets": previous.get("tickets", []), "last_ticket": previous.get("last_ticket")},
        )

    async def screen_ticket(self, agent_id: str, bet: GameBet) -> Dict[str, Any]:
        from app.services import counter_screen

        race = await self.get_race(bet.round_id)
        previous = await self.screen_state(self.screen_code(agent_id)) or {}
        ticket = {
            "bet_id": bet.id,
            "race_id": race.id,
            "race_number": race.round_number,
            "bet_type": bet.bet_type,
            "selection": list(bet.selection),
            "stake": float(bet.stake),
            "odds": float(bet.odds),
            "potential_payout": float(bet.potential_payout),
            "status": bet.status,
            "winnings": float(bet.winnings or 0),
            "placed_at": _iso(bet.placed_at or now_utc()),
        }
        tickets = [t for t in previous.get("tickets", []) if t.get("bet_id") != bet.id] + [ticket]
        return await counter_screen.publish(
            self.redis, self.SCREEN_GAME, agent_id,
            {"event": "ticket", "preview": None, "tickets": tickets[-8:], "last_ticket": ticket},
        )
