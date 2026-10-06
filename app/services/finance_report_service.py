# app/services/finance_report_service.py
"""Rapport financier de l'administration : TOUT l'argent qui entre et qui sort,
et le revenu du système (par période et par mois).

Argent qui ENTRE (date de l'opération)
- ventes de tickets au guichet ............ tickets.initial_amount (date de vente)
- recharges de tickets .................... ticket_cash_movements « recharge »
- dépôts sur comptes joueurs .............. transactions DEPOSIT terminées
  (espèces au bureau / MonCash, NatCash, ...)
Argent qui SORT (date du paiement, CHAQUE paiement, partiel compris)
- tickets payés au guichet ................ ticket_cash_movements « payout »
- soldes rendus à l'annulation ............ ticket_cash_movements « cancel_refund »
- retraits des comptes joueurs ............ transactions WITHDRAWAL terminées

Revenu du système (rattaché à la date du PARI) :
  mises des paris réglés - gains gagnés + gains jamais réclamés
(un gain sur un ticket expiré sans être réclamé reste à la maison).
Les gains gagnés sont répartis : payés / encore dus / sur comptes joueurs
(dus tant que non retirés) / jamais réclamés. Un paiement partiel est
réparti au prorata du solde du ticket.
Revenu net = revenu - bonus offerts - commissions des agents.

Les montants par mois utilisent les mois locaux (heure d'Haïti).
Requêtes portables (CASE sur les bornes au lieu de date_trunc).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Sequence

from sqlalchemy import and_, case, func, literal_column, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import local_date_start_utc, now_utc, today_haiti
from app.models.bureau import Bureau, CashierSession
from app.models.cash_movement import KIND_RECHARGE, MONEY_OUT_KINDS, TicketCashMovement
from app.models.enums import KenoBetStatus, PaymentMethod, TicketStatus, TransactionStatus, TransactionType
from app.models.game import GameBet
from app.models.keno import KenoBet
from app.models.ticket import Ticket
from app.models.transaction import Transaction
from app.models.user import User
from app.models.wallet import Wallet

MONTHS_FR = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet",
             "Août", "Septembre", "Octobre", "Novembre", "Décembre"]
GAMES = [("keno", "Keno"), ("lucky6", "Lucky6"), ("horse_races", "Horse Races"), ("lucky_wheel", "Lucky Wheel (historique)")]
ZERO = Decimal("0")


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def _f(value) -> float:
    return float(round(_d(value), 2))


def month_start(year: int, month: int) -> datetime:
    """Début du mois local (Haïti), en UTC."""
    if month == 13:
        year, month = year + 1, 1
    return local_date_start_utc(date(year, month, 1))


def _bucket(column, bounds: Sequence[datetime]):
    """Numéro de tranche (0..n-1) d'une date selon les bornes [b0, b1, ..., bn]."""
    return case(*[(column < bounds[i + 1], i) for i in range(len(bounds) - 1)], else_=-1)


def _keys(grouped: bool):
    """GROUP BY sur les alias : PostgreSQL refuse de regrouper une expression
    CASE répétée avec des paramètres différents."""
    return [literal_column("b")] + ([literal_column("g")] if grouped else [])


async def _tolerant(db: AsyncSession, coro_factory):
    """Exécute une source dans un SAVEPOINT : si sa table n'existe pas encore
    (migration pas appliquée), elle est ignorée au lieu de casser la page."""
    try:
        async with db.begin_nested():
            await coro_factory()
    except DBAPIError as exc:  # table absente (Postgres : ProgrammingError, SQLite : OperationalError)
        import logging

        logging.getLogger(__name__).warning(f"Finances : source ignorée ({exc.orig.__class__.__name__})")


