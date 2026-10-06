# tests/test_api/test_payouts.py
"""Paiement des gains par l'agent : le serveur calcule le montant, verrouille
le ticket, refuse le double paiement, les paris en attente, l'autre bureau,
la caisse fermée ou insuffisante, et compte la sortie dans la caisse."""

import re
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.timezone import now_utc
from app.models.bureau import Bureau, CashierSession
from app.models.enums import TicketStatus
from app.models.game import GameBet
from app.models.ticket import Ticket
from app.routes.agent import router as agent_panel_router
from app.services.lucky6_service import Lucky6Service


@pytest_asyncio.fixture
async def client(db_session, fake_redis):
    app = FastAPI()
    app.include_router(agent_panel_router)
    app.add_middleware(AdminCsrfMiddleware)

    async def _db():
        yield db_session

    async def _redis():
        return fake_redis

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _csrf(html):
    return re.search(r'name="csrf_token"\s+value="([^"]*)"', html).group(1)


async def _login(client, agent):
    page = await client.get("/agent/login")
    r = await client.post("/agent/login", data={"code": agent.phone, "password": "AgentPass123!", "csrf_token": _csrf(page.text)},
                          follow_redirects=False)
    assert r.status_code == 303


async def _headers(client):
    return {"X-CSRFToken": (await client.get("/agent/api/keno/csrf-token")).json()["csrf_token"]}


async def _open_session(db_session, agent, cash="1000"):
    session = CashierSession(bureau_id=agent.bureau_id, agent_id=agent.id, starting_balance=Decimal(cash),
                             current_balance=Decimal(cash), expected_balance=Decimal(cash), status="OPEN", opened_at=now_utc())
    db_session.add(session)
    await db_session.flush()
    return session


async def _winning_ticket(db_session, fake_redis, make_ticket, agent, winnings="300", status="WON"):
    """Ticket vendu au comptant (solde 0 après la mise), pari Lucky6 réglé :
    le gain est crédité sur le solde du ticket comme le fait le règlement."""
    info = await make_ticket(agent, balance=Decimal("50"))
    ticket = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == info["ticket_number"]))).scalar_one()
    race = await Lucky6Service(db_session, fake_redis).create_race()
    bet = GameBet(round_id=race.id, game_type="lucky6", ticket_id=ticket.id, agent_id=agent.id, bet_type="SIX",
                  selection=[1, 2, 3, 4, 5, 6], stake=Decimal("50"), odds=Decimal("6"), potential_payout=Decimal(winnings),
                  status=status, winnings=Decimal(winnings) if status == "WON" else Decimal("0"), placed_at=now_utc())
    db_session.add(bet)
    ticket.balance = Decimal(winnings) if status == "WON" else Decimal("0")
    await db_session.flush()
    return ticket


