# app/services/keno_service.py
"""Service complet pour le jeu Keno.

Point d'entrée UNIQUE pour la prise de paris, le tirage, le règlement, les
remboursements et la configuration (modifiable par l'admin à tout moment).

Deux modes (configuration « mode ») :
- « scheduled » (par défaut) : tirages PARTAGÉS par tous les bureaux, toutes
  les N minutes, calés sur l'horloge. Réglages figés dans le tirage à sa
  création (keno_draws.config) ; paris pris au bureau jusqu'à N secondes
  avant l'heure ; tirage + règlement par le worker (tick) ; les écrans
  dévoilent les 20 boules au rythme de l'horloge du serveur.
- « instant » (option) : au bureau, un tirage par ticket. L'empreinte du
  seed du tirage est affichée AVANT le pari ; le pari, le débit, le tirage,
  le règlement et le crédit éventuel se font dans UNE transaction ; le seed
  est révélé sur le reçu (vérifiable).

Règles d'argent :
- tirage verrouillé (FOR UPDATE) pendant le tirage / règlement ;
- débit du compte : WalletService.debit_for_bet (verrou du portefeuille,
  limites de jeu responsable), référence unique BET-KENO-<pari> ;
- débit / crédit d'un ticket : ligne du ticket verrouillée ;
- gain : référence unique WIN-KENO-<pari> ; remboursement : REFUND-KENO-<pari>
  (la base refuse un second paiement) ;
- seuls les paris PENDING sont réglés ou remboursés.
"""

import json
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

import redis.asyncio as redis
from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    GameException,
    InsufficientBalanceException,
    NotFoundException,
    ValidationException,
)
from app.core.logger import get_logger
from app.core.timezone import now_utc
from app.models.enums import TicketStatus
from app.models.keno import KenoBet, KenoBetStatus, KenoDraw, KenoDrawStatus
from app.models.ticket import Ticket
from app.models.user import User
from app.schemas.keno import KenoBetCreate
from app.services import keno_engine as engine
from app.services.audit_service import AuditAction, AuditService
from app.services.base import BaseService
from app.services.fairness import new_server_seed, seed_hash
from app.services.rng_service import RNGService
from app.services.wallet_service import WalletService

CONFIG_KEY = "settings:keno"
PREPARED_KEY = "keno:prepared:{agent_id}"
MODE_INSTANT = "instant"
MODE_SCHEDULED = "scheduled"
KENO_NUMBER_LOCK = 0x4B454E4F  # « KENO » : verrou consultatif PostgreSQL (numérotation)