class FinanceReportService:
    def __init__(self, db: AsyncSession, redis_client=None):
        self.db = db
        self.redis = redis_client  # taux de commission par défaut

    # ------------------------------------------------------------------ sources
    async def _sum(self, date_col, amount, bounds, *where, group=None, join=None) -> Dict[Any, Dict[str, Decimal]]:
        """{(tranche[, groupe]): {"amount", "count"}} pour une source."""
        b = _bucket(date_col, bounds).label("b")
        cols = [b] + ([group.label("g")] if group is not None else [])
        stmt = select(*cols, func.coalesce(func.sum(amount), 0).label("amount"), func.count().label("count"))
        if join is not None:
            stmt = stmt.select_from(join)
        stmt = (stmt.where(date_col >= bounds[0], date_col < bounds[-1], *where)
                .group_by(*_keys(group is not None)))
        out: Dict[Any, Dict[str, Decimal]] = {}
        for row in (await self.db.execute(stmt)).all():
            key = (row.b, row.g) if group is not None else row.b
            out[key] = {"amount": _d(row.amount), "count": int(row.count or 0)}
        return out

    async def _bets(self, bounds) -> Dict[str, Dict[int, Dict[str, Decimal]]]:
        """Mises / gains des paris réglés, par jeu et par tranche."""
        games: Dict[str, Dict[int, Dict[str, Decimal]]] = {g: {} for g, _ in GAMES}

        async def collect(name, date_col, stake, win, *where, group=None):
            b = _bucket(date_col, bounds).label("b")
            cols = [b] + ([group.label("g")] if group is not None else [])
            stmt = (select(*cols, func.coalesce(func.sum(stake), 0).label("stakes"),
                           func.coalesce(func.sum(win), 0).label("wins"), func.count().label("count"))
                    .where(date_col >= bounds[0], date_col < bounds[-1], *where).group_by(*_keys(group is not None)))
            for row in (await self.db.execute(stmt)).all():
                game = row.g if group is not None else name
                if game not in games:
                    games[game] = {}
                games[game][row.b] = {"stakes": _d(row.stakes), "wins": _d(row.wins), "count": int(row.count or 0)}

        from app.models.lucky import LuckyPlay  # Lucky Wheel retirée : parties passées gardées dans les comptes

        await _tolerant(self.db, lambda: collect("keno", KenoBet.placed_at, KenoBet.stake, KenoBet.winnings,
                                                 KenoBet.status.in_([KenoBetStatus.WON, KenoBetStatus.LOST])))
        await _tolerant(self.db, lambda: collect(None, GameBet.placed_at, GameBet.stake, GameBet.winnings,
                                                 GameBet.status.in_(["WON", "LOST"]), group=GameBet.game_type))
        await _tolerant(self.db, lambda: collect("lucky_wheel", LuckyPlay.played_at, LuckyPlay.stake, LuckyPlay.winnings,
                                                 LuckyPlay.status == "COMPLETED"))
        return games

    async def _wins_split(self, bounds) -> Dict[str, Dict[int, Dict[str, Decimal]]]:
        """{jeu: {tranche: {paid, due, accounts, unclaimed}}} pour les gains des paris gagnés :
        - accounts  : crédités sur un compte joueur (dus tant qu'il ne les a pas retirés) ;
        - paid      : déjà remis au guichet (ticket payé ; paiement partiel au prorata
                      du solde du ticket : payé / (payé + solde restant)) ;
        - due       : reste sur un ticket encore valable ;
        - unclaimed : reste sur un ticket expiré ou annulé : jamais réclamé, garde la maison."""
        now = now_utc()
        out: Dict[str, Dict[int, Dict[str, Decimal]]] = {}
        paid_amt = func.coalesce(Ticket.paid_amount, 0)
        left_amt = func.coalesce(Ticket.balance, 0)

        async def collect(name, date_col, user_col, ticket_col, win_col, *where, group=None):
            on_account = user_col.isnot(None)
            paid_part = case(
                (on_account, 0),
                (Ticket.status == TicketStatus.PAID, win_col),
                ((paid_amt + left_amt) > 0, win_col * paid_amt / (paid_amt + left_amt)),
                else_=0,
            )
            valid = and_(Ticket.status == TicketStatus.ACTIVE, Ticket.expires_at >= now)
            b = _bucket(date_col, bounds).label("b")
            cols = [b] + ([group.label("g")] if group is not None else [])
            stmt = (select(*cols,
                           func.coalesce(func.sum(case((on_account, win_col), else_=0)), 0).label("acc"),
                           func.coalesce(func.sum(paid_part), 0).label("paid"),
                           func.coalesce(func.sum(case((on_account, 0), (valid, win_col - paid_part), else_=0)), 0).label("due"),
                           func.coalesce(func.sum(case((on_account, 0), (valid, 0), else_=win_col - paid_part)), 0).label("lost"))
                    .select_from(date_col.class_)
                    .outerjoin(Ticket, Ticket.id == ticket_col)
                    .where(date_col >= bounds[0], date_col < bounds[-1], win_col > 0, *where)
                    .group_by(*_keys(group is not None)))
            for row in (await self.db.execute(stmt)).all():
                game = row.g if group is not None else name
                slot = out.setdefault(game, {}).setdefault(row.b, {"paid": ZERO, "due": ZERO, "accounts": ZERO, "unclaimed": ZERO})
                slot["accounts"] += _d(row.acc)
                slot["paid"] += _d(row.paid)
                slot["due"] += _d(row.due)
                slot["unclaimed"] += _d(row.lost)

        from app.models.lucky import LuckyPlay

        await _tolerant(self.db, lambda: collect("keno", KenoBet.placed_at, KenoBet.user_id, KenoBet.ticket_id, KenoBet.winnings,
                                                 KenoBet.status == KenoBetStatus.WON))
        await _tolerant(self.db, lambda: collect(None, GameBet.placed_at, GameBet.user_id, GameBet.ticket_id, GameBet.winnings,
                                                 GameBet.status == "WON", group=GameBet.game_type))
        await _tolerant(self.db, lambda: collect("lucky_wheel", LuckyPlay.played_at, LuckyPlay.user_id, LuckyPlay.ticket_id,
                                                 LuckyPlay.winnings, LuckyPlay.status == "COMPLETED"))
        return out

    @staticmethod
    def _split_total(split, i) -> Dict[str, Decimal]:
        tot = {"paid": ZERO, "due": ZERO, "accounts": ZERO, "unclaimed": ZERO}
        for slots in split.values():
            for k, v in slots.get(i, {}).items():
                tot[k] += v
        return tot

    async def game_totals(self, bounds: Sequence[datetime]) -> List[Dict[str, Any]]:
        """Mises et gains gagnés des paris RÉGLÉS, tous jeux, par tranche.
        Définition unique utilisée par le tableau de bord, les statistiques
        et les rapports (paris en attente et remboursés exclus)."""
        bets = await self._bets(bounds)
        split = await self._wins_split(bounds)
        out = []
        for i in range(len(bounds) - 1):
            sp = self._split_total(split, i)
            row = {"stakes": ZERO, "wins": ZERO, "bets": 0, "games": {},
                   "wins_paid": _f(sp["paid"]), "wins_due": _f(sp["due"] + sp["accounts"]),
                   "wins_unclaimed": _f(sp["unclaimed"])}
            for game, slots in bets.items():
                v = slots.get(i)
                if v:
                    row["stakes"] += v["stakes"]
                    row["wins"] += v["wins"]
                    row["bets"] += v["count"]
                    row["games"][game] = {"stakes": _f(v["stakes"]), "wins": _f(v["wins"]), "bets": v["count"]}
            row["stakes"], row["wins"] = _f(row["stakes"]), _f(row["wins"])
            out.append(row)
        return out

    async def _pending_stakes(self) -> Decimal:
        k = (await self.db.execute(select(func.coalesce(func.sum(KenoBet.stake), 0))
                                   .where(KenoBet.status == KenoBetStatus.PENDING))).scalar()
        g = (await self.db.execute(select(func.coalesce(func.sum(GameBet.stake), 0))
                                   .where(GameBet.status == "PENDING"))).scalar()
        return _d(k) + _d(g)

    # ------------------------------------------------------------------ calcul
    async def _compute(self, bounds: Sequence[datetime]) -> List[Dict[str, Any]]:
        """Une ligne complète (entrées, sorties, jeux, revenu) par tranche."""
        done = Transaction.status == TransactionStatus.COMPLETED
        sales = await self._sum(Ticket.created_at, Ticket.initial_amount, bounds)
        recharges = await self._sum(TicketCashMovement.created_at, TicketCashMovement.amount, bounds,
                                    TicketCashMovement.kind == KIND_RECHARGE)
        payouts = await self._sum(TicketCashMovement.created_at, TicketCashMovement.amount, bounds,
                                  TicketCashMovement.kind.in_(MONEY_OUT_KINDS), group=TicketCashMovement.kind)
        deposits = await self._sum(Transaction.created_at, Transaction.amount, bounds, done,
                                   Transaction.transaction_type == TransactionType.DEPOSIT,
                                   group=Transaction.payment_method)
        withdrawals = await self._sum(Transaction.created_at, Transaction.amount, bounds, done,
                                      Transaction.transaction_type == TransactionType.WITHDRAWAL)
        bonuses = await self._sum(Transaction.created_at, Transaction.amount, bounds, done,
                                  Transaction.transaction_type == TransactionType.BONUS)
        gaps = await self._sum(CashierSession.closed_at, CashierSession.difference, bounds,
                               CashierSession.status == "CLOSED", CashierSession.difference != 0)
        bets = await self._bets(bounds)
        wins_split = await self._wins_split(bounds)
        from app.services.commission_service import CommissionService

        commissions = await CommissionService(self.db, self.redis).totals(bounds)

        rows = []
        for i in range(len(bounds) - 1):
            dep_cash = sum((v["amount"] for (b, m), v in deposits.items() if b == i and m == PaymentMethod.CASH), ZERO)
            dep_other = sum((v["amount"] for (b, m), v in deposits.items() if b == i and m != PaymentMethod.CASH), ZERO)
            sale = sales.get(i, {}).get("amount", ZERO)
            recharge = recharges.get(i, {}).get("amount", ZERO)
            paid = sum((v["amount"] for (b, k), v in payouts.items() if b == i and k != "cancel_refund"), ZERO)
            paid_count = sum((v["count"] for (b, k), v in payouts.items() if b == i and k != "cancel_refund"), 0)
            refunds = sum((v["amount"] for (b, k), v in payouts.items() if b == i and k == "cancel_refund"), ZERO)
            wd = withdrawals.get(i, {}).get("amount", ZERO)
            money_in = sale + recharge + dep_cash + dep_other
            money_out = paid + refunds + wd
            games = []
            stakes = wins = unclaimed = ZERO
            for key, label in GAMES + [(g, g) for g in bets if g not in dict(GAMES)]:
                v = bets.get(key, {}).get(i)
                if not v:
                    continue
                lost = wins_split.get(key, {}).get(i, {}).get("unclaimed", ZERO)
                stakes += v["stakes"]
                wins += v["wins"]
                unclaimed += lost
                ggr = v["stakes"] - v["wins"] + lost
                games.append({"key": key, "label": label, "bets": v["count"], "stakes": _f(v["stakes"]),
                              "wins": _f(v["wins"]), "unclaimed": _f(lost), "revenue": _f(ggr),
                              "margin": round(float(ggr / v["stakes"] * 100), 1) if v["stakes"] else 0.0})
            ggr = stakes - wins + unclaimed
            split = self._split_total(wins_split, i)
            bonus = bonuses.get(i, {}).get("amount", ZERO)
            commission = commissions[i]
            rows.append({
                "in": {"ticket_sales": _f(sale), "ticket_sales_count": sales.get(i, {}).get("count", 0),
                       "recharges": _f(recharge),
                       "deposits_cash": _f(dep_cash), "deposits_mobile": _f(dep_other), "total": _f(money_in)},
                "out": {"ticket_payouts": _f(paid), "ticket_payouts_count": paid_count, "cancel_refunds": _f(refunds),
                        "withdrawals": _f(wd), "total": _f(money_out)},
                "cash_flow": _f(money_in - money_out),
                "stakes": _f(stakes), "wins": _f(wins),
                "wins_split": {k: _f(v) for k, v in split.items()},
                "wins_paid": _f(split["paid"]),                       # remis aux joueurs au guichet
                "wins_due": _f(split["due"] + split["accounts"]),     # encore dus (tickets valables + comptes)
                "wins_unclaimed": _f(split["unclaimed"]),             # jamais réclamés : restent à la maison
                "revenue": _f(ggr), "bonus": _f(bonus),
                "commissions": _f(commission), "net_revenue": _f(ggr - bonus - commission),
                "margin": round(float(ggr / stakes * 100), 1) if stakes else 0.0,
                "cash_gaps": _f(gaps.get(i, {}).get("amount", ZERO)), "cash_gaps_count": gaps.get(i, {}).get("count", 0),
                "games": games,
            })
        return rows

    # ------------------------------------------------------------------ API
    async def period(self, start: datetime, end: datetime) -> Dict[str, Any]:
        """Totaux de la période [start, end[ (bornes UTC)."""
        return (await self._compute([start, end]))[0]

    async def monthly(self, year: int) -> List[Dict[str, Any]]:
        """Une ligne par mois de l'année (jusqu'au mois en cours)."""
        today = today_haiti()
        last = 12 if year < today.year else (today.month if year == today.year else 0)
        if last == 0:
            return []
        bounds = [month_start(year, m) for m in range(1, last + 2)]
        rows = await self._compute(bounds)
        for m, row in enumerate(rows, start=1):
            row["month"] = m
            row["label"] = f"{MONTHS_FR[m - 1]} {year}"
            row["current"] = year == today.year and m == today.month
        return rows

    async def by_bureau(self, start: datetime, end: datetime) -> List[Dict[str, Any]]:
        """Entrées / sorties au guichet par bureau (tickets + espèces sur comptes)."""
        bounds = [start, end]
        done = Transaction.status == TransactionStatus.COMPLETED
        cash = Transaction.payment_method == PaymentMethod.CASH
        sales = await self._sum(Ticket.created_at, Ticket.initial_amount, bounds, group=Ticket.bureau_id)
        recharges = await self._sum(TicketCashMovement.created_at, TicketCashMovement.amount, bounds,
                                    TicketCashMovement.kind == KIND_RECHARGE, group=TicketCashMovement.bureau_id)
        payouts = await self._sum(TicketCashMovement.created_at, TicketCashMovement.amount, bounds,
                                  TicketCashMovement.kind.in_(MONEY_OUT_KINDS), group=TicketCashMovement.bureau_id)
        # espèces déposées / retirées sur un compte joueur au guichet : bureau de l'agent
        tx_agent = Transaction.__table__.join(User.__table__, User.id == Transaction.created_by)
        deps = await self._sum(Transaction.created_at, Transaction.amount, bounds, done, cash,
                               Transaction.transaction_type == TransactionType.DEPOSIT, group=User.bureau_id, join=tx_agent)
        wds = await self._sum(Transaction.created_at, Transaction.amount, bounds, done, cash,
                              Transaction.transaction_type == TransactionType.WITHDRAWAL, group=User.bureau_id, join=tx_agent)
        gaps = await self._sum(CashierSession.closed_at, CashierSession.difference, bounds,
                               CashierSession.status == "CLOSED", group=CashierSession.bureau_id)
        ids = {k[1] for src in (sales, recharges, payouts, deps, wds, gaps) for k in src}
        ids.discard(None)
        names = {}
        if ids:
            names = dict((await self.db.execute(select(Bureau.id, Bureau.name).where(Bureau.id.in_(ids)))).all())
        rows = []
        for bid in ids:
            get = lambda src: src.get((0, bid), {}).get("amount", ZERO)  # noqa: E731
            money_in = get(sales) + get(recharges) + get(deps)
            money_out = get(payouts) + get(wds)
            rows.append({"bureau": names.get(bid, "?"), "ticket_sales": _f(get(sales) + get(recharges)), "deposits": _f(get(deps)),
                         "ticket_payouts": _f(get(payouts)), "withdrawals": _f(get(wds)),
                         "in": _f(money_in), "out": _f(money_out), "cash_flow": _f(money_in - money_out),
                         "cash_gaps": _f(get(gaps))})
        rows.sort(key=lambda r: -r["in"])
        return rows

    async def snapshot(self) -> Dict[str, Any]:
        """Situation en ce moment : argent dû aux joueurs et argent en caisse."""
        tickets_due = (await self.db.execute(
            select(func.coalesce(func.sum(Ticket.balance), 0), func.count())
            .where(Ticket.status == TicketStatus.ACTIVE, Ticket.balance > 0, Ticket.expires_at >= now_utc())
        )).one()
        wallets = (await self.db.execute(select(func.coalesce(func.sum(Wallet.balance), 0)))).scalar()
        sessions = (await self.db.execute(
            select(func.coalesce(func.sum(CashierSession.current_balance), 0), func.count())
            .where(CashierSession.status == "OPEN")
        )).one()
        return {
            "tickets_due": _f(tickets_due[0]), "tickets_due_count": int(tickets_due[1] or 0),
            "wallets_due": _f(wallets), "players_due": _f(_d(tickets_due[0]) + _d(wallets)),
            "pending_stakes": _f(await self._pending_stakes()),
            "cash_in_drawers": _f(sessions[0]), "open_sessions": int(sessions[1] or 0),
        }

    @staticmethod
    def totals(rows: List[Dict[str, Any]]) -> Dict[str, float]:
        keys = ["stakes", "wins", "wins_paid", "wins_due", "wins_unclaimed", "revenue", "bonus", "commissions", "net_revenue", "cash_flow", "cash_gaps"]
        out = {k: _f(sum(_d(r[k]) for r in rows)) for k in keys}
        out["in"] = _f(sum(_d(r["in"]["total"]) for r in rows))
        out["out"] = _f(sum(_d(r["out"]["total"]) for r in rows))
        out["margin"] = round(out["revenue"] / out["stakes"] * 100, 1) if out["stakes"] else 0.0
        return out