@pytest.mark.asyncio
async def test_summary_shows_bets_and_amount(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent)
    await _login(client, agent)
    r = await client.get(f"/agent/api/payouts/{ticket.ticket_number.lower()}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["can_pay"] is True and data["amount_to_pay"] == 300
    assert data["total_winnings"] == 300 and data["bets"][0]["game"] == "Lucky6" and data["bets"][0]["status_label"] == "Gagné"


@pytest.mark.asyncio
async def test_pay_once_records_amount_and_cash_session(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    session = await _open_session(db_session, agent)
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent)
    await _login(client, agent)
    r = await client.post("/agent/api/payouts/pay", headers=await _headers(client), json={"ticket_number": ticket.ticket_number})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["paid_now"] == 300 and r.json()["data"]["status"] == "paid"
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.PAID and ticket.paid_amount == Decimal("300") and ticket.balance == 0
    assert ticket.paid_by_agent == agent.id
    assert session.cash_out_count == 1 and session.cash_out_amount == Decimal("300") and session.current_balance == Decimal("700")

    again = await client.post("/agent/api/payouts/pay", headers=await _headers(client), json={"ticket_number": ticket.ticket_number})
    assert again.status_code == 400 and "déjà payé" in again.json()["detail"]
    assert session.cash_out_count == 1
    summary = (await client.get(f"/agent/api/payouts/{ticket.ticket_number}")).json()
    assert summary["can_pay"] is False and "déjà payé" in summary["reason"]


@pytest.mark.asyncio
async def test_pending_bet_blocks_payment(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    await _open_session(db_session, agent)
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent)
    race = await Lucky6Service(db_session, fake_redis).create_race()
    db_session.add(GameBet(round_id=race.id, game_type="lucky6", ticket_id=ticket.id, agent_id=agent.id, bet_type="FIRST_ODD",
                           selection=[], stake=Decimal("10"), odds=Decimal("1.9"), potential_payout=Decimal("19"),
                           status="PENDING", placed_at=now_utc()))
    await db_session.flush()
    await _login(client, agent)
    assert (await client.get(f"/agent/api/payouts/{ticket.ticket_number}")).json()["pending"] == 1
    r = await client.post("/agent/api/payouts/pay", headers=await _headers(client), json={"ticket_number": ticket.ticket_number})
    assert r.status_code == 400 and "pas encore connu" in r.json()["detail"]
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.ACTIVE and ticket.balance == Decimal("300")


@pytest.mark.asyncio
async def test_other_bureau_refused(client, db_session, fake_redis, make_agent, make_ticket):
    seller = await make_agent()
    other = await make_agent()
    await _open_session(db_session, other)
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, seller)
    await _login(client, other)
    r = await client.post("/agent/api/payouts/pay", headers=await _headers(client), json={"ticket_number": ticket.ticket_number})
    assert r.status_code == 400 and "autre bureau" in r.json()["detail"]
    assert "se paie dans ce bureau" in (await client.get(f"/agent/api/payouts/{ticket.ticket_number}")).json()["reason"]


@pytest.mark.asyncio
async def test_requires_open_and_sufficient_cash(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent)
    await _login(client, agent)
    r = await client.post("/agent/api/payouts/pay", headers=await _headers(client), json={"ticket_number": ticket.ticket_number})
    assert r.status_code == 400 and "session de caisse" in r.json()["detail"]

    await _open_session(db_session, agent, cash="100")
    r = await client.post("/agent/api/payouts/pay", headers=await _headers(client), json={"ticket_number": ticket.ticket_number})
    assert r.status_code == 400 and "Caisse insuffisante" in r.json()["detail"]
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.ACTIVE


@pytest.mark.asyncio
async def test_losing_ticket_nothing_to_pay(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    await _open_session(db_session, agent)
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent, winnings="0", status="LOST")
    await _login(client, agent)
    summary = (await client.get(f"/agent/api/payouts/{ticket.ticket_number}")).json()
    assert summary["can_pay"] is False and "Aucun gain" in summary["reason"]
    r = await client.post("/agent/api/payouts/pay", headers=await _headers(client), json={"ticket_number": ticket.ticket_number})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unknown_ticket_and_page(client, db_session, make_agent):
    agent = await make_agent()
    await _login(client, agent)
    assert (await client.get("/agent/api/payouts/KNO-XXXX-0000")).status_code == 404
    page = await client.get("/agent/gains?ticket=kno-abcd-1234")
    assert page.status_code == 200 and "KNO-ABCD-1234" in page.text and "Payer les gains" in page.text


@pytest.mark.asyncio
async def test_bureau_cash_decreases(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    await _open_session(db_session, agent)
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent)
    bureau = await db_session.get(Bureau, agent.bureau_id)
    before = bureau.cash_balance
    await _login(client, agent)
    r = await client.post("/agent/api/payouts/pay", headers=await _headers(client), json={"ticket_number": ticket.ticket_number})
    assert r.status_code == 200, r.text
    await db_session.refresh(bureau)
    assert bureau.cash_balance == before - Decimal("300")


# ---------------------------------------------------------------------------
# Tous les chemins de paiement enregistrent le montant payé (tickets.paid_amount)
# ---------------------------------------------------------------------------

def test_record_payout_partial_then_full():
    from datetime import datetime, timedelta

    t = Ticket(ticket_number="KNO-TEST-0001", bureau_id="b", initial_amount=Decimal("50"), balance=Decimal("300"),
               status=TicketStatus.ACTIVE, expires_at=datetime.utcnow() + timedelta(days=1))
    assert t.record_payout(Decimal("100"), "a1") == Decimal("100")
    assert t.status == TicketStatus.ACTIVE and t.balance == Decimal("200") and t.paid_amount == Decimal("100")
    t.record_payout(Decimal("200"), "a2")
    assert t.status == TicketStatus.PAID and t.paid_amount == Decimal("300") and t.paid_by_agent == "a2" and t.paid_at
    with pytest.raises(ValueError):
        t.record_payout(Decimal("1"), "a2")  # plus de solde


async def _admin(client):
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    page = await client.get("/admin/login")
    r = await client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": _csrf(page.text)},
                          follow_redirects=False)
    assert r.status_code == 303
    return {"X-CSRFToken": _csrf((await client.get("/admin/login")).text)}


@pytest.mark.asyncio
async def test_admin_payout_records_winnings_not_stake(admin_client, admin_user, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent)  # mise 50, gain 300
    headers = await _admin(admin_client)
    r = await admin_client.post(f"/admin/api/tickets/{ticket.id}/payout", headers=headers)
    assert r.status_code == 200, r.text
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.PAID and ticket.paid_amount == Decimal("300") and ticket.amount_paid == Decimal("300")


@pytest.mark.asyncio
async def test_admin_payout_refuses_pending_bets(admin_client, admin_user, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent)
    race = await Lucky6Service(db_session, fake_redis).create_race()
    db_session.add(GameBet(round_id=race.id, game_type="lucky6", ticket_id=ticket.id, agent_id=agent.id, bet_type="SIX",
                           selection=[1, 2, 3, 4, 5, 6], stake=Decimal("10"), odds=Decimal("6"), potential_payout=Decimal("60"),
                           status="PENDING", placed_at=now_utc()))
    await db_session.flush()
    r = await admin_client.post(f"/admin/api/tickets/{ticket.id}/payout", headers=await _admin(admin_client))
    assert r.status_code == 400 and "pas encore connu" in r.json()["detail"]


@pytest.mark.asyncio
async def test_admin_bulk_payout_records_amounts_and_debits_bureau(admin_client, admin_user, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    t1 = await _winning_ticket(db_session, fake_redis, make_ticket, agent, winnings="300")
    t2 = await _winning_ticket(db_session, fake_redis, make_ticket, agent, winnings="120")
    bureau = await db_session.get(Bureau, agent.bureau_id)
    before = Decimal(str(bureau.cash_balance or 0))
    r = await admin_client.post("/admin/api/tickets/bulk/payout", headers=await _admin(admin_client), json=[t1.id, t2.id])
    assert r.status_code == 200, r.text
    assert r.json()["paid"] == 2 and r.json()["total"] == 420.0
    for t, amount in ((t1, "300"), (t2, "120")):
        await db_session.refresh(t)
        assert t.status == TicketStatus.PAID and t.paid_amount == Decimal(amount)
    await db_session.refresh(bureau)
    assert bureau.cash_balance == before - Decimal("420")  # avant : rien n'était débité


@pytest.mark.asyncio
async def test_finance_counts_paid_winnings_after_admin_payout(admin_client, admin_user, db_session, fake_redis, make_agent, make_ticket):
    """Le cas de la capture : ticket vendu 50, gagné 300, payé -> « Gains payés
    sur tickets » = 300 (et non 50, la mise)."""
    from app.services.finance_report_service import FinanceReportService
    from datetime import timedelta

    agent = await make_agent()
    ticket = await _winning_ticket(db_session, fake_redis, make_ticket, agent)
    await admin_client.post(f"/admin/api/tickets/{ticket.id}/payout", headers=await _admin(admin_client))
    p = await FinanceReportService(db_session).period(now_utc() - timedelta(days=1), now_utc() + timedelta(days=1))
    assert p["out"]["ticket_payouts"] == 300.0 and p["wins_split"]["paid"] == 300.0
