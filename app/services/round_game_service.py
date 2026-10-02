# app/services/round_game_service.py
"""
Jeux à manches partagées (« rounds ») : socle commun à Horse Races et Lucky6.

Données : tables génériques game_rounds / game_bets (colonne game_type).
Argent  : WalletService (compte joueur) ou solde du ticket (bureau).
Hasard  : seed secret engagé par son empreinte, révélé après le résultat
          (app.services.fairness).

Cycle :  SCHEDULED -> BETTING_OPEN -> BETTING_CLOSED -> RUNNING -> FINISHED -> SETTLED
         (CANCELLED possible avant FINISHED : mises remboursées)

Chaque jeu est une sous-classe qui fournit :
- GAME_TYPE, CONFIG_KEY, REF (préfixe des références de transaction), WS_TYPE,
  AUDIT_RESOURCE, MSG (libellés) ;
- validate_config(), build_round(), generate_result(), round_duration_ms(),
  quote(), potential_payout(), bet_winnings(), serialize_details().

Garanties communes (inchangées par rapport à Horse Races) :
- prise de pari sous verrou PARTAGÉ (nombre illimité de parieurs simultanés) ;
- transitions sous verrou EXCLUSIF + contrôle du statut (jamais deux fois) ;
- règlement idempotent : UPDATE … WHERE status = 'PENDING' + référence unique
  WIN-<REF>-<pari> ; remboursement REFUND-<REF>-<pari> ;
- les méthodes ne valident pas la session : l'appelant appelle
  commit_and_publish() qui valide PUIS diffuse (rien n'est diffusé si rollback).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

import redis.asyncio as redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.exceptions import GameException, InsufficientBalanceException, NotFoundException, ValidationException
from app.core.logger import get_logger
from app.core.timezone import now_haiti, now_utc
from app.models.enums import AuditAction, TicketStatus
from app.models.game import GameBet, GameBetStatus, GameRound, GameRoundStatus
from app.models.ticket import Ticket
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.fairness import new_server_seed, seed_hash
from app.services.wallet_service import WalletService

ACTIVE_STATUSES = (
    GameRoundStatus.SCHEDULED.value,
    GameRoundStatus.BETTING_OPEN.value,
    GameRoundStatus.BETTING_CLOSED.value,
    GameRoundStatus.RUNNING.value,
)
RESULT_VISIBLE_STATUSES = (
    GameRoundStatus.RUNNING.value,
    GameRoundStatus.FINISHED.value,
    GameRoundStatus.SETTLED.value,
)
SEED_REVEALED_STATUSES = (
    GameRoundStatus.FINISHED.value,
    GameRoundStatus.SETTLED.value,
    GameRoundStatus.CANCELLED.value,
)


class RoundGameService:
    GAME_TYPE: str = ""
    CONFIG_KEY: str = ""
    REF: str = ""                 # WIN-<REF>-<pari>, BET-<REF>-…, REFUND-<REF>-…
    WS_TYPE: str = ""             # type des messages WebSocket
    AUDIT_RESOURCE: str = ""
    BET_RESOURCE: str = ""
    MSG: Dict[str, str] = {
        "not_found": "Manche",
        "closed": "Les paris sont fermés pour cette manche",
        "agent_only": "Les paris se prennent uniquement chez un agent",
        "too_close": "Départ trop proche : les paris n'auraient pas le temps d'ouvrir",
        "already_result": "Le résultat de cette manche a déjà été généré",
        "finished": "Manche déjà terminée",
        "settled": "Manche déjà réglée",
        "cancelled": "Manche annulée",
        "running": "Manche déjà lancée",
        "status": "Action impossible : manche au statut {status}",
    }

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client
        self.wallet_service = WalletService(db, redis_client)
        self.audit_service = AuditService(db, redis_client)
        self.logger = get_logger(self.__class__.__name__)
        self._events: List[Dict[str, Any]] = []

    # ============================================================
    # À fournir par chaque jeu
    # ============================================================

    def validate_config(self, config: Dict[str, Any]) -> Dict[str, Any]:  # pragma: no cover - abstrait
        raise NotImplementedError

    def build_round(self, config: Dict[str, Any], server_seed: str, round_number: int, nonce: int) -> Tuple[list, dict]:
        """Renvoie (participants, config figée de la manche)."""
        raise NotImplementedError  # pragma: no cover

    def generate_result(self, race: GameRound) -> None:
        """Calcule race.result (et race.race_script) à partir du seed."""
        raise NotImplementedError  # pragma: no cover

    def round_duration_ms(self, race: GameRound) -> int:
        raise NotImplementedError  # pragma: no cover

    def quote(self, race: GameRound, bet_type: str, selection: Sequence[Any]) -> Tuple[List[Any], Decimal]:
        """Normalise la sélection et renvoie (sélection, cote figée)."""
        raise NotImplementedError  # pragma: no cover

    def potential_payout(self, race: GameRound, bet_type: str, stake: Decimal, odds: Decimal) -> Decimal:
        return (stake * odds).quantize(Decimal("0.01"), ROUND_DOWN)

    def bet_winnings(self, bet: GameBet, race: GameRound) -> Decimal:
        """Gain d'un pari d'après le résultat (0 si perdu)."""
        raise NotImplementedError  # pragma: no cover

    def serialize_details(self, race: GameRound, data: Dict[str, Any], result_visible: bool) -> None:
        """Ajoute les champs propres au jeu à serialize_race()."""

    # ============================================================
    # Configuration (réglage Redis, comme les autres réglages admin)
    # ============================================================

    async def get_config(self) -> Dict[str, Any]:
        raw = None
        try:
            raw = await self.redis.get(self.CONFIG_KEY)
        except Exception as e:
            self.logger.warning(f"Configuration {self.GAME_TYPE} illisible, valeurs par défaut : {e}")
        if not raw:
            return self.validate_config({})
        try:
            return self.validate_config(json.loads(raw))
        except Exception:
            self.logger.error(f"Configuration {self.GAME_TYPE} invalide en Redis, valeurs par défaut utilisées")
            return self.validate_config({})

    async def save_config(self, config: Dict[str, Any], admin_id: Optional[str] = None) -> Dict[str, Any]:
        old = await self.get_config()
        clean = self.validate_config(config)
        await self.redis.set(self.CONFIG_KEY, json.dumps(clean))
        await self.audit_service.log(
            action=AuditAction.LIMIT_CHANGED,
            agent_id=admin_id,
            resource_type=f"{self.AUDIT_RESOURCE}_config",
            old_values=old,
            new_values=clean,
            extra_data={"event": "config_updated"},
        )
        return clean

    # ============================================================
    # Lecture
    # ============================================================

    async def get_race(self, race_id: str) -> GameRound:
        result = await self.db.execute(
            select(GameRound).where(
                GameRound.id == race_id,
                GameRound.game_type == self.GAME_TYPE,
                GameRound.is_deleted == False,  # noqa: E712
            )
        )
        race = result.scalar_one_or_none()
        if race is None:
            raise NotFoundException(self.MSG["not_found"], race_id)
        return race

    async def _get_race_for_share(self, race_id: str) -> GameRound:
        result = await self.db.execute(
            select(GameRound)
            .where(GameRound.id == race_id, GameRound.game_type == self.GAME_TYPE, GameRound.is_deleted == False)  # noqa: E712
            .with_for_update(read=True)
        )
        race = result.scalar_one_or_none()
        if race is None:
            raise NotFoundException(self.MSG["not_found"], race_id)
        return race

    async def _get_race_for_update(self, race_id: str) -> GameRound:
        result = await self.db.execute(
            select(GameRound)
            .where(GameRound.id == race_id, GameRound.game_type == self.GAME_TYPE, GameRound.is_deleted == False)  # noqa: E712
            .with_for_update()
        )
        race = result.scalar_one_or_none()
        if race is None:
            raise NotFoundException(self.MSG["not_found"], race_id)
        return race

    async def bet_totals(self, race_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Nombre de paris et total des mises par manche (paris remboursés exclus)."""
        if not race_ids:
            return {}
        result = await self.db.execute(
            select(GameBet.round_id, func.count(GameBet.id), func.coalesce(func.sum(GameBet.stake), 0))
            .where(GameBet.round_id.in_(list(race_ids)), GameBet.status != GameBetStatus.REFUNDED.value)
            .group_by(GameBet.round_id)
        )
        return {rid: {"total_bets": int(n), "total_stake": Decimal(str(total))} for rid, n, total in result.all()}

    async def _refresh_totals(self, race: GameRound) -> None:
        totals = (await self.bet_totals([race.id])).get(race.id, {"total_bets": 0, "total_stake": Decimal("0")})
        race.total_bets = totals["total_bets"]
        race.total_stake = totals["total_stake"]

    async def get_current_race(self) -> Optional[GameRound]:
        """Manche en cours ou prochaine manche (la plus proche non terminée)."""
        result = await self.db.execute(
            select(GameRound)
            .where(GameRound.game_type == self.GAME_TYPE, GameRound.status.in_(ACTIVE_STATUSES))
            .order_by(GameRound.scheduled_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_open_race(self) -> Optional[GameRound]:
        """Manche sur laquelle on peut parier maintenant (paris ouverts)."""
        result = await self.db.execute(
            select(GameRound)
            .where(GameRound.game_type == self.GAME_TYPE, GameRound.status == GameRoundStatus.BETTING_OPEN.value)
            .order_by(GameRound.scheduled_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def live_state(self) -> Dict[str, Any]:
        """État pour les écrans : la manche à afficher (en cours, ou la
        prochaine) et celle qui prend les paris (souvent la suivante)."""
        current = await self.get_current_race()
        open_race = await self.get_open_race()
        return {
            "race": self.serialize_race(current) if current else None,
            "betting_race": self.serialize_race(open_race) if open_race else None,
            "server_time": _iso(now_utc()),
        }

    async def get_history(self, limit: int = 20, offset: int = 0) -> List[GameRound]:
        result = await self.db.execute(
            select(GameRound)
            .where(
                GameRound.game_type == self.GAME_TYPE,
                GameRound.status.in_(
                    [GameRoundStatus.FINISHED.value, GameRoundStatus.SETTLED.value, GameRoundStatus.CANCELLED.value]
                ),
            )
            .order_by(GameRound.scheduled_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_user_bets(self, user_id: str, limit: int = 50, offset: int = 0) -> List[GameBet]:
        result = await self.db.execute(
            select(GameBet)
            .where(GameBet.user_id == user_id, GameBet.game_type == self.GAME_TYPE)
            .order_by(GameBet.placed_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_agent_race_bets(self, race_id: str, agent_id: str) -> List[GameBet]:
        result = await self.db.execute(
            select(GameBet)
            .where(GameBet.round_id == race_id, GameBet.agent_id == agent_id)
            .order_by(GameBet.placed_at.desc())
        )
        return list(result.scalars().all())

    async def lookup_bets(self, code: str) -> List[GameBet]:
        """Suivi public d'un pari : par code de pari (identifiant imprimé sur
        le reçu) ou par numéro de ticket. Ne renvoie que des paris de ce jeu."""
        code = (code or "").strip()
        if not code:
            return []
        try:
            uuid.UUID(code)
            is_bet_code = True
        except ValueError:
            is_bet_code = False
        if is_bet_code:
            bet = await self.db.get(GameBet, code.lower())
            return [bet] if bet is not None and bet.game_type == self.GAME_TYPE else []
        result = await self.db.execute(
            select(GameBet)
            .join(Ticket, Ticket.id == GameBet.ticket_id)
            .where(Ticket.ticket_number == code.upper(), GameBet.game_type == self.GAME_TYPE)
            .order_by(GameBet.placed_at.desc())
            .limit(20)
        )
        return list(result.scalars().all())

    async def get_race_bets(self, race_id: str) -> List[GameBet]:
        result = await self.db.execute(
            select(GameBet).where(GameBet.round_id == race_id).order_by(GameBet.placed_at.asc())
        )
        return list(result.scalars().all())

    # ============================================================
    # Sérialisation (ce qui est visible selon le statut)
    # ============================================================

    def serialize_race(self, race: GameRound) -> Dict[str, Any]:
        status = race.status
        data = {
            "race_id": race.id,
            "race_number": race.round_number,
            "game_type": race.game_type,
            "status": status,
            "scheduled_at": _iso(race.scheduled_at),
            "betting_opens_at": _iso(race.betting_opens_at),
            "betting_closes_at": _iso(race.betting_closes_at),
            "started_at": _iso(race.started_at),
            "finished_at": _iso(race.finished_at),
            "settled_at": _iso(race.settled_at),
            "server_time": _iso(now_utc()),
            "min_bet": race.config.get("min_bet"),
            "max_bet": race.config.get("max_bet"),
            "server_seed_hash": race.server_seed_hash,
            "server_seed": race.server_seed if status in SEED_REVEALED_STATUSES else None,
            "cancel_reason": race.cancel_reason,
        }
        self.serialize_details(race, data, status in RESULT_VISIBLE_STATUSES and bool(race.result))
        return data

    @staticmethod
    def serialize_bet(bet: GameBet) -> Dict[str, Any]:
        return {
            "bet_id": bet.id,
            "race_id": bet.round_id,
            "bet_type": bet.bet_type,
            "selection": bet.selection,
            "stake": float(bet.stake),
            "odds": float(bet.odds),
            "potential_payout": float(bet.potential_payout),
            "status": bet.status,
            "winnings": float(bet.winnings or 0),
            "placed_at": _iso(bet.placed_at),
            "settled_at": _iso(bet.settled_at),
        }

    # ============================================================
    # Écran du joueur au guichet (affichage seulement, voir counter_screen)
    # ============================================================

    SCREEN_GAME: str = ""

    @classmethod
    def screen_code(cls, agent_id: str) -> str:
        from app.services import counter_screen

        return counter_screen.screen_code(cls.SCREEN_GAME, agent_id)

    async def screen_state(self, code: str) -> Optional[Dict[str, Any]]:
        """État de l'écran, avec le statut à jour de chaque ticket (gagné, perdu…)."""
        from app.services import counter_screen

        state = await counter_screen.get_state(self.redis, self.SCREEN_GAME, code)
        if not state or not state.get("tickets"):
            return state
        ids = [t["bet_id"] for t in state["tickets"]]
        rows = {b.id: b for b in (await self.db.execute(select(GameBet).where(GameBet.id.in_(ids)))).scalars().all()}
        for t in state["tickets"]:
            bet = rows.get(t["bet_id"])
            if bet is not None:
                t["status"] = bet.status
                t["winnings"] = float(bet.winnings or 0)
        return state

    # ============================================================
    # Création / transitions
    # ============================================================

    async def _next_round_number(self) -> int:
        result = await self.db.execute(
            select(func.max(GameRound.round_number)).where(GameRound.game_type == self.GAME_TYPE)
        )
        return (result.scalar() or 0) + 1

    async def create_race(
        self,
        scheduled_at: Optional[datetime] = None,
        created_by: Optional[str] = None,
        open_betting: bool = True,
        config: Optional[Dict[str, Any]] = None,
    ) -> GameRound:
        """Crée une manche : seed secret (empreinte publiée), réglages figés."""
        config = self.validate_config(config) if config else await self.get_config()
        now = now_utc()
        scheduled_at = scheduled_at or now + timedelta(minutes=int(config["interval_minutes"]))
        closes_at = scheduled_at - timedelta(seconds=int(config["betting_close_seconds"]))
        if closes_at <= now:
            raise ValidationException(self.MSG["too_close"])

        round_number = await self._next_round_number()
        server_seed = new_server_seed()
        nonce = 0
        participants, race_config = self.build_round(config, server_seed, round_number, nonce)
        status = GameRoundStatus.BETTING_OPEN.value if open_betting else GameRoundStatus.SCHEDULED.value
        race = GameRound(
            id=str(uuid.uuid4()),
            game_type=self.GAME_TYPE,
            round_number=round_number,
            status=status,
            scheduled_at=scheduled_at,
            betting_opens_at=now if open_betting else None,
            betting_closes_at=closes_at,
            participants=participants,
            config=race_config,
            server_seed=server_seed,
            server_seed_hash=seed_hash(server_seed),
            nonce=nonce,
            created_by=created_by,
        )
        self.db.add(race)
        await self.db.flush()

        await self._audit_race(race, "race_created", created_by)
        if open_betting:
            await self._audit_race(race, "betting_opened", created_by)
        self._emit(race, "betting_opened" if open_betting else "race_scheduled")
        self.logger.info(f"{self.GAME_TYPE} #{round_number} créé(e) (départ {scheduled_at.isoformat()}Z)")
        return race

    async def open_betting(self, race_id: str, by: Optional[str] = None) -> GameRound:
        race = await self._get_race_for_update(race_id)
        self._require_status(race, [GameRoundStatus.SCHEDULED.value])
        if race.betting_closes_at and race.betting_closes_at <= now_utc():
            raise GameException("L'heure de fermeture des paris est déjà passée")
        race.status = GameRoundStatus.BETTING_OPEN.value
        race.betting_opens_at = now_utc()
        await self.db.flush()
        await self._audit_race(race, "betting_opened", by)
        self._emit(race, "betting_opened")
        return race

    async def close_betting(self, race_id: str, by: Optional[str] = None, now: Optional[datetime] = None) -> GameRound:
        race = await self._get_race_for_update(race_id)
        self._require_status(race, [GameRoundStatus.SCHEDULED.value, GameRoundStatus.BETTING_OPEN.value])
        race.status = GameRoundStatus.BETTING_CLOSED.value
        await self._refresh_totals(race)
        now = now or now_utc()
        if race.betting_closes_at is None or race.betting_closes_at > now:
            race.betting_closes_at = now
        await self.db.flush()
        await self._audit_race(race, "betting_closed", by)
        self._emit(race, "betting_closed")
        return race

    async def start_race(self, race_id: str, by: Optional[str] = None, now: Optional[datetime] = None) -> GameRound:
        """Ferme les paris si besoin, calcule le résultat (seed), lance l'animation."""
        race = await self._get_race_for_update(race_id)
        self._require_status(race, [
            GameRoundStatus.SCHEDULED.value, GameRoundStatus.BETTING_OPEN.value, GameRoundStatus.BETTING_CLOSED.value,
        ])
        if race.result:
            raise GameException(self.MSG["already_result"])
        now = now or now_utc()
        if race.status != GameRoundStatus.BETTING_CLOSED.value:
            if race.betting_closes_at is None or race.betting_closes_at > now:
                race.betting_closes_at = now

        self.generate_result(race)
        race.status = GameRoundStatus.RUNNING.value
        race.started_at = now
        await self._refresh_totals(race)
        await self.db.flush()

        await self._audit_race(race, "race_started", by, new_values={"result": race.result})
        self._emit(race, "race_started")
        return race

    async def finish_race(self, race_id: str, by: Optional[str] = None, now: Optional[datetime] = None) -> GameRound:
        race = await self._get_race_for_update(race_id)
        self._require_status(race, [GameRoundStatus.RUNNING.value])
        race.status = GameRoundStatus.FINISHED.value
        race.finished_at = now or now_utc()
        await self.db.flush()
        await self._audit_race(race, "race_finished", by, new_values={"result": race.result})
        self._emit(race, "race_finished")
        return race

    async def settle_race(self, race_id: str, by: Optional[str] = None) -> Dict[str, Any]:
        """Règle tous les paris en attente d'une manche terminée.

        Idempotent :
        1. manche verrouillée (SELECT … FOR UPDATE) ;
        2. un pari n'est réglé que si son statut passe de PENDING à WON/LOST
           (UPDATE … WHERE status = 'PENDING') ;
        3. chaque gain porte la référence unique WIN-<REF>-<bet_id>.
        """
        race = await self._get_race_for_update(race_id)
        if race.status == GameRoundStatus.SETTLED.value:
            return {"race_id": race.id, "settled_bets": 0, "winners": 0, "total_payout": 0.0, "already_settled": True}
        self._require_status(race, [GameRoundStatus.FINISHED.value])

        pending = await self.db.execute(
            select(GameBet).where(GameBet.round_id == race.id, GameBet.status == GameBetStatus.PENDING.value)
        )
        settled = winners = 0
        total_payout = Decimal("0")
        now = now_utc()

        for bet in pending.scalars().all():
            winnings = self.bet_winnings(bet, race)
            won = winnings > 0
            claimed = await self.db.execute(
                update(GameBet)
                .where(GameBet.id == bet.id, GameBet.status == GameBetStatus.PENDING.value)
                .values(
                    status=(GameBetStatus.WON if won else GameBetStatus.LOST).value,
                    winnings=winnings,
                    settled_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                continue  # déjà réglé par un autre processus
            _mark(bet, status=(GameBetStatus.WON if won else GameBetStatus.LOST).value, winnings=winnings, settled_at=now)
            settled += 1
            if won:
                winners += 1
                total_payout += winnings
                if bet.user_id:
                    tx = await self.wallet_service.credit_for_win(
                        user_id=bet.user_id,
                        amount=winnings,
                        bet_id=bet.id,
                        draw_id=race.id,
                        reference=f"WIN-{self.REF}-{bet.id}",
                    )
                    bet.payout_transaction_id = tx.id
                    user = await self.db.get(User, bet.user_id)
                    if user:
                        user.total_wins = (user.total_wins or Decimal("0")) + winnings
                elif bet.ticket_id:
                    ticket = await self._get_ticket_for_update(bet.ticket_id)
                    ticket.balance = (ticket.balance or Decimal("0")) + winnings

        race.total_payout = (race.total_payout or Decimal("0")) + total_payout
        await self._refresh_totals(race)
        race.status = GameRoundStatus.SETTLED.value
        race.settled_at = now
        await self.db.flush()

        await self.audit_service.log(
            action=AuditAction.BET_SETTLED,
            agent_id=by,
            resource_type=self.AUDIT_RESOURCE,
            resource_id=race.id,
            new_values={"settled_bets": settled, "winners": winners, "total_payout": float(total_payout)},
            extra_data={"event": "settlement_completed", "race_number": race.round_number},
        )
        self._emit(race, "race_settled")
        self.logger.info(f"✅ {self.GAME_TYPE} #{race.round_number} réglé(e) : {settled} paris, {winners} gagnants, {total_payout} HTG")
        return {
            "race_id": race.id,
            "settled_bets": settled,
            "winners": winners,
            "total_payout": float(total_payout),
            "already_settled": False,
        }

    async def cancel_race(self, race_id: str, reason: str, by: Optional[str] = None) -> Dict[str, Any]:
        """Annule une manche non terminée et rembourse toutes les mises
        (référence unique REFUND-<REF>-<bet_id> : jamais remboursé deux fois)."""
        race = await self._get_race_for_update(race_id)
        if race.status == GameRoundStatus.CANCELLED.value:
            return {"race_id": race.id, "refunded_bets": 0, "already_cancelled": True}
        self._require_status(race, list(ACTIVE_STATUSES))

        pending = await self.db.execute(
            select(GameBet).where(GameBet.round_id == race.id, GameBet.status == GameBetStatus.PENDING.value)
        )
        refunded = 0
        now = now_utc()
        for bet in pending.scalars().all():
            claimed = await self.db.execute(
                update(GameBet)
                .where(GameBet.id == bet.id, GameBet.status == GameBetStatus.PENDING.value)
                .values(status=GameBetStatus.REFUNDED.value, settled_at=now)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                continue
            _mark(bet, status=GameBetStatus.REFUNDED.value, settled_at=now)
            refunded += 1
            if bet.user_id:
                tx = await self.wallet_service.credit_refund(
                    user_id=bet.user_id,
                    amount=Decimal(bet.stake),
                    bet_id=bet.id,
                    draw_id=race.id,
                    reference=f"REFUND-{self.REF}-{bet.id}",
                )
                bet.payout_transaction_id = tx.id
            elif bet.ticket_id:
                ticket = await self._get_ticket_for_update(bet.ticket_id)
                ticket.balance = (ticket.balance or Decimal("0")) + Decimal(bet.stake)

        race.status = GameRoundStatus.CANCELLED.value
        race.cancelled_at = now
        race.cancel_reason = (reason or "Annulée")[:200]
        await self.db.flush()
        await self._audit_race(race, "race_cancelled", by, new_values={"refunded_bets": refunded, "reason": reason})
        self._emit(race, "race_cancelled")
        return {"race_id": race.id, "refunded_bets": refunded, "already_cancelled": False}

    # ============================================================
    # Paris
    # ============================================================

    async def place_bet(
        self,
        race_id: str,
        bet_type: str,
        selection: Sequence[Any],
        stake: Any,
        user_id: Optional[str] = None,
        ticket_number: Optional[str] = None,
        agent_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> GameBet:
        """Accepte un pari : contrôles serveur, cote figée, débit, enregistrement.

        Règle métier : les paris se prennent UNIQUEMENT au bureau, par un agent
        (pour un compte joueur ou pour un ticket)."""
        if not agent_id:
            raise ValidationException(self.MSG["agent_only"])
        if bool(user_id) == bool(ticket_number):
            raise ValidationException("Un pari est financé par un compte OU par un ticket")

        try:
            stake = Decimal(str(stake)).quantize(Decimal("0.01"), ROUND_DOWN)
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationException("Mise invalide")

        # Verrou PARTAGÉ (FOR SHARE) : un nombre illimité de paris peuvent être
        # pris en même temps sur la même manche ; seules la fermeture des paris
        # et le départ (verrou exclusif) attendent qu'ils soient terminés.
        race = await self._get_race_for_share(race_id)
        now = now_utc()
        if race.status != GameRoundStatus.BETTING_OPEN.value or (race.betting_closes_at and now >= race.betting_closes_at):
            raise GameException(self.MSG["closed"])

        min_bet = Decimal(str(race.config.get("min_bet", 10)))
        max_bet = Decimal(str(race.config.get("max_bet", 10000)))
        if stake < min_bet or stake > max_bet:
            raise GameException(f"Mise invalide. Min : {min_bet} HTG, Max : {max_bet} HTG")

        bet_type = (bet_type or "").upper()
        chosen, odds = self.quote(race, bet_type, selection)
        bet_id = str(uuid.uuid4())
        bet = GameBet(
            id=bet_id,
            round_id=race.id,
            game_type=self.GAME_TYPE,
            agent_id=agent_id,
            bet_type=bet_type,
            selection=chosen,
            stake=stake,
            odds=odds,
            potential_payout=self.potential_payout(race, bet_type, stake, odds),
            status=GameBetStatus.PENDING.value,
            placed_at=now,
        )

        if user_id:
            transaction = await self.wallet_service.debit_for_bet(
                user_id=user_id, amount=stake, bet_id=bet_id, draw_id=race.id, reference=f"BET-{self.REF}-{bet_id}",
            )
            bet.user_id = user_id
            bet.debit_transaction_id = transaction.id
            user = await self.db.get(User, user_id)
            if user:
                user.total_bets_count = (user.total_bets_count or 0) + 1
                user.total_bets_amount = (user.total_bets_amount or Decimal("0")) + stake
        else:
            ticket = await self._get_ticket_by_number_for_update(ticket_number)
            if ticket.status != TicketStatus.ACTIVE:
                raise GameException("Ticket inactif")
            if ticket.expires_at and ticket.expires_at < now:
                raise GameException("Ticket expiré")
            if ticket.balance < stake:
                raise InsufficientBalanceException(float(stake), float(ticket.balance))
            ticket.balance -= stake
            bet.ticket_id = ticket.id

        self.db.add(bet)
        # Pas de compteur incrémenté sur la manche ici : ce serait une ligne
        # unique écrite par tous les parieurs à la fois (file d'attente). Les
        # totaux sont recalculés à la fermeture / au départ / au règlement.
        await self.db.flush()

        await self.audit_service.log(
            action=AuditAction.BET_PLACED,
            user_id=user_id,
            agent_id=agent_id,
            resource_type=self.BET_RESOURCE,
            resource_id=bet.id,
            ip_address=ip_address,
            new_values={
                "race_id": race.id,
                "race_number": race.round_number,
                "bet_type": bet_type,
                "selection": chosen,
                "stake": float(stake),
                "odds": float(odds),
                "ticket": ticket_number,
            },
            extra_data={"event": "bet_placed"},
        )
        self.logger.info(f"🎟️ {self.GAME_TYPE} : pari {bet_type} {chosen} {stake} HTG @ {odds} sur #{race.round_number}")
        return bet

    # ============================================================
    # Cycle automatique (appelé par Celery toutes les quelques secondes)
    # ============================================================

    async def tick(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """Fait avancer les manches selon l'heure. Chaque transition est
        protégée par le verrou de la manche et le contrôle de statut : deux
        appels simultanés ne font rien deux fois."""
        now = now or now_utc()
        counts = {"opened": 0, "closed": 0, "started": 0, "finished": 0, "settled": 0, "created": 0}

        async def ids(where) -> List[str]:
            result = await self.db.execute(select(GameRound.id).where(GameRound.game_type == self.GAME_TYPE, *where))
            return list(result.scalars().all())

        for race_id in await ids([
            GameRound.status == GameRoundStatus.SCHEDULED.value,
            GameRound.betting_closes_at > now,
            GameRound.scheduled_at > now,
        ]):
            race = await self.get_race(race_id)
            # Une manche créée « programmée » sans heure d'ouverture attend l'admin
            if race.betting_opens_at is not None and race.betting_opens_at <= now:
                await self.open_betting(race_id)
                counts["opened"] += 1

        for race_id in await ids([
            GameRound.status == GameRoundStatus.BETTING_OPEN.value,
            GameRound.betting_closes_at <= now,
        ]):
            await self.close_betting(race_id, now=now)
            counts["closed"] += 1

        for race_id in await ids([GameRound.status.in_([
            GameRoundStatus.SCHEDULED.value, GameRoundStatus.BETTING_OPEN.value, GameRoundStatus.BETTING_CLOSED.value,
        ]), GameRound.scheduled_at <= now]):
            await self.start_race(race_id, now=now)
            counts["started"] += 1

        for race_id in await ids([GameRound.status == GameRoundStatus.RUNNING.value]):
            race = await self.get_race(race_id)
            duration = timedelta(milliseconds=self.round_duration_ms(race))
            if race.started_at and race.started_at + duration <= now:
                await self.finish_race(race_id, now=now)
                counts["finished"] += 1

        for race_id in await ids([GameRound.status == GameRoundStatus.FINISHED.value]):
            await self.settle_race(race_id)
            counts["settled"] += 1

        config = await self.get_config()
        if self.auto_create_allowed(config):
            upcoming = await ids([GameRound.status.in_([
                GameRoundStatus.SCHEDULED.value, GameRoundStatus.BETTING_OPEN.value,
            ])])
            if not upcoming:
                await self.create_race(scheduled_at=self.next_scheduled_at(config, now))
                counts["created"] += 1
        return counts

    def next_scheduled_at(self, config: Dict[str, Any], now: datetime) -> datetime:
        """Heure de départ de la prochaine manche créée automatiquement."""
        return now + timedelta(minutes=int(config["interval_minutes"]))

    def auto_create_allowed(self, config: Dict[str, Any]) -> bool:
        return bool(config.get("auto_enabled")) and self._within_opening_hours(config)

    @staticmethod
    def _within_opening_hours(config: Dict[str, Any]) -> bool:
        hour = now_haiti().hour
        return int(config.get("open_hour", 0)) <= hour < int(config.get("close_hour", 24))

    # ============================================================
    # Validation + diffusion
    # ============================================================

    async def commit_and_publish(self) -> None:
        await self.db.commit()
        events, self._events = self._events, []
        if not events:
            return
        from app.api.websockets.manager import publish
        for message in events:
            try:
                await publish(message, draw_id="all")
            except Exception as e:  # la diffusion ne doit jamais annuler l'opération
                self.logger.error(f"Diffusion {self.GAME_TYPE} impossible : {e}")

    # ============================================================
    # Internes
    # ============================================================

    def _emit(self, race: GameRound, event: str) -> None:
        self._events.append({
            "type": self.WS_TYPE,
            "event": event,
            "data": self.serialize_race(race),
            "timestamp": _iso(now_utc()),
        })

    def _require_status(self, race: GameRound, allowed: Sequence[str]) -> None:
        if race.status not in allowed:
            messages = {
                GameRoundStatus.FINISHED.value: self.MSG["finished"],
                GameRoundStatus.SETTLED.value: self.MSG["settled"],
                GameRoundStatus.CANCELLED.value: self.MSG["cancelled"],
                GameRoundStatus.RUNNING.value: self.MSG["running"],
            }
            raise GameException(messages.get(race.status, self.MSG["status"].format(status=race.status)))

    async def _get_ticket_for_update(self, ticket_id: str) -> Ticket:
        result = await self.db.execute(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
        ticket = result.scalar_one_or_none()
        if ticket is None:
            raise NotFoundException("Ticket", ticket_id)
        return ticket

    async def _get_ticket_by_number_for_update(self, ticket_number: str) -> Ticket:
        result = await self.db.execute(
            select(Ticket).where(Ticket.ticket_number == ticket_number).with_for_update()
        )
        ticket = result.scalar_one_or_none()
        if ticket is None:
            raise NotFoundException("Ticket", ticket_number)
        return ticket

    async def _audit_race(
        self, race: GameRound, event: str, by: Optional[str] = None, new_values: Optional[Dict[str, Any]] = None
    ) -> None:
        await self.audit_service.log(
            action=AuditAction.DRAW_GENERATED,
            agent_id=by,
            resource_type=self.AUDIT_RESOURCE,
            resource_id=race.id,
            new_values=new_values or {"status": race.status},
            extra_data={"event": event, "race_number": race.round_number},
        )


def _mark(bet: GameBet, **values) -> None:
    """Reporte sur l'objet en mémoire les valeurs déjà écrites en base par
    l'UPDATE conditionnel (sans relire le pari, sans réécriture)."""
    for key, value in values.items():
        set_committed_value(bet, key, value)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Datetime UTC naive -> ISO 8601 avec « Z » (lu correctement par le navigateur)."""
    return value.isoformat() + "Z" if value else None
