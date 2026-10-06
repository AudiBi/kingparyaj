# app/services/commission_service.py
"""Commission des agents : un pourcentage de leurs ventes.

Ventes de l'agent = mises des paris qu'il a encaissés avec de l'argent
NOUVEAU : espèces (ticket vendu au comptant) ou compte joueur. Un gain
rejoué sur le même ticket n'est pas une vente (pas d'argent encaissé) :
counts_as_sale = faux, pas de commission. Paris remboursés / annulés exclus.

La commission est FIGÉE sur chaque pari au moment de la vente (colonne
commission, au taux du jour) : changer le taux ne réécrit pas le passé.
Les paris antérieurs à cette règle (commission NULL) sont calculés au taux
actuel de l'agent.

Taux : taux personnel de l'agent (users.commission_rate), sinon le taux par
défaut fixé par l'admin, stocké en base (system_settings) ; repli sur
AGENT_COMMISSION_RATE du .env s'il n'a jamais été fixé.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence

import redis.asyncio as redis
from sqlalchemy import case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import ValidationException
from app.models.enums import KenoBetStatus, UserRole
from app.models.game import GameBet
from app.models.keno import KenoBet
from app.models.setting import SystemSetting
from app.models.user import User

DEFAULT_RATE_KEY = "agent_commission_rate"            # clé system_settings
LEGACY_REDIS_KEY = "settings:agent_commission_rate"   # ancien stockage (Redis seul)
MAX_RATE = Decimal("50")
CENT = Decimal("0.01")
NEW_MONEY = ("cash", "account")  # sources de financement qui sont des ventes


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def money(value) -> Decimal:
    return _d(value).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_rate(value) -> Decimal:
    """Taux en % (0 à 50), deux décimales."""
    try:
        rate = Decimal(str(value).replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        raise ValidationException("Taux de commission invalide")
    if not rate.is_finite() or rate < 0 or rate > MAX_RATE:
        raise ValidationException(f"Le taux de commission doit être entre 0 et {MAX_RATE} %")
    return rate.quantize(CENT, rounding=ROUND_HALF_UP)


async def get_default_rate(db: AsyncSession, redis_client: Optional[redis.Redis] = None) -> Decimal:
    """Taux par défaut : base de données > ancien réglage Redis > .env."""
    row = await db.get(SystemSetting, DEFAULT_RATE_KEY)
    if row is not None:
        return parse_rate(row.value)
    if redis_client is not None:  # réglage fait avant le passage en base : on le garde
        try:
            raw = await redis_client.get(LEGACY_REDIS_KEY)
            if raw is not None:
                return parse_rate(raw.decode() if isinstance(raw, bytes) else raw)
        except Exception:
            pass
    return parse_rate(settings.AGENT_COMMISSION_RATE or 0)


async def set_default_rate(db: AsyncSession, value, by: Optional[str] = None) -> Decimal:
    rate = parse_rate(value)
    row = await db.get(SystemSetting, DEFAULT_RATE_KEY)
    if row is None:
        db.add(SystemSetting(key=DEFAULT_RATE_KEY, value=str(rate), updated_by=by, updated_at=datetime.utcnow()))
    else:
        row.value, row.updated_by, row.updated_at = str(rate), by, datetime.utcnow()
    await db.flush()
    return rate


def effective_rate(agent: User, default_rate: Decimal) -> Decimal:
    return _d(agent.commission_rate) if agent.commission_rate is not None else default_rate


async def freeze_commission(db: AsyncSession, redis_client, agent: User, bet, funded_by: Optional[str]) -> None:
    """À appeler juste après l'enregistrement d'un pari au guichet : fige la
    commission de l'agent sur ce pari (taux du jour)."""
    is_sale = (funded_by or "cash") in NEW_MONEY
    bet.counts_as_sale = is_sale
    if not is_sale:
        bet.commission = Decimal("0")
        return
    rate = effective_rate(agent, await get_default_rate(db, redis_client))
    bet.commission = money(_d(bet.stake) * rate / 100)


def _bucket(column, bounds: Sequence):
    return case(*[(column < bounds[i + 1], i) for i in range(len(bounds) - 1)], else_=-1)


