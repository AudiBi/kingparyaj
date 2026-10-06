# tests/test_services/test_finance_report.py
"""Rapport financier admin : toutes les entrées / sorties d'argent (guichets
ET comptes joueurs) et le revenu du système (mises - gains), par mois."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.timezone import now_utc, today_haiti
from app.models.bureau import CashierSession
from app.models.enums import KenoBetStatus, PaymentMethod, TicketStatus, TransactionStatus, TransactionType
from app.models.game import GameBet
from app.models.keno import KenoBet
from app.models.ticket import Ticket
from app.models.transaction import Transaction
from app.models.wallet import Wallet
from app.services.finance_report_service import FinanceReportService
from app.services.keno_service import KenoService
from app.services.lucky6_service import Lucky6Service
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


async def _tx(db, user, kind, amount, method=None, status=TransactionStatus.COMPLETED, agent=None):
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one()
    tx = Transaction(user_id=user.id, wallet_id=wallet.id, reference=Transaction.generate_reference("TX"),
                     transaction_type=kind, payment_method=method, amount=Decimal(amount),
                     balance_before=Decimal("0"), balance_after=Decimal("0"), status=status,
                     created_by=agent.id if agent else None)
    db.add(tx)
    await db.flush()
    return tx


async def _scenario(db, fake_redis, make_agent, make_ticket, make_user):
    """Un mois typique :
    - guichet : 2 tickets vendus (100 + 50), un ticket payé 300 ;
    - comptes : dépôt espèces 200 (au guichet), dépôt MonCash 500, retrait 80,
      dépôt en attente (ignoré), bonus 10 ;
    - paris : Lucky6 gagné (mise 50, gain 300), Lucky6 perdu (mise 100),
      Keno perdu (mise 40), Keno en attente (ignoré du revenu)."""
    agent = await make_agent()
    player = await make_user()
    t1 = await make_ticket(agent, balance=Decimal("100"))
    await make_ticket(agent, balance=Decimal("50"))
    paid = (await db.execute(select(Ticket).where(Ticket.ticket_number == t1["ticket_number"]))).scalar_one()
    paid.balance = Decimal("300")              # gain crédité sur le ticket…
    paid.record_payout(Decimal("300"), agent.id)  # …puis payé au guichet (paiement daté)

    await _tx(db, player, TransactionType.DEPOSIT, "200", PaymentMethod.CASH, agent=agent)
    await _tx(db, player, TransactionType.DEPOSIT, "500", PaymentMethod.MONCASH)
    await _tx(db, player, TransactionType.DEPOSIT, "999", PaymentMethod.MONCASH, status=TransactionStatus.PENDING)
    await _tx(db, player, TransactionType.WITHDRAWAL, "80", PaymentMethod.CASH, agent=agent)
    await _tx(db, player, TransactionType.BONUS, "10")

    race = await Lucky6Service(db, fake_redis).create_race()
    for status, stake, win in (("WON", "50", "300"), ("LOST", "100", "0")):
        db.add(GameBet(round_id=race.id, game_type="lucky6", user_id=player.id, agent_id=agent.id, bet_type="SIX", selection=[1, 2, 3, 4, 5, 6],
                       stake=Decimal(stake), odds=Decimal("6"), potential_payout=Decimal("300"), status=status,
                       winnings=Decimal(win), placed_at=now_utc()))
    keno = KenoService(db, fake_redis)
    draw = await keno.generate_draw(draw_time=now_utc() + timedelta(minutes=5), config=await keno.get_config())
    for status, stake in ((KenoBetStatus.LOST, "40"), (KenoBetStatus.PENDING, "25")):
        db.add(KenoBet(draw_id=draw.id, user_id=player.id, agent_id=agent.id, picks=[1, 2], stake=Decimal(stake), status=status,
                       winnings=Decimal("0"), placed_at=now_utc()))
    db.add(CashierSession(bureau_id=agent.bureau_id, agent_id=agent.id, starting_balance=Decimal("0"), current_balance=Decimal("95"),
                          expected_balance=Decimal("100"), difference=Decimal("-5"), status="CLOSED", opened_at=now_utc(), closed_at=now_utc()))
    await db.flush()
    return agent


def _bounds():
    return now_utc() - timedelta(days=1), now_utc() + timedelta(days=1)


@pytest.mark.asyncio
async def test_period_counts_counter_and_account_money(db_session, fake_redis, make_agent, make_ticket, make_user):
    await _scenario(db_session, fake_redis, make_agent, make_ticket, make_user)
    p = await FinanceReportService(db_session).period(*_bounds())
    assert p["in"] == {"ticket_sales": 150.0, "ticket_sales_count": 2, "recharges": 0.0, "deposits_cash": 200.0,
                       "deposits_mobile": 500.0, "total": 850.0}
    assert p["out"] == {"ticket_payouts": 300.0, "ticket_payouts_count": 1, "cancel_refunds": 0.0, "withdrawals": 80.0, "total": 380.0}
    assert p["cash_flow"] == 470.0
    # revenu : mises réglées 50 + 100 + 40 = 190, gains 300 -> -110 ; bonus 10
    assert (p["stakes"], p["wins"], p["revenue"], p["bonus"], p["net_revenue"]) == (190.0, 300.0, -110.0, 10.0, -120.0)
    games = {g["key"]: g for g in p["games"]}
    assert games["lucky6"]["revenue"] == -150.0 and games["lucky6"]["bets"] == 2
    assert games["keno"]["stakes"] == 40.0 and games["keno"]["bets"] == 1
    assert p["cash_gaps"] == -5.0 and p["cash_gaps_count"] == 1


@pytest.mark.asyncio
async def test_outside_period_is_ignored(db_session, fake_redis, make_agent, make_ticket, make_user):
    await _scenario(db_session, fake_redis, make_agent, make_ticket, make_user)
    p = await FinanceReportService(db_session).period(now_utc() - timedelta(days=60), now_utc() - timedelta(days=30))
    assert p["in"]["total"] == 0 and p["out"]["total"] == 0 and p["revenue"] == 0 and p["games"] == []


@pytest.mark.asyncio
async def test_monthly_revenue_current_month(db_session, fake_redis, make_agent, make_ticket, make_user):
    await _scenario(db_session, fake_redis, make_agent, make_ticket, make_user)
    service = FinanceReportService(db_session)
    months = await service.monthly(today_haiti().year)
    assert len(months) == today_haiti().month
    current = months[-1]
    assert current["current"] and current["in"]["total"] == 850.0 and current["revenue"] == -110.0
    assert all(m["in"]["total"] == 0 for m in months[:-1])
    totals = service.totals(months)
    assert totals["in"] == 850.0 and totals["out"] == 380.0 and totals["net_revenue"] == -120.0
    assert await service.monthly(today_haiti().year + 1) == []


@pytest.mark.asyncio
async def test_by_bureau_and_snapshot(db_session, fake_redis, make_agent, make_ticket, make_user):
    agent = await _scenario(db_session, fake_redis, make_agent, make_ticket, make_user)
    service = FinanceReportService(db_session)
    rows = await service.by_bureau(*_bounds())
    assert len(rows) == 1
    row = rows[0]
    assert (row["ticket_sales"], row["deposits"], row["ticket_payouts"], row["withdrawals"]) == (150.0, 200.0, 300.0, 80.0)
    assert row["cash_flow"] == -30.0 and row["cash_gaps"] == -5.0
    db_session.add(CashierSession(bureau_id=agent.bureau_id, agent_id=agent.id, starting_balance=Decimal("0"),
                                  current_balance=Decimal("700"), expected_balance=Decimal("0"), status="OPEN", opened_at=now_utc()))
    await db_session.flush()
    snap = await service.snapshot()
    assert snap["tickets_due"] == 50.0 and snap["tickets_due_count"] == 1
    assert snap["pending_stakes"] == 25.0 and snap["cash_in_drawers"] == 700.0 and snap["open_sessions"] == 1


async def _admin_login(client):
    page = await client.get("/admin/login")
    token = re.search(r'name="csrf_token"\s+value="([^"]*)"', page.text).group(1)
    r = await client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": token},
                          follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.asyncio
async def test_admin_finance_page_and_export(admin_client, admin_user, db_session, fake_redis, make_agent, make_ticket, make_user):
    await _scenario(db_session, fake_redis, make_agent, make_ticket, make_user)
    await _admin_login(admin_client)
    r = await admin_client.get("/admin/reports/financial")
    assert r.status_code == 200, r.text[:2000]
    html = r.text
    assert "Argent qui entre" in html and "850,00 HTG" in html and "380,00 HTG" in html
    assert "Gains gagnés sur la période : où en sont-ils ?" in html and 'id="winsAccounts">300,00 HTG' in html
    assert "Revenu du système par mois" in html and "Lucky6" in html and "en cours" in html

    r = await admin_client.get("/admin/api/reports/financial/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "Ventes de tickets au guichet;150.0" in r.text and "Paiements des tickets au guichet (partiels compris);300.0" in r.text
    assert f"REVENU PAR MOIS {today_haiti().year}" in r.text

    assert (await admin_client.get("/admin/reports/financial?start_date=bad")).status_code == 400


@pytest.mark.asyncio
async def test_wins_split_paid_due_expired_accounts(db_session, fake_redis, make_agent, make_ticket, make_user):
    """« Gains gagnés » (revenu) ≠ argent payé : on montre où en est chaque gain.
    Ticket payé 300, ticket actif 120 (pas encore présenté), ticket expiré 80,
    compte joueur 50, Keno gagné sur ticket actif 40 -> total 590."""
    agent = await make_agent()
    player = await make_user()
    race = await Lucky6Service(db_session, fake_redis).create_race()

    async def ticket(status, expired=False):
        info = await make_ticket(agent, balance=Decimal("10"))
        t = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == info["ticket_number"]))).scalar_one()
        t.status = status
        if expired:
            t.expires_at = now_utc() - timedelta(days=1)
        return t

    paid, active, old = await ticket(TicketStatus.PAID), await ticket(TicketStatus.ACTIVE), await ticket(TicketStatus.ACTIVE, expired=True)
    for funding, win in (({"ticket_id": paid.id}, "300"), ({"ticket_id": active.id}, "120"),
                         ({"ticket_id": old.id}, "80"), ({"user_id": player.id}, "50")):
        db_session.add(GameBet(round_id=race.id, game_type="lucky6", agent_id=agent.id, bet_type="SIX", selection=[1, 2, 3, 4, 5, 6],
                               stake=Decimal("10"), odds=Decimal("6"), potential_payout=Decimal(win), status="WON",
                               winnings=Decimal(win), placed_at=now_utc(), **funding))
    keno = KenoService(db_session, fake_redis)
    draw = await keno.generate_draw(draw_time=now_utc() + timedelta(minutes=5), config=await keno.get_config())
    db_session.add(KenoBet(draw_id=draw.id, ticket_id=active.id, agent_id=agent.id, picks=[1, 2], stake=Decimal("10"),
                           status=KenoBetStatus.WON, winnings=Decimal("40"), placed_at=now_utc()))
    await db_session.flush()

    p = await FinanceReportService(db_session).period(*_bounds())
    assert p["wins"] == 590.0
    assert p["wins_split"] == {"paid": 300.0, "due": 160.0, "unclaimed": 80.0, "accounts": 50.0}
    # payés = remis au guichet ; dus = tickets valables + comptes ; jamais réclamés = restent à la maison
    assert p["wins_paid"] == 300.0 and p["wins_due"] == 210.0 and p["wins_unclaimed"] == 80.0
    assert p["revenue"] == p["stakes"] - p["wins_paid"] - p["wins_due"]
    assert sum(p["wins_split"].values()) == p["wins"]
    (totals,) = await FinanceReportService(db_session).game_totals(list(_bounds()))
    assert totals["wins"] == 590.0 and totals["bets"] == 5