class KenoService(BaseService[KenoDraw, None, None]):
    """
    Service complet pour le jeu Keno.
    Gère les tirages, les paris, et le règlement.
    """

    # Valeurs par défaut (la configuration admin les remplace : get_config)
    TOTAL_NUMBERS = engine.TOTAL_NUMBERS
    DRAWN_COUNT = engine.DRAWN_COUNT
    MIN_PICKS = 1
    MAX_PICKS = engine.HARD_MAX_PICKS
    MIN_BET = Decimal("10")
    MAX_BET = Decimal("100000")
    # Mode planifié : les paris ferment N secondes avant l'heure du tirage
    BETTING_CLOSE_SECONDS = 10
    # Heures d'ouverture des tirages planifiés (heure d'Haïti)
    OPEN_HOUR = 8
    CLOSE_HOUR = 23
    # Table de paiement d'origine (la configuration admin la remplace)
    PAYTABLE = engine.DEFAULT_PAYTABLE

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, KenoDraw)
        self.redis = redis_client
        self.rng = RNGService()
        self.audit_service = AuditService(db, redis_client)
        self.wallet_service = WalletService(db, redis_client)
        self.logger = get_logger("KenoService")

    # ========== Configuration ==========

    async def get_config(self) -> Dict[str, Any]:
        raw = None
        try:
            raw = await self.redis.get(CONFIG_KEY)
        except Exception as e:
            self.logger.warning(f"Configuration Keno illisible, valeurs par défaut : {e}")
        if raw:
            try:
                return engine.validate_config(json.loads(raw))
            except Exception:
                self.logger.error("Configuration Keno invalide en Redis : valeurs par défaut utilisées")
        return engine.validate_config({})

    async def save_config(self, config: Dict[str, Any], admin_id: Optional[str] = None) -> Dict[str, Any]:
        try:
            clean = engine.validate_config(config)
        except engine.KenoConfigError as e:
            raise ValidationException(str(e))
        old = await self.get_config()
        await self.redis.set(CONFIG_KEY, json.dumps(clean))  # pas d'expiration
        await self._refresh_unbet_snapshots(clean)
        await self.audit_service.log(
            action=AuditAction.LIMIT_CHANGED,
            agent_id=admin_id,
            resource_type="keno_config",
            old_values=old,
            new_values=clean,
            extra_data={"event": "keno_config_updated"},
        )
        return clean

    @staticmethod
    def paytable_of(config: Dict[str, Any]) -> Dict[int, Dict[int, Decimal]]:
        return engine.parse_paytable(config["paytable"])

    @staticmethod
    def draw_config(draw: KenoDraw, config: Dict[str, Any]) -> Dict[str, Any]:
        """Réglages d'un tirage : ceux figés à sa création (tirage partagé),
        sinon la configuration courante (tirage instantané, anciens tirages)."""
        return {**config, **(draw.config or {})}

    async def _refresh_unbet_snapshots(self, config: Dict[str, Any]) -> int:
        """Après une modification de l'admin : les tirages partagés à venir
        qui n'ont encore AUCUN pari prennent les nouveaux réglages. Un tirage
        qui a des paris garde les siens (ses tickets ont été acceptés avec)."""
        has_bets = select(KenoBet.id).where(KenoBet.draw_id == KenoDraw.id).exists()
        ids = (await self.db.execute(
            select(KenoDraw.id).where(
                KenoDraw.status == KenoDrawStatus.PENDING, KenoDraw.mode == MODE_SCHEDULED, ~has_bets,
                KenoDraw.is_deleted == False,  # noqa: E712
            )
        )).scalars().all()
        count = 0
        for draw_id in ids:
            draw = await self._get_draw_for_update(draw_id)  # attend les paris en cours
            n = (await self.db.execute(select(func.count(KenoBet.id)).where(KenoBet.draw_id == draw_id))).scalar()
            if draw.status == KenoDrawStatus.PENDING and not n:
                draw.config = engine.snapshot(config)
                count += 1
        await self.db.flush()
        return count

    # ========== Tirages ==========

    async def generate_draw(
        self, draw_time: Optional[datetime] = None, mode: str = MODE_SCHEDULED, config: Optional[Dict[str, Any]] = None,
    ) -> KenoDraw:
        """Crée un tirage en attente (numéro = dernier + 1), avec son seed
        secret : l'empreinte est publiable immédiatement."""
        seed = new_server_seed()
        for attempt in range(5):
            try:
                async with self.db.begin_nested():
                    draw = KenoDraw(
                        draw_number=await self.next_draw_number(),
                        draw_time=draw_time or now_utc(),
                        status=KenoDrawStatus.PENDING,
                        mode=mode,
                        server_seed=seed,
                        server_seed_hash=seed_hash(seed),
                        config=engine.snapshot(config) if config else None,
                    )
                    self.db.add(draw)
                    await self.db.flush()
                break
            except IntegrityError:
                # deux bureaux ont pris le même numéro au même instant : on recommence
                if attempt == 4:
                    raise
        self.logger.info(f"Draw generated: #{draw.draw_number} ({mode})")
        return draw

    async def next_draw_number(self) -> int:
        """Numéro suivant. Sous PostgreSQL, un verrou de transaction réservé au
        Keno sérialise l'attribution : plusieurs bureaux qui préparent leur
        tirage au même instant obtiennent des numéros différents (le verrou
        est libéré au commit, quelques millisecondes plus tard)."""
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            await self.db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": KENO_NUMBER_LOCK})
        result = await self.db.execute(select(func.max(KenoDraw.draw_number)))
        return (result.scalar() or 0) + 1

    async def _get_draw_for_update(self, draw_id: str) -> KenoDraw:
        """Charge un tirage en le verrouillant (SELECT … FOR UPDATE) : deux
        règlements concurrents (worker + admin) se sérialisent, et les prises
        de paris (verrou partagé) attendent la fin du règlement."""
        result = await self.db.execute(
            select(KenoDraw)
            .where(KenoDraw.id == draw_id, KenoDraw.is_deleted == False)
            .with_for_update()
        )
        draw = result.scalar_one_or_none()
        if draw is None:
            raise NotFoundException("Tirage", draw_id)
        return draw

    async def _get_draw_for_share(self, draw_id: str) -> KenoDraw:
        """Verrou PARTAGÉ (FOR SHARE) : un nombre illimité de paris peuvent être
        pris en même temps sur le même tirage planifié ; seul le tirage
        (verrou exclusif) attend qu'ils soient enregistrés."""
        result = await self.db.execute(
            select(KenoDraw)
            .where(KenoDraw.id == draw_id, KenoDraw.is_deleted == False)
            .with_for_update(read=True)
        )
        draw = result.scalar_one_or_none()
        if draw is None:
            raise NotFoundException("Tirage", draw_id)
        return draw

    async def schedule_draws(self, hours: int = 24, now: Optional[datetime] = None) -> int:
        """Mode partagé : crée les tirages des prochaines `hours` heures, alignés
        sur l'intervalle, pendant les heures d'ouverture (heure d'Haïti).
        Idempotent. Ne fait rien en mode instantané. (Le cycle automatique
        utilise schedule_next, qui ne crée que les prochains tirages.)"""
        config = await self.get_config()
        if config["mode"] != MODE_SCHEDULED:
            return 0
        now = now or now_utc()
        return await self._create_slots(config, now, now + timedelta(hours=hours), limit=None)

    async def schedule_next(self, now: Optional[datetime] = None, count: int = 2) -> int:
        """Cycle automatique : garde les `count` prochains tirages partagés
        créés (réglages figés à leur création, empreinte publiée). Créer peu
        à l'avance permet aux modifications de l'admin de s'appliquer vite."""
        config = await self.get_config()
        if config["mode"] != MODE_SCHEDULED or not config["enabled"]:
            return 0
        now = now or now_utc()
        upcoming = (await self.db.execute(
            select(func.count(KenoDraw.id)).where(
                KenoDraw.status == KenoDrawStatus.PENDING, KenoDraw.mode == MODE_SCHEDULED,
                KenoDraw.draw_time > now, KenoDraw.is_deleted == False,  # noqa: E712
            )
        )).scalar() or 0
        if upcoming >= count:
            return 0
        horizon = now + timedelta(minutes=int(config["interval_minutes"]) * (count + 1))
        return await self._create_slots(config, now, horizon, limit=count - upcoming)

    @staticmethod
    def first_slot(config: Dict[str, Any], now: datetime) -> datetime:
        """Premier créneau (calé sur l'horloge) laissant au moins 1 minute de paris."""
        step = int(config["interval_minutes"]) * 60
        earliest = now + timedelta(seconds=int(config["betting_close_seconds"]) + 60)
        midnight = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
        slots = -(-int((earliest - midnight).total_seconds()) // step)
        return midnight + timedelta(seconds=slots * step)

    async def _create_slots(self, config: Dict[str, Any], now: datetime, end: datetime, limit: Optional[int]) -> int:
        from app.core.timezone import to_haiti

        existing = await self.db.execute(
            select(KenoDraw.draw_time).where(
                KenoDraw.draw_time > now, KenoDraw.draw_time <= end, KenoDraw.is_deleted == False,  # noqa: E712
                KenoDraw.mode == MODE_SCHEDULED, KenoDraw.status != KenoDrawStatus.CANCELLED,
            )
        )
        existing_times = {t.replace(second=0, microsecond=0) for t in existing.scalars().all()}
        last = (await self.db.execute(
            select(func.max(KenoDraw.draw_time)).where(
                KenoDraw.mode == MODE_SCHEDULED, KenoDraw.status != KenoDrawStatus.CANCELLED,
                KenoDraw.is_deleted == False,  # noqa: E712
            )
        )).scalar()

        current = self.first_slot(config, now)
        step = timedelta(minutes=int(config["interval_minutes"]))
        if last is not None and last >= current:
            current = last + step  # jamais deux tirages sur le même créneau
        created = 0
        while current <= end and (limit is None or created < limit):
            local_hour = to_haiti(current).hour
            if current not in existing_times and int(config["open_hour"]) <= local_hour < int(config["close_hour"]):
                await self.generate_draw(draw_time=current, mode=MODE_SCHEDULED, config=config)
                created += 1
            current += step
        await self.db.flush()
        return created

    async def cancel_draw(self, draw_id: str, by: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        """Annule un tirage EN ATTENTE et rembourse ses paris (compte : REFUND-KENO-<pari>,
        ticket : solde recrédité). Idempotent : un pari remboursé ne l'est pas deux fois."""
        draw = await self._get_draw_for_update(draw_id)
        if draw.status == KenoDrawStatus.CANCELLED:
            return {"draw_id": draw.id, "draw_number": draw.draw_number, "refunded_bets": 0, "already_cancelled": True}
        if draw.status != KenoDrawStatus.PENDING:
            raise GameException("Ce tirage a déjà eu lieu : il ne peut pas être annulé")

        bets = (await self.db.execute(
            select(KenoBet).where(KenoBet.draw_id == draw.id, KenoBet.status == KenoBetStatus.PENDING)
        )).scalars().all()
        refunded = Decimal("0")
        now = now_utc()
        for bet in bets:
            if bet.user_id:
                await self.wallet_service.credit_refund(
                    user_id=bet.user_id, amount=bet.stake, bet_id=bet.id, draw_id=draw.id,
                    reference=f"REFUND-KENO-{bet.id}",
                )
            elif bet.ticket_id:
                await self._credit_ticket(bet.ticket_id, bet.stake)
            bet.status = KenoBetStatus.REFUNDED
            bet.settled_at = now
            refunded += bet.stake

        draw.status = KenoDrawStatus.CANCELLED
        draw.closed_at = now
        draw.closed_by = by or "system"
        await self.db.flush()
        await self._refresh_totals(draw)
        await self.audit_service.log(
            action=AuditAction.DRAW_GENERATED,
            agent_id=by if by and by != "system" else None,
            resource_type="keno_draw",
            resource_id=draw.id,
            reason=reason or None,
            new_values={"draw_number": draw.draw_number, "refunded_bets": len(bets), "refunded_amount": float(refunded)},
            extra_data={"event": "keno_draw_cancelled"},
        )
        return {"draw_id": draw.id, "draw_number": draw.draw_number, "refunded_bets": len(bets),
                "refunded_amount": float(refunded), "already_cancelled": False}

    async def cancel_pending_draws_without_bets(self, older_than: Optional[datetime] = None, by: str = "system") -> int:
        """Annule les tirages en attente SANS pari (ex. tirages instantanés
        préparés mais jamais joués), optionnellement plus vieux qu'une date."""
        has_bets = select(KenoBet.id).where(KenoBet.draw_id == KenoDraw.id).exists()
        query = select(KenoDraw.id).where(KenoDraw.status == KenoDrawStatus.PENDING, ~has_bets)
        if older_than is not None:
            query = query.where(KenoDraw.draw_time < older_than)
        count = 0
        for draw_id in (await self.db.execute(query)).scalars().all():
            draw = await self._get_draw_for_update(draw_id)
            has = (await self.db.execute(select(func.count(KenoBet.id)).where(KenoBet.draw_id == draw_id))).scalar()
            if draw.status != KenoDrawStatus.PENDING or has:
                continue  # un pari est arrivé entre-temps : on garde le tirage
            draw.status = KenoDrawStatus.CANCELLED
            draw.closed_at = now_utc()
            draw.closed_by = by
            count += 1
        await self.db.flush()
        return count

    def betting_closes_at(self, draw: KenoDraw) -> datetime:
        seconds = (draw.config or {}).get("betting_close_seconds", self.BETTING_CLOSE_SECONDS)
        return draw.draw_time - timedelta(seconds=int(seconds))

    def is_betting_open(self, draw: KenoDraw, now: Optional[datetime] = None) -> bool:
        now = now or now_utc()
        return draw.status == KenoDrawStatus.PENDING and now < self.betting_closes_at(draw)

    async def execute_draw(self, draw_id: str) -> Dict[str, Any]:
        """Exécute un tirage EN ATTENTE et règle tous ses paris."""
        draw = await self._get_draw_for_update(draw_id)
        if draw.status != KenoDrawStatus.PENDING:
            raise GameException(f"Tirage déjà {draw.status}")
        return await self.settle_bets_for_draw(draw_id, _draw=draw)

    def _numbers_for(self, draw: KenoDraw) -> List[int]:
        """Résultat du tirage : dérivé du seed (vérifiable) ; tirages antérieurs
        à la migration (sans seed) : générateur cryptographique."""
        if draw.server_seed:
            return engine.draw_numbers(draw.server_seed, draw.draw_number)
        return self.rng.generate_keno_numbers()

    async def settle_bets_for_draw(
        self, draw_id: str, _draw: Optional[KenoDraw] = None, commit: bool = True
    ) -> Dict[str, Any]:
        """Tire les numéros si nécessaire, puis règle les paris encore en attente.

        Idempotent :
        - tirage verrouillé pendant le règlement ;
        - seuls les paris PENDING sont réglés (un pari réglé n'est plus repris) ;
        - chaque gain est crédité avec la référence unique WIN-KENO-<bet_id>,
          que la base refuse d'enregistrer deux fois.
        Les totaux du tirage sont recalculés à partir de TOUS ses paris.
        """
        draw = _draw or await self._get_draw_for_update(draw_id)

        if draw.status == KenoDrawStatus.CANCELLED:
            raise GameException("Ce tirage a été annulé")

        just_drawn = False
        if draw.status == KenoDrawStatus.PENDING or not draw.numbers:
            draw.numbers = self._numbers_for(draw)
            draw.status = KenoDrawStatus.COMPLETED
            draw.closed_at = now_utc()
            just_drawn = True
        numbers = [int(n) for n in draw.numbers]
        self.validate_draw_numbers(numbers)

        config = self.draw_config(draw, await self.get_config())  # table figée du tirage
        paytable = self.paytable_of(config)
        max_payout = Decimal(str(config["max_payout"]))

        bets_result = await self.db.execute(
            select(KenoBet).where(
                and_(KenoBet.draw_id == draw.id, KenoBet.status == KenoBetStatus.PENDING)
            )
        )
        bets = bets_result.scalars().all()

        settled_payout = Decimal("0")
        winners: List[KenoBet] = []
        now = now_utc()

        for bet in bets:
            winnings, hits = self._calculate_winnings(bet.picks, numbers, bet.stake, paytable, max_payout)

            bet.hits = hits
            bet.multiplier = engine.multiplier(paytable, len(bet.picks), hits)
            bet.winnings = winnings
            bet.status = KenoBetStatus.WON if winnings > 0 else KenoBetStatus.LOST
            bet.settled_at = now

            if winnings > 0:
                winners.append(bet)
                settled_payout += winnings
                if bet.user_id:
                    await self.wallet_service.credit_for_win(
                        user_id=bet.user_id,
                        amount=winnings,
                        bet_id=bet.id,
                        draw_id=draw.id,
                        reference=f"WIN-KENO-{bet.id}",
                    )
                    await self._update_user_stats(bet.user_id, winnings, is_bet=False)
                elif bet.ticket_id:
                    await self._credit_ticket(bet.ticket_id, winnings)

        await self.db.flush()
        await self._refresh_totals(draw)

        await self.audit_service.log(
            action=AuditAction.DRAW_GENERATED,
            resource_type="keno_draw",
            resource_id=draw.id,
            new_values={
                "draw_number": draw.draw_number,
                "mode": draw.mode,
                "numbers": numbers,
                "settled_bets": len(bets),
                "total_payout": float(settled_payout),
            },
            extra_data={"event": "keno_draw_finished" if just_drawn else "keno_draw_resettled"},
        )
        if commit:
            await self.db.commit()  # règlement + journal d'audit validés ensemble

        self.logger.info(f"Draw settled: #{draw.draw_number} - {len(bets)} bets, {settled_payout} payout")

        return {
            "draw_id": draw.id,
            "draw_number": draw.draw_number,
            "numbers": numbers,
            "total_bets": int(draw.total_bets or 0),
            "settled_bets": len(bets),
            "total_payout": float(draw.total_payout or 0),
            "winners_count": len(winners),
            "winner_bet_ids": [b.id for b in winners],
            "just_drawn": just_drawn,
        }

    async def _refresh_totals(self, draw: KenoDraw) -> None:
        """Totaux du tirage recalculés depuis ses paris (jamais incrémentés
        pari par pari : ce serait une ligne écrite par tous les parieurs à la fois).
        Les paris remboursés ne comptent pas."""
        result = await self.db.execute(
            select(
                func.count(KenoBet.id),
                func.coalesce(func.sum(KenoBet.stake), 0),
                func.coalesce(func.sum(KenoBet.winnings), 0),
            ).where(KenoBet.draw_id == draw.id, KenoBet.status != KenoBetStatus.REFUNDED)
        )
        count, stake_total, payout_total = result.one()
        draw.total_bets = int(count or 0)
        draw.total_amount = Decimal(str(stake_total or 0))
        draw.total_payout = Decimal(str(payout_total or 0))

    async def live_totals(self, draw_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Nombre de paris et mises en cours, pour l'affichage des tirages ouverts."""
        draw_ids = list(draw_ids)
        if not draw_ids:
            return {}
        result = await self.db.execute(
            select(KenoBet.draw_id, func.count(KenoBet.id), func.coalesce(func.sum(KenoBet.stake), 0))
            .where(KenoBet.draw_id.in_(draw_ids))
            .group_by(KenoBet.draw_id)
        )
        return {d: {"total_bets": int(n), "total_amount": Decimal(str(s))} for d, n, s in result.all()}

    # ========== Validation ==========

    @classmethod
    def validate_picks(cls, picks: Any, min_picks: Optional[int] = None, max_picks: Optional[int] = None) -> List[int]:
        """Numéros joués : entiers de 1 à 80, sans doublon, en nombre autorisé.
        Renvoie la liste triée (lève ValidationException sinon)."""
        min_picks = cls.MIN_PICKS if min_picks is None else min_picks
        max_picks = cls.MAX_PICKS if max_picks is None else max_picks
        if not isinstance(picks, (list, tuple)):
            raise ValidationException("Numéros invalides")
        clean: List[int] = []
        for value in picks:
            if isinstance(value, bool):
                raise ValidationException("Numéros invalides")
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValidationException("Numéros invalides")
            if number != value and str(number) != str(value).strip():
                raise ValidationException("Numéros invalides")
            if not 1 <= number <= cls.TOTAL_NUMBERS:
                raise ValidationException(f"Les numéros doivent être entre 1 et {cls.TOTAL_NUMBERS}")
            clean.append(number)
        if len(set(clean)) != len(clean):
            raise ValidationException("Un numéro ne peut être choisi qu'une fois")
        if not min_picks <= len(clean) <= max_picks:
            raise ValidationException(f"Choisissez entre {min_picks} et {max_picks} numéros")
        return sorted(clean)

    @classmethod
    def validate_stake(cls, stake: Any, min_bet: Any = None, max_bet: Any = None) -> Decimal:
        min_bet = cls.MIN_BET if min_bet is None else Decimal(str(min_bet))
        max_bet = cls.MAX_BET if max_bet is None else Decimal(str(max_bet))
        try:
            value = Decimal(str(stake))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationException("Mise invalide")
        if not value.is_finite() or value != value.quantize(Decimal("0.01"), ROUND_DOWN):
            raise ValidationException("Mise invalide (2 décimales maximum)")
        if value < min_bet or value > max_bet:
            raise ValidationException(f"Mise invalide. Min : {min_bet:g} HTG, Max : {max_bet:g} HTG")
        return value

    @classmethod
    def validate_draw_numbers(cls, numbers: List[int]) -> None:
        """Garde-fou avant tout paiement : exactement 20 numéros uniques de 1 à 80."""
        if not engine.is_valid_draw(numbers):
            raise GameException("Résultat de tirage invalide : règlement refusé")

    # ========== Paris : mode instantané (au bureau) ==========

    async def prepare_instant_draw(self, agent_id: str) -> KenoDraw:
        """Tirage instantané prêt pour le prochain ticket de ce bureau : son
        empreinte (sha256 du seed) est affichée avant le pari. Réutilise le
        tirage déjà préparé tant qu'il n'a pas été joué. L'appelant valide."""
        key = PREPARED_KEY.format(agent_id=agent_id)
        try:
            prepared_id = await self.redis.get(key)
        except Exception:
            prepared_id = None
        if prepared_id:
            draw = await self.db.get(KenoDraw, prepared_id)
            if draw is not None and draw.status == KenoDrawStatus.PENDING and draw.mode == MODE_INSTANT:
                used = (await self.db.execute(select(func.count(KenoBet.id)).where(KenoBet.draw_id == draw.id))).scalar()
                if not used:
                    return draw
        draw = await self.generate_draw(mode=MODE_INSTANT)
        try:
            await self.redis.set(key, draw.id, ex=3600)
        except Exception:
            pass
        return draw

    async def play_instant(
        self,
        draw_id: str,
        picks: Any,
        stake: Any,
        agent_id: str,
        user_id: Optional[str] = None,
        ticket_number: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Un ticket = un tirage. Dans UNE transaction : contrôles, débit, pari,
        tirage (seed engagé à l'avance), règlement, crédit éventuel, commit.
        En cas d'erreur, rien n'est débité (l'appelant annule la transaction)."""
        if not agent_id:
            raise ValidationException("Les paris Keno se prennent uniquement chez un agent")
        config = await self.get_config()
        if not config["enabled"]:
            raise GameException("Le Keno est momentanément fermé")
        if config["mode"] != MODE_INSTANT:
            raise GameException("Le Keno instantané n'est pas activé")

        draw = await self._get_draw_for_update(draw_id)
        if draw.mode != MODE_INSTANT or not draw.server_seed:
            raise GameException("Tirage invalide pour un jeu instantané")
        if draw.status != KenoDrawStatus.PENDING:
            raise GameException("Ce tirage a déjà été joué : rechargez la page")
        used = (await self.db.execute(select(func.count(KenoBet.id)).where(KenoBet.draw_id == draw.id))).scalar()
        if used:
            raise GameException("Ce tirage a déjà été joué : rechargez la page")

        bet = await self._new_bet(draw, picks, stake, config, user_id, ticket_number, agent_id, ip_address)
        draw.draw_time = now_utc()  # heure réelle du tirage
        await self.settle_bets_for_draw(draw.id, _draw=draw, commit=False)
        await self.db.commit()

        try:
            await self.redis.delete(PREPARED_KEY.format(agent_id=agent_id))
        except Exception:
            pass
        return self.serialize_instant(draw, bet, config)

    def serialize_instant(self, draw: KenoDraw, bet: KenoBet, config: Dict[str, Any]) -> Dict[str, Any]:
        numbers = [int(n) for n in draw.numbers or []]
        picks = [int(n) for n in bet.picks]
        return {
            "bet_id": bet.id,
            "status": bet.status.value if hasattr(bet.status, "value") else bet.status,
            "draw_id": draw.id,
            "draw_number": draw.draw_number,
            "drawn_at": draw.draw_time.isoformat() + "Z" if draw.draw_time else None,
            "picks": picks,
            "spots": len(picks),
            "stake": float(bet.stake),
            "winning_numbers": numbers,
            "draw_order": engine.draw_order(draw.server_seed, draw.draw_number) if draw.server_seed else numbers,
            "matches": engine.matches(picks, numbers),
            "match_count": int(bet.hits or 0),
            "multiplier": float(bet.multiplier or 0),
            "payout": float(bet.winnings or 0),
            "max_payout": config["max_payout"],
            "server_seed_hash": draw.server_seed_hash,
            "server_seed": draw.server_seed if draw.status != KenoDrawStatus.PENDING else None,
            "ticket_id": bet.ticket_id,
        }

    # ========== Écran joueur (au guichet) ==========

    @staticmethod
    def screen_code(agent_id: str) -> str:
        """Code de l'écran joueur d'un guichet (voir app.services.counter_screen)."""
        from app.services import counter_screen

        return counter_screen.screen_code("keno", agent_id)

    @staticmethod
    def is_screen_code(code: str) -> bool:
        from app.services import counter_screen

        return counter_screen.is_screen_code(code)

    async def screen_preview(self, agent_id: str, picks: Any, stake: Any, prepared: Optional[KenoDraw]) -> Dict[str, Any]:
        """Ticket en préparation, montré au joueur pendant que l'agent le saisit.
        Affichage seulement : rien n'est joué ni débité ici."""
        config = await self.get_config()
        if not isinstance(picks, (list, tuple)):
            picks = []
        clean = sorted({int(n) for n in picks if isinstance(n, (int, float)) and not isinstance(n, bool) and 1 <= int(n) <= self.TOTAL_NUMBERS})
        clean = clean[: config["max_picks"]]
        try:
            amount = Decimal(str(stake)) if stake not in (None, "") else None
            if amount is not None and (not amount.is_finite() or amount <= 0):
                amount = None
        except (InvalidOperation, ValueError, TypeError):
            amount = None
        row = self.paytable_of(config).get(len(clean), {})
        cap = Decimal(str(config["max_payout"]))
        payload = {
            "event": "preview",
            "draw_number": prepared.draw_number if prepared else None,
            "server_seed_hash": prepared.server_seed_hash if prepared else None,
            "picks": clean,
            "spots": len(clean),
            "stake": float(amount) if amount is not None else None,
            "paytable": [
                {"hits": h, "multiplier": float(m), "payout": float(engine.payout(amount, m, cap)) if amount else None}
                for h, m in sorted(row.items(), reverse=True)
            ],
            "max_win": float(engine.payout(amount, max(row.values()), cap)) if amount and row else None,
        }
        return await self.publish_screen(agent_id, payload)

    async def screen_play(self, agent_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Ticket joué : le résultat (déjà réglé en base) est animé sur l'écran joueur."""
        keys = ("draw_id", "draw_number", "server_seed_hash", "server_seed", "picks", "spots", "stake",
                "draw_order", "winning_numbers", "matches", "match_count", "multiplier", "payout", "drawn_at")
        config = await self.get_config()
        row = self.paytable_of(config).get(int(result.get("spots") or 0), {})
        max_win = float(engine.payout(Decimal(str(result["stake"])), max(row.values()), Decimal(str(config["max_payout"])))) if row else None
        return await self.publish_screen(agent_id, {"event": "play", "max_win": max_win, **{k: result.get(k) for k in keys}})

    async def publish_screen(self, agent_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        from app.services import counter_screen

        return await counter_screen.publish(self.redis, "keno", agent_id, payload)

    async def screen_state(self, code: str) -> Optional[Dict[str, Any]]:
        from app.services import counter_screen

        return await counter_screen.get_state(self.redis, "keno", code)

    # ========== Tirages partagés : état en direct, cycle automatique ==========

    @staticmethod
    def _iso(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() + "Z" if value else None

    def serialize_draw(self, draw: KenoDraw, config: Dict[str, Any]) -> Dict[str, Any]:
        """Tirage partagé tel que l'affichent le panel agent et les écrans.
        Numéros, ordre de sortie et seed : seulement une fois le tirage fait."""
        cfg = self.draw_config(draw, config)
        status = draw.status.value if hasattr(draw.status, "value") else draw.status
        done = status == KenoDrawStatus.COMPLETED.value and bool(draw.numbers)
        order = None
        if done:
            order = engine.draw_order(draw.server_seed, draw.draw_number) if draw.server_seed else [int(n) for n in draw.numbers]
        paytable = self.paytable_of(cfg)
        return {
            "draw_id": draw.id,
            "draw_number": draw.draw_number,
            "mode": draw.mode,
            "status": status,
            "draw_time": self._iso(draw.draw_time),
            "betting_closes_at": self._iso(self.betting_closes_at(draw)),
            "drawn_at": self._iso(draw.closed_at) if done else None,  # début de l'animation
            "server_time": self._iso(now_utc()),
            "server_seed_hash": draw.server_seed_hash,
            "server_seed": draw.server_seed if done or status == KenoDrawStatus.CANCELLED.value else None,
            "draw_order": order,
            "numbers": sorted(order) if order else None,
            "min_picks": cfg["min_picks"], "max_picks": cfg["max_picks"],
            "min_bet": cfg["min_bet"], "max_bet": cfg["max_bet"], "max_payout": cfg["max_payout"],
            "paytable": {str(sp): {str(h): float(m) for h, m in sorted(row.items())} for sp, row in paytable.items()},
            "intro_ms": int(cfg.get("intro_ms", 3000)),
            "ball_interval_ms": int(cfg.get("ball_interval_ms", 1500)),
            "outro_ms": int(cfg.get("outro_ms", 8000)),
            "duration_ms": engine.draw_duration_ms(cfg),
        }

    async def live_state(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Tirage à afficher (celui dont l'animation est en cours, sinon le
        prochain) et tirage qui prend les paris."""
        now = now or now_utc()
        config = await self.get_config()
        upcoming = list((await self.db.execute(
            select(KenoDraw).where(
                KenoDraw.mode == MODE_SCHEDULED, KenoDraw.status == KenoDrawStatus.PENDING,
                KenoDraw.is_deleted == False,  # noqa: E712
            ).order_by(KenoDraw.draw_time).limit(3)
        )).scalars().all())
        last = (await self.db.execute(
            select(KenoDraw).where(
                KenoDraw.mode == MODE_SCHEDULED, KenoDraw.status == KenoDrawStatus.COMPLETED,
            ).order_by(KenoDraw.draw_time.desc()).limit(1)
        )).scalar_one_or_none()
        display = None
        if last is not None and last.closed_at is not None:
            end = last.closed_at + timedelta(milliseconds=engine.draw_duration_ms(self.draw_config(last, config)))
            if now < end:
                display = last
        if display is None and upcoming:
            display = upcoming[0]
        betting = next((d for d in upcoming if now < self.betting_closes_at(d)), None)
        return {
            "draw": self.serialize_draw(display, config) if display else None,
            "betting_draw": self.serialize_draw(betting, config) if betting else None,
            "server_time": self._iso(now_utc()),
            "mode": config["mode"],
            "enabled": config["enabled"],
        }

    async def shared_history(self, limit: int = 10) -> List[KenoDraw]:
        result = await self.db.execute(
            select(KenoDraw).where(
                KenoDraw.mode == MODE_SCHEDULED,
                KenoDraw.status.in_([KenoDrawStatus.COMPLETED, KenoDrawStatus.CANCELLED]),
            ).order_by(KenoDraw.draw_time.desc()).limit(limit)
        )
        return list(result.scalars().all())

    def serialize_shared_bet(self, bet: KenoBet, draw: Optional[KenoDraw], config: Dict[str, Any]) -> Dict[str, Any]:
        status = bet.status.value if hasattr(bet.status, "value") else bet.status
        cfg = self.draw_config(draw, config) if draw is not None else config
        row = self.paytable_of(cfg).get(len(bet.picks), {})
        cap = Decimal(str(cfg["max_payout"]))
        done = draw is not None and bool(draw.numbers) and status != KenoBetStatus.PENDING.value
        drawn = set(int(n) for n in draw.numbers) if done else set()
        return {
            "bet_id": bet.id,
            "draw_id": bet.draw_id,
            "draw_number": draw.draw_number if draw else None,
            "draw_time": self._iso(draw.draw_time) if draw else None,
            "server_seed_hash": draw.server_seed_hash if draw else None,
            "picks": [int(n) for n in bet.picks],
            "spots": len(bet.picks),
            "stake": float(bet.stake),
            "max_win": float(engine.payout(Decimal(bet.stake), max(row.values()), cap)) if row else None,
            "status": status,
            "matches": sorted(n for n in bet.picks if int(n) in drawn),
            "match_count": int(bet.hits or 0),
            "multiplier": float(bet.multiplier or 0),
            "payout": float(bet.winnings or 0),
            "placed_at": self._iso(bet.placed_at),
        }

    async def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Cycle automatique du Keno partagé (worker, toutes les quelques
        secondes) : tire et règle les tirages arrivés à l'heure, puis garde les
        prochains tirages créés. Chaque tirage est réglé dans sa propre
        transaction (verrou + paris PENDING seulement : jamais deux fois).
        Un tirage échu est TOUJOURS tiré, même après la fermeture."""
        now = now or now_utc()
        results: List[Dict[str, Any]] = []
        due = await self.db.execute(
            select(KenoDraw.id).where(
                KenoDraw.status == KenoDrawStatus.PENDING, KenoDraw.draw_time <= now,
                KenoDraw.is_deleted == False, KenoDraw.mode == MODE_SCHEDULED,  # noqa: E712
            ).order_by(KenoDraw.draw_time)
        )
        for draw_id in due.scalars().all():
            try:
                results.append(await self.execute_draw(draw_id))  # valide (commit) le tirage
            except GameException:
                await self.db.rollback()  # déjà tiré par un autre processus
        created = 0
        config = await self.get_config()
        from app.core.timezone import now_haiti

        if config["mode"] == MODE_SCHEDULED and int(config["open_hour"]) <= now_haiti().hour < int(config["close_hour"]):
            created = await self.schedule_next(now)
            await self.db.commit()
        return {"settled": results, "created": created}

    async def publish_live(self, event: str) -> None:
        """Diffuse l'état en direct à tous les écrans (après commit)."""
        from app.api.websockets.manager import publish

        try:
            await publish({"type": "keno_round", "event": event, "data": await self.live_state()}, draw_id="all")
        except Exception as e:  # la diffusion ne doit jamais annuler l'opération
            self.logger.error(f"Diffusion Keno impossible : {e}")

    # ========== Écran du guichet (tirages partagés) ==========

    SHARED_SCREEN = "kn"

    @classmethod
    def shared_screen_code(cls, agent_id: str) -> str:
        from app.services import counter_screen

        return counter_screen.screen_code(cls.SHARED_SCREEN, agent_id)

    async def shared_screen_state(self, code: str) -> Optional[Dict[str, Any]]:
        """État de l'écran du guichet, avec le résultat à jour de chaque ticket."""
        from app.services import counter_screen

        state = await counter_screen.get_state(self.redis, self.SHARED_SCREEN, code)
        if not state or not state.get("tickets"):
            return state
        config = await self.get_config()
        ids = [t["bet_id"] for t in state["tickets"]]
        bets = {b.id: b for b in (await self.db.execute(select(KenoBet).where(KenoBet.id.in_(ids)))).scalars().all()}
        draws = {d.id: d for d in (await self.db.execute(
            select(KenoDraw).where(KenoDraw.id.in_([b.draw_id for b in bets.values()]))
        )).scalars().all()} if bets else {}
        state["tickets"] = [
            self.serialize_shared_bet(bets[t["bet_id"]], draws.get(bets[t["bet_id"]].draw_id), config)
            if t["bet_id"] in bets else t
            for t in state["tickets"]
        ]
        return state

    async def shared_screen_preview(self, agent_id: str, draw_id: Optional[str], picks: Any, stake: Any) -> Dict[str, Any]:
        """Ticket en cours de saisie sur le tirage partagé. Affichage seulement."""
        from app.services import counter_screen

        config = await self.get_config()
        draw = await self.db.get(KenoDraw, draw_id) if draw_id else None
        cfg = self.draw_config(draw, config) if draw is not None else config
        if not isinstance(picks, (list, tuple)):
            picks = []
        clean = sorted({int(n) for n in picks if isinstance(n, (int, float)) and not isinstance(n, bool) and 1 <= int(n) <= self.TOTAL_NUMBERS})
        clean = clean[: cfg["max_picks"]]
        try:
            amount = Decimal(str(stake)) if stake not in (None, "") else None
            if amount is not None and (not amount.is_finite() or amount <= 0):
                amount = None
        except (InvalidOperation, ValueError, TypeError):
            amount = None
        row = self.paytable_of(cfg).get(len(clean), {})
        cap = Decimal(str(cfg["max_payout"]))
        preview = {
            "draw_id": draw.id if draw else None,
            "draw_number": draw.draw_number if draw else None,
            "picks": clean,
            "spots": len(clean),
            "stake": float(amount) if amount is not None else None,
            "paytable": [
                {"hits": h, "multiplier": float(m), "payout": float(engine.payout(amount, m, cap)) if amount else None}
                for h, m in sorted(row.items(), reverse=True)
            ],
            "max_win": float(engine.payout(amount, max(row.values()), cap)) if amount and row else None,
        }
        previous = await self.shared_screen_state(self.shared_screen_code(agent_id)) or {}
        return await counter_screen.publish(
            self.redis, self.SHARED_SCREEN, agent_id,
            {"event": "preview", "preview": preview, "tickets": previous.get("tickets", [])},
        )

    async def shared_screen_ticket(self, agent_id: str, bet: KenoBet) -> Dict[str, Any]:
        from app.services import counter_screen

        config = await self.get_config()
        draw = await self.db.get(KenoDraw, bet.draw_id)
        previous = await self.shared_screen_state(self.shared_screen_code(agent_id)) or {}
        ticket = self.serialize_shared_bet(bet, draw, config)
        tickets = [t for t in previous.get("tickets", []) if t.get("bet_id") != bet.id] + [ticket]
        return await counter_screen.publish(
            self.redis, self.SHARED_SCREEN, agent_id,
            {"event": "ticket", "preview": None, "tickets": tickets[-8:]},
        )

    # ========== Paris : mode planifié ==========

    async def place_bet(
        self,
        user_id: str,
        bet_data: KenoBetCreate,
        ip_address: str = None,
        agent_id: Optional[str] = None,
    ) -> KenoBet:
        """Pari d'un compte joueur sur un tirage planifié."""
        return await self.create_bet(
            draw_id=bet_data.draw_id,
            picks=bet_data.picks,
            stake=bet_data.stake,
            user_id=user_id,
            agent_id=agent_id,
            ip_address=ip_address,
        )

    async def place_bet_with_ticket(
        self,
        ticket_number: str,
        draw_id: str,
        picks: List[int],
        stake: Decimal,
        agent_id: str,
        ip_address: str = None,
    ) -> KenoBet:
        """Pari financé par un ticket sur un tirage planifié."""
        return await self.create_bet(
            draw_id=draw_id,
            picks=picks,
            stake=stake,
            ticket_number=ticket_number,
            agent_id=agent_id,
            ip_address=ip_address,
        )

    async def create_bet(
        self,
        draw_id: str,
        picks: Any,
        stake: Any,
        user_id: Optional[str] = None,
        ticket_number: Optional[str] = None,
        agent_id: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> KenoBet:
        """Pari sur un tirage PLANIFIÉ — dans la transaction de l'appelant
        (qui fait le commit ; en cas d'erreur, rien n'est débité)."""
        config = await self.get_config()
        if not config["enabled"]:
            raise GameException("Le Keno est momentanément fermé")
        draw = await self._get_draw_for_share(draw_id)
        if draw.mode == MODE_INSTANT:
            raise GameException("Tirage instantané : utilisez le jeu instantané")
        if draw.status != KenoDrawStatus.PENDING:
            raise GameException("Ce tirage n'est plus disponible")
        if now_utc() >= self.betting_closes_at(draw):
            raise GameException("Les paris sont fermés pour ce tirage")
        return await self._new_bet(draw, picks, stake, self.draw_config(draw, config), user_id, ticket_number, agent_id, ip_address)

    async def _new_bet(
        self,
        draw: KenoDraw,
        picks: Any,
        stake: Any,
        config: Dict[str, Any],
        user_id: Optional[str],
        ticket_number: Optional[str],
        agent_id: Optional[str],
        ip_address: Optional[str],
    ) -> KenoBet:
        """Contrôles serveur (numéros, mise, solde), débit et enregistrement."""
        if bool(user_id) == bool(ticket_number):
            raise ValidationException("Un pari est financé par un compte OU par un ticket")
        chosen = self.validate_picks(picks, config["min_picks"], config["max_picks"])
        amount = self.validate_stake(stake, config["min_bet"], config["max_bet"])
        if not self.paytable_of(config).get(len(chosen)):
            raise ValidationException(f"Aucun gain prévu pour {len(chosen)} numéro(s) joué(s)")

        now = now_utc()
        bet_id = str(uuid.uuid4())
        bet = KenoBet(
            id=bet_id,
            draw_id=draw.id,
            agent_id=agent_id,
            picks=chosen,
            stake=amount,
            status=KenoBetStatus.PENDING,
            placed_at=now,
        )

        if user_id:
            await self.wallet_service.debit_for_bet(
                user_id=user_id,
                amount=amount,
                bet_id=bet_id,
                draw_id=draw.id,
                reference=f"BET-KENO-{bet_id}",
            )
            bet.user_id = user_id
            await self._update_user_stats(user_id, amount, is_bet=True)
        else:
            ticket = await self._get_ticket_for_update(ticket_number=ticket_number)
            if ticket.status != TicketStatus.ACTIVE:
                raise GameException("Ticket inactif")
            if ticket.expires_at and ticket.expires_at < now:
                raise GameException("Ticket expiré")
            if ticket.balance < amount:
                raise InsufficientBalanceException(float(amount), float(ticket.balance))
            ticket.balance -= amount
            bet.ticket_id = ticket.id

        self.db.add(bet)
        await self.db.flush()

        await self.audit_service.log(
            action=AuditAction.BET_PLACED,
            user_id=user_id,
            agent_id=agent_id,
            resource_type="keno_bet",
            resource_id=bet.id,
            ip_address=ip_address,
            new_values={
                "draw_id": draw.id,
                "draw_number": draw.draw_number,
                "mode": draw.mode,
                "picks": chosen,
                "stake": float(amount),
                "ticket": ticket_number,
            },
            extra_data={"event": "keno_ticket_created"},
        )
        self.logger.info(f"Keno bet placed: draw=#{draw.draw_number}, picks={len(chosen)}, stake={amount}")
        return bet

    async def _get_ticket_for_update(self, ticket_number: str = None, ticket_id: str = None) -> Ticket:
        query = select(Ticket).with_for_update()
        if ticket_id:
            query = query.where(Ticket.id == ticket_id)
        else:
            query = query.where(Ticket.ticket_number == (ticket_number or "").strip())
        ticket = (await self.db.execute(query)).scalar_one_or_none()
        if ticket is None:
            raise NotFoundException("Ticket", ticket_number or ticket_id)
        return ticket

    # ========== Vérification ==========

    async def verify(self, draw_id: str) -> Dict[str, Any]:
        draw = await self.db.get(KenoDraw, draw_id)
        if draw is None:
            raise NotFoundException("Tirage", draw_id)
        data = {
            "draw_id": draw.id,
            "draw_number": draw.draw_number,
            "mode": draw.mode,
            "status": draw.status.value if hasattr(draw.status, "value") else draw.status,
            "server_seed_hash": draw.server_seed_hash,
            "numbers": [int(n) for n in draw.numbers] if draw.numbers else None,
        }
        if not draw.server_seed:
            return {**data, "verifiable": False, "reason": "Tirage antérieur au système vérifiable"}
        if draw.status == KenoDrawStatus.PENDING:
            return {**data, "verifiable": False, "reason": "Le seed sera révélé après le tirage"}
        check = engine.verify_draw(draw.server_seed, draw.server_seed_hash, draw.draw_number, draw.numbers or [])
        return {**data, "verifiable": True, "server_seed": draw.server_seed, **check}

    # ========== Calculs ==========

    def _calculate_winnings(
        self,
        picks: List[int],
        draw_numbers: List[int],
        stake: Decimal,
        paytable: Optional[Dict[int, Dict[int, Decimal]]] = None,
        max_payout: Optional[Decimal] = None,
    ) -> Tuple[Decimal, int]:
        """Calcule les gains en fonction des numéros tirés"""
        paytable = paytable or self.PAYTABLE
        hits = len(set(picks) & set(draw_numbers))
        mult = engine.multiplier(paytable, len(picks), hits)
        winnings = engine.payout(stake, mult, max_payout if max_payout is not None else engine.HARD_MAX_PAYOUT)
        return winnings, hits

    def _get_multiplier(self, picks_count: int, hits: int) -> Decimal:
        """Récupère le multiplicateur (table par défaut) pour un nombre de picks et hits"""
        return engine.multiplier(self.PAYTABLE, picks_count, hits)

    # ========== Utilitaires ==========
    
    async def get_user_bets(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 50
    ) -> List[KenoBet]:
        """Récupère l'historique des paris d'un utilisateur"""
        result = await self.db.execute(
            select(KenoBet)
            .where(KenoBet.user_id == user_id)
            .order_by(KenoBet.placed_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def get_upcoming_draws(self, limit: int = 5) -> List[KenoDraw]:
        """Récupère les prochains tirages"""
        result = await self.db.execute(
            select(KenoDraw)
            .where(
                and_(
                    KenoDraw.draw_time > now_utc(),
                    KenoDraw.status == KenoDrawStatus.PENDING,
                    KenoDraw.mode == MODE_SCHEDULED,
                )
            )
            .order_by(KenoDraw.draw_time)
            .limit(limit)
        )
        return result.scalars().all()
    
    async def get_last_results(self, limit: int = 10) -> List[KenoDraw]:
        """Récupère les derniers résultats"""
        result = await self.db.execute(
            select(KenoDraw)
            .where(KenoDraw.status == KenoDrawStatus.COMPLETED)
            .order_by(KenoDraw.draw_time.desc())
            .limit(limit)
        )
        return result.scalars().all()
    
    async def _update_user_stats(self, user_id: str, stake: Decimal, is_bet: bool = True) -> None:
        """Met à jour les statistiques utilisateur"""
        user = await self.db.get(User, user_id)
        if user:
            if is_bet:
                user.total_bets_count += 1
                user.total_bets_amount += stake
            else:
                user.total_wins += stake
            await self.db.flush()
    
    async def _credit_ticket(self, ticket_id: str, amount: Decimal) -> None:
        """Crédite un ticket (gains), ligne verrouillée : un pari simultané sur
        le même ticket ne peut pas écraser le nouveau solde."""
        ticket = await self._get_ticket_for_update(ticket_id=ticket_id)
        ticket.balance += amount
        ticket.keep_payable_today()  # payable jusqu'à minuit le jour du résultat
        await self.db.flush()
    
    async def get_statistics(self, user_id: str) -> Dict[str, Any]:
        """Récupère les statistiques Keno d'un utilisateur"""
        result = await self.db.execute(
            select(
                func.count(KenoBet.id).label("total_bets"),
                func.sum(KenoBet.stake).label("total_stake"),
                func.sum(KenoBet.winnings).label("total_winnings"),
                func.count().filter(KenoBet.winnings > 0).label("wins")
            ).where(KenoBet.user_id == user_id)
        )
        stats = result.one()
        
        total_bets = stats.total_bets or 0
        total_wins = stats.wins or 0
        
        return {
            "total_bets": total_bets,
            "total_stake": float(stats.total_stake or 0),
            "total_winnings": float(stats.total_winnings or 0),
            "win_rate": round((total_wins / total_bets * 100) if total_bets > 0 else 0, 2),
            "net_result": float((stats.total_winnings or 0) - (stats.total_stake or 0)),
            "best_win": float(await self._get_best_win(user_id))
        }
    
    async def _get_best_win(self, user_id: str) -> Decimal:
        """Récupère le meilleur gain d'un utilisateur"""
        result = await self.db.execute(
            select(func.max(KenoBet.winnings))
            .where(KenoBet.user_id == user_id)
        )
        return result.scalar() or Decimal("0")