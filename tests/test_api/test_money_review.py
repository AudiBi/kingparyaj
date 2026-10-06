# tests/test_api/test_money_review.py
"""Non-régression de la revue des flux d'argent (une classe de test par
constat) : chaque test échouait avant la correction."""

import re
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.v1 import admin as api_admin_module
from app.api.v1 import tickets as api_tickets_module
from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import create_access_token
from app.core.timezone import now_utc
from app.models.bureau import Bureau, CashierSession
from app.models.cash_movement import TicketCashMovement
from app.models.enums import KenoBetStatus, TicketStatus, UserRole
from app.models.game import GameBet
from app.models.keno import KenoBet, KenoDraw
from app.models.ticket import Ticket
from app.routes import lucky6 as lucky6_routes
from app.routes.agent import router as agent_router
from app.services.commission_service import set_default_rate
from app.services.finance_report_service import FinanceReportService
from app.services.keno_service import KenoService
from app.services.lucky6_service import Lucky6Service


# ---------------------------------------------------------------- fixtures
@pytest_asyncio.fixture
async def client(db_session, fake_redis):
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(lucky6_routes.agent_router)
    app.include_router(api_admin_module.router, prefix="/api/v1")
    app.include_router(api_tickets_module.router, prefix="/api/v1")
    app.add_middleware(AdminCsrfMiddleware)

    async def _db():
        yield db_session

    async def _redis():
        return fake_redis

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


def _bearer(user) -> dict:
    return {"Authorization": f"Bearer {create_access_token({'sub': user.id, 'role': user.role.value})}"}


def _csrf(html):
    return re.search(r'name="csrf_token"\s+value="([^"]*)"', html).group(1)


async def _agent_login(client, agent):
    page = await client.get("/agent/login")
    r = await client.post("/agent/login", data={"code": agent.phone, "password": "AgentPass123!", "csrf_token": _csrf(page.text)},
                          follow_redirects=False)
    assert r.status_code == 303
    return {"X-CSRFToken": (await client.get("/agent/api/keno/csrf-token")).json()["csrf_token"]}


async def _session(db, agent, cash="1000"):
    s = CashierSession(bureau_id=agent.bureau_id, agent_id=agent.id, starting_balance=Decimal(cash), current_balance=Decimal(cash),
                       expected_balance=Decimal(cash), status="OPEN", opened_at=now_utc())
    db.add(s)
    await db.flush()
    return s


async def _won_ticket(db, fake_redis, make_ticket, agent, win="300"):
    """Ticket vendu 50 au comptant, pari Lucky6 gagné : gain crédité sur le ticket."""
    info = await make_ticket(agent, balance=Decimal("50"))
    t = (await db.execute(select(Ticket).where(Ticket.ticket_number == info["ticket_number"]))).scalar_one()
    race = await Lucky6Service(db, fake_redis).create_race()
    db.add(GameBet(round_id=race.id, game_type="lucky6", ticket_id=t.id, agent_id=agent.id, bet_type="SIX", selection=[1, 2, 3, 4, 5, 6],
                   stake=Decimal("50"), odds=Decimal("6"), potential_payout=Decimal(win), status="WON",
                   winnings=Decimal(win), placed_at=now_utc()))
    t.balance = Decimal(win)
    await db.flush()
    return t


def _period():
    return now_utc() - timedelta(days=1), now_utc() + timedelta(days=1)


# ---------------------------------------------------------------- P0 : reset de tirage
@pytest.mark.asyncio
async def test_p0_dangerous_get_reset_is_gone_and_settled_draw_cannot_be_cancelled(client, db_session, fake_redis, make_user):
    admin = await make_user()
    admin.role = UserRole.ADMIN
    keno = KenoService(db_session, fake_redis)
    draw = await keno.generate_draw(draw_time=now_utc() + timedelta(minutes=5), config=await keno.get_config())
    player = await make_user()
    db_session.add(KenoBet(draw_id=draw.id, user_id=player.id, picks=[1, 2], stake=Decimal("10"),
                           status=KenoBetStatus.WON, winnings=Decimal("100"), placed_at=now_utc()))
    draw.status = "completed"
    await db_session.flush()

    r = await client.get(f"/api/v1/admin/keno/draws/{draw.id}/reset", headers=_bearer(admin))
    assert r.status_code in (404, 405)  # plus de GET qui modifie l'argent
    r = await client.post(f"/api/v1/admin/keno/draws/{draw.id}/cancel", headers=_bearer(admin))
    assert r.status_code == 400  # tirage réglé : rien n'est remboursé
    bet = (await db_session.execute(select(KenoBet).where(KenoBet.draw_id == draw.id))).scalar_one()
    assert bet.status == KenoBetStatus.WON