class CommissionService:
    def __init__(self, db: AsyncSession, redis_client: Optional[redis.Redis] = None):
        self.db = db
        self.redis = redis_client

    async def sales(self, bounds: Sequence, agent_ids: Optional[Iterable[str]] = None) -> Dict[tuple, Dict[str, Decimal]]:
        """{(tranche, agent_id): {"sales", "frozen", "legacy"}} :
        sales = mises qui comptent comme ventes ; frozen = commissions figées ;
        legacy = mises des anciens paris sans commission figée."""
        ids = list(agent_ids) if agent_ids is not None else None
        out: Dict[tuple, Dict[str, Decimal]] = {}

        async def add(date_col, agent_col, stake_col, *where, commission_col=None, sale_col=None):
            b = _bucket(date_col, bounds).label("b")
            sale = sale_col if sale_col is not None else True
            cols = [b, agent_col.label("a"), func.coalesce(func.sum(stake_col), 0).label("s")]
            if commission_col is not None:
                cols += [func.coalesce(func.sum(commission_col), 0).label("f"),
                         func.coalesce(func.sum(case((commission_col.is_(None), stake_col), else_=0)), 0).label("l")]
            stmt = (select(*cols)
                    .where(date_col >= bounds[0], date_col < bounds[-1], agent_col.isnot(None), *where)
                    .group_by(literal_column("b"), literal_column("a")))
            if sale_col is not None:
                stmt = stmt.where(sale)
            if ids is not None:
                stmt = stmt.where(agent_col.in_(ids))
            for row in (await self.db.execute(stmt)).all():
                slot = out.setdefault((row.b, row.a), {"sales": Decimal("0"), "frozen": Decimal("0"), "legacy": Decimal("0")})
                slot["sales"] += _d(row.s)
                if commission_col is not None:
                    slot["frozen"] += _d(row.f)
                    slot["legacy"] += _d(row.l)
                else:
                    slot["legacy"] += _d(row.s)

        await add(KenoBet.placed_at, KenoBet.agent_id, KenoBet.stake, KenoBet.status != KenoBetStatus.REFUNDED,
                  commission_col=KenoBet.commission, sale_col=KenoBet.counts_as_sale)
        await add(GameBet.placed_at, GameBet.agent_id, GameBet.stake, GameBet.status.notin_(["VOID", "REFUNDED"]),
                  commission_col=GameBet.commission, sale_col=GameBet.counts_as_sale)
        from app.models.lucky import LuckyPlay  # historique Lucky Wheel (jeu retiré) : taux actuel

        await add(LuckyPlay.played_at, LuckyPlay.agent_id, LuckyPlay.stake, LuckyPlay.status == "COMPLETED")
        return out

    @staticmethod
    def _commission(slot: Dict[str, Decimal], rate: Decimal) -> Decimal:
        return money(slot["frozen"] + slot["legacy"] * rate / 100)

    async def for_agent(self, agent: User, start, end) -> Dict[str, Any]:
        """Commission d'un agent sur [start, end[."""
        default = await get_default_rate(self.db, self.redis)
        rate = effective_rate(agent, default)
        slot = (await self.sales([start, end], [agent.id])).get(
            (0, agent.id), {"sales": Decimal("0"), "frozen": Decimal("0"), "legacy": Decimal("0")})
        return {"rate": float(rate), "custom": agent.commission_rate is not None, "default_rate": float(default),
                "sales": float(money(slot["sales"])), "commission": float(self._commission(slot, rate))}

    async def _agents(self, ids) -> Dict[str, User]:
        ids = [i for i in set(ids) if i]
        if not ids:
            return {}
        rows = (await self.db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
        return {u.id: u for u in rows}

    async def totals(self, bounds: Sequence) -> List[Decimal]:
        """Commissions de tous les agents, par tranche (pour le rapport financier)."""
        default = await get_default_rate(self.db, self.redis)
        sales = await self.sales(bounds)
        agents = await self._agents(a for _, a in sales)
        out = [Decimal("0")] * (len(bounds) - 1)
        for (b, aid), slot in sales.items():
            if 0 <= b < len(out):
                agent = agents.get(aid)
                out[b] += self._commission(slot, effective_rate(agent, default) if agent else default)
        return [money(v) for v in out]

    async def by_agent(self, start, end) -> List[Dict[str, Any]]:
        """Une ligne par agent (ventes, taux, commission) sur la période ;
        les agents actifs sans vente apparaissent aussi."""
        default = await get_default_rate(self.db, self.redis)
        sales = await self.sales([start, end])
        listed = (await self.db.execute(
            select(User).where(User.role.in_([UserRole.AGENT, UserRole.MANAGER]), User.is_deleted == False)  # noqa: E712
        )).scalars().all()
        agents = {u.id: u for u in listed}
        agents.update(await self._agents(a for _, a in sales if a not in agents))
        empty = {"sales": Decimal("0"), "frozen": Decimal("0"), "legacy": Decimal("0")}
        rows = []
        for aid, agent in agents.items():
            slot = sales.get((0, aid), empty)
            if not slot["sales"] and not agent.is_active:
                continue
            rate = effective_rate(agent, default)
            rows.append({"agent_id": aid, "agent": agent.full_name or agent.phone, "phone": agent.phone,
                         "rate": float(rate), "custom": agent.commission_rate is not None,
                         "sales": float(money(slot["sales"])), "commission": float(self._commission(slot, rate))})
        rows.sort(key=lambda r: (-r["commission"], r["agent"]))
        return rows