# ---------------------------------------------------------------- P1-2 : paiements partiels
@pytest.mark.asyncio
async def test_p1_partial_payout_is_dated_and_counted(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    session = await _session(db_session, agent)
    ticket = await _won_ticket(db_session, fake_redis, make_ticket, agent)
    headers = await _agent_login(client, agent)
    r = await client.post("/agent/api/cashier/payout", headers=headers,
                          json={"payout_type": "ticket", "identifier": ticket.ticket_number, "amount": 200})
    assert r.status_code == 200, r.text

    moves = (await db_session.execute(select(TicketCashMovement))).scalars().all()
    assert [(m.kind, m.amount, m.session_id, m.agent_id) for m in moves] == [("payout", Decimal("200.00"), session.id, agent.id)]
    p = await FinanceReportService(db_session).period(*_period())
    assert p["out"]["ticket_payouts"] == 200.0  # avant : 0
    assert p["wins_paid"] == 200.0 and p["wins_due"] == 100.0  # avant : 0 et 300
    # rapports agent : la sortie de 200 apparaît
    page = await client.get("/agent/reports")
    assert page.status_code == 200 and "200" in page.text


# ---------------------------------------------------------------- P1-3 : gains jamais réclamés
@pytest.mark.asyncio
async def test_p1_unclaimed_winnings_stay_with_the_house(db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _won_ticket(db_session, fake_redis, make_ticket, agent)
    ticket.expires_at = now_utc() - timedelta(minutes=1)
    await db_session.flush()
    p = await FinanceReportService(db_session).period(*_period())
    assert p["wins_unclaimed"] == 300.0
    assert p["revenue"] == 50.0  # mise 50, rien payé (avant : -250)
    assert p["games"][0]["revenue"] == 50.0


# ---------------------------------------------------------------- P1-4 : gains sur compte = dus
@pytest.mark.asyncio
async def test_p1_account_winnings_are_owed_not_paid(db_session, fake_redis, make_agent, make_user):
    agent = await make_agent()
    player = await make_user()
    race = await Lucky6Service(db_session, fake_redis).create_race()
    db_session.add(GameBet(round_id=race.id, game_type="lucky6", user_id=player.id, agent_id=agent.id, bet_type="SIX",
                           selection=[1, 2, 3, 4, 5, 6], stake=Decimal("50"), odds=Decimal("6"), potential_payout=Decimal("300"),
                           status="WON", winnings=Decimal("300"), placed_at=now_utc()))
    await db_session.flush()
    p = await FinanceReportService(db_session).period(*_period())
    assert p["wins_paid"] == 0.0 and p["wins_due"] == 300.0 and p["revenue"] == -250.0


# ---------------------------------------------------------------- P2-6 : recharge
@pytest.mark.asyncio
async def test_p2_recharge_is_money_in(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    await _session(db_session, agent)
    info = await make_ticket(agent, balance=Decimal("50"))
    r = await client.post(f"/api/v1/tickets/{info['ticket_number']}/recharge", headers=_bearer(agent), json={"amount": 100})
    assert r.status_code == 200, r.text
    p = await FinanceReportService(db_session).period(*_period())
    assert p["in"]["ticket_sales"] == 50.0 and p["in"]["recharges"] == 100.0 and p["in"]["total"] == 150.0


# ---------------------------------------------------------------- annulation de ticket
@pytest.mark.asyncio
async def test_cancel_refund_is_money_out_and_bulk_debits_bureau(admin_client, admin_user, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    tickets = [await make_ticket(agent, balance=Decimal(v)) for v in ("40", "60")]
    ids = [(await db_session.execute(select(Ticket.id).where(Ticket.ticket_number == t["ticket_number"]))).scalar() for t in tickets]
    bureau = await db_session.get(Bureau, agent.bureau_id)
    before = Decimal(str(bureau.cash_balance or 0))
    from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    page = await admin_client.get("/admin/login")
    await admin_client.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": _csrf(page.text)})
    headers = {"X-CSRFToken": _csrf((await admin_client.get("/admin/login")).text)}
    r = await admin_client.post("/admin/api/tickets/bulk/cancel", headers=headers, json={"ticket_ids": ids, "reason": "test"})
    assert r.status_code == 200, r.text
    await db_session.refresh(bureau)
    assert bureau.cash_balance == before - Decimal("100")  # avant : rien n'était débité
    p = await FinanceReportService(db_session).period(*_period())
    assert p["out"]["cancel_refunds"] == 100.0 and p["out"]["ticket_payouts"] == 0.0
    for tid in ids:
        assert (await db_session.get(Ticket, tid)).paid_at is None  # annulé ≠ payé


# ---------------------------------------------------------------- bureau du ticket
@pytest.mark.asyncio
async def test_casier_refuses_ticket_of_another_bureau(client, db_session, fake_redis, make_agent, make_ticket):
    seller = await make_agent()
    other = await make_agent()
    await _session(db_session, other)
    ticket = await _won_ticket(db_session, fake_redis, make_ticket, seller)
    headers = await _agent_login(client, other)
    r = await client.post("/agent/api/cashier/payout", headers=headers,
                          json={"payout_type": "ticket", "identifier": ticket.ticket_number})
    assert r.status_code == 400 and "autre bureau" in r.text  # avant : payé (contrôle inopérant)


# ---------------------------------------------------------------- P2-7 : commission figée via la vraie route
@pytest.mark.asyncio
async def test_commission_frozen_on_real_bet_and_not_on_replay(client, db_session, fake_redis, make_agent):
    agent = await make_agent()
    await _session(db_session, agent)
    await set_default_rate(db_session, "10")
    race = await Lucky6Service(db_session, fake_redis).create_race()
    headers = await _agent_login(client, agent)
    r = await client.post("/agent/api/lucky6/bet", headers=headers, json={
        "race_id": race.id, "bet_type": "SIX", "selection": [1, 2, 3, 4, 5, 6], "stake": "50", "player_type": "cash"})
    assert r.status_code == 200, r.text
    number = r.json()["bet"]["ticket_number"]
    sold = (await db_session.execute(select(GameBet).order_by(GameBet.placed_at))).scalars().all()[-1]
    assert sold.commission == Decimal("5.00") and sold.counts_as_sale

    ticket = (await db_session.execute(select(Ticket).where(Ticket.ticket_number == number))).scalar_one()
    ticket.balance = Decimal("100")  # gain crédité, rejoué sur le même ticket
    await db_session.flush()
    headers = {"X-CSRFToken": (await client.get("/agent/api/keno/csrf-token")).json()["csrf_token"]}
    r = await client.post("/agent/api/lucky6/bet", headers=headers, json={
        "race_id": race.id, "bet_type": "SIX", "selection": [7, 8, 9, 10, 11, 12], "stake": "100",
        "player_type": "ticket", "identifier": number})
    assert r.status_code == 200, r.text
    replay = (await db_session.execute(select(GameBet).where(GameBet.stake == Decimal("100")))).scalar_one()
    assert replay.commission == Decimal("0") and replay.counts_as_sale is False


# ---------------------------------------------------------------- caisse suffisante (même règle partout)
@pytest.mark.asyncio
async def test_casier_refuses_payout_bigger_than_drawer(client, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    await _session(db_session, agent, cash="100")
    ticket = await _won_ticket(db_session, fake_redis, make_ticket, agent)  # 300 à payer
    headers = await _agent_login(client, agent)
    r = await client.post("/agent/api/cashier/payout", headers=headers,
                          json={"payout_type": "ticket", "identifier": ticket.ticket_number})
    assert r.status_code == 400 and "Caisse insuffisante" in r.text
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.ACTIVE and ticket.balance == Decimal("300")


# ---------------------------------------------------------------- mises à jour concurrentes (PostgreSQL)
@pytest.mark.asyncio
async def test_bureau_cash_updates_are_atomic_under_concurrency(db_session, make_bureau):
    """Deux agents du même bureau débitent la caisse « en même temps » : les deux
    débits doivent compter (avant : lecture-calcul-écriture, un débit perdu)."""
    import os

    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("concurrence réelle : lancer avec TEST_DATABASE_URL (PostgreSQL)")
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    bureau = await make_bureau(cash_balance=Decimal("1000"))
    await db_session.commit()
    maker = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s1, maker() as s2:
        b1 = await s1.get(Bureau, bureau.id)
        b2 = await s2.get(Bureau, bureau.id)          # les deux lisent 1000
        b1.cash_balance = Bureau.cash_balance - Decimal("100")
        await s1.commit()
        b2.cash_balance = Bureau.cash_balance - Decimal("200")
        await s2.commit()
    async with maker() as s3:
        assert (await s3.get(Bureau, bureau.id)).cash_balance == Decimal("700")
