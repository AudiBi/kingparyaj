# tests/test_api/test_admin_tickets_list.py
"""Liste des tickets du panel admin : filtres, pages, API détail, paiement, annulation, actions groupées, export."""

import csv
from decimal import Decimal
from io import StringIO

import pytest
from sqlalchemy import select

from app.models.bureau import Bureau
from app.models.enums import TicketStatus
from app.models.ticket import Ticket
from app.services.horse_race_service import HorseRaceService


async def _ticket(db_session, make_ticket, agent, balance="100") -> Ticket:
    info = await make_ticket(agent, balance=Decimal(balance))
    return (await db_session.execute(select(Ticket).where(Ticket.ticket_number == info["ticket_number"]))).scalar_one()


async def _with_pending_bet(db_session, fake_redis, agent, ticket, launch=False):
    service = HorseRaceService(db_session, fake_redis)
    race = await service.create_race()
    await service.place_bet(race.id, "WIN", [race.participants[0]["player_number"]], "20",
                            ticket_number=ticket.ticket_number, agent_id=agent.id)
    if launch:
        await service.launch_race(race.id, by=agent.id)
    await db_session.commit()


# ======================================================================== liste, filtres, pages

@pytest.mark.asyncio
async def test_list_filters_and_pages(panel, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, "150")
    ticket.player_name = "Rosemond"
    await db_session.commit()

    assert (await panel.get("/admin/tickets")).status_code == 200
    for query in (
        "status=active", "status=paid", "status=expired", "status=cancelled",
        f"bureau_id={agent.bureau_id}", f"agent_id={agent.id}", "search=Rosemond", f"search={ticket.ticket_number}",
        "min_amount=100", "max_amount=500", "min_amount=100&max_amount=200",
        "start_date=2020-01-01&end_date=2099-12-31", "per_page=100", "page=999",
    ):
        assert (await panel.get(f"/admin/tickets?{query}")).status_code == 200, query
    assert ticket.ticket_number in (await panel.get("/admin/tickets?search=Rosemond")).text
    assert ticket.ticket_number not in (await panel.get("/admin/tickets?status=paid")).text
    assert ticket.ticket_number not in (await panel.get("/admin/tickets?min_amount=1000")).text
    assert ticket.ticket_number in (await panel.get("/admin/tickets?start_date=2020-01-01&end_date=2099-12-31")).text
    assert ticket.ticket_number not in (await panel.get("/admin/tickets?end_date=2000-01-01")).text


@pytest.mark.asyncio
async def test_invalid_filter_values_do_not_crash_the_page(panel):
    for query in ("status=zzz", "start_date=pas-une-date", "end_date=31/12/2026", "min_amount=abc", "bureau_id=inconnu"):
        response = await panel.get(f"/admin/tickets?{query}")
        assert response.status_code in (200, 400, 422), (query, response.status_code)  # 400/422 : valeur refusée proprement, jamais 500


@pytest.mark.asyncio
async def test_detail_page_and_api(panel, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent)
    await db_session.commit()
    assert (await panel.get(f"/admin/tickets/{ticket.id}")).status_code == 200
    api = await panel.get(f"/admin/api/tickets/{ticket.id}")
    assert api.status_code == 200, api.text
    assert api.json()["ticket_number"] == ticket.ticket_number and api.json()["balance"] == 100.0
    assert (await panel.get("/admin/api/tickets/inconnu")).status_code == 404
    assert (await panel.get("/admin/tickets/inconnu")).status_code == 404
    qr = await panel.get(f"/admin/api/tickets/{ticket.ticket_number}/qr")
    assert qr.status_code == 200 and qr.json()["success"] is True
    assert (await panel.get("/admin/api/tickets/statistics")).status_code == 200


# ======================================================================== paiement

@pytest.mark.asyncio
async def test_single_payout(panel, db_session, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, "80")
    bureau = await db_session.get(Bureau, agent.bureau_id)
    before = Decimal(str(bureau.cash_balance or 0))
    await db_session.commit()
    r = await panel.post(f"/admin/api/tickets/{ticket.id}/payout")
    assert r.status_code == 200 and r.json()["amount"] == 80.0, r.text
    await db_session.refresh(ticket)
    await db_session.refresh(bureau)
    assert ticket.status == TicketStatus.PAID and ticket.balance == 0 and bureau.cash_balance == before - Decimal("80")
    again = await panel.post(f"/admin/api/tickets/{ticket.id}/payout")
    assert again.status_code == 400 and "n'est plus actif" in again.json()["error"]
    assert (await panel.post("/admin/api/tickets/inconnu/payout")).status_code == 404


@pytest.mark.asyncio
async def test_payout_waits_for_the_draw(panel, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, "100")
    await _with_pending_bet(db_session, fake_redis, agent, ticket)
    r = await panel.post(f"/admin/api/tickets/{ticket.id}/payout")
    assert r.status_code == 400 and "pas encore connu" in r.json()["error"]
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.ACTIVE


@pytest.mark.asyncio
async def test_bulk_payout_skips_pending_and_empty_tickets(panel, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    good = await _ticket(db_session, make_ticket, agent, "60")
    pending = await _ticket(db_session, make_ticket, agent, "100")
    empty = await _ticket(db_session, make_ticket, agent, "50")
    empty.balance = Decimal("0")
    await _with_pending_bet(db_session, fake_redis, agent, pending)
    r = await panel.post("/admin/api/tickets/bulk/payout", json={"ticket_ids": [good.id, pending.id, empty.id]})
    assert r.status_code == 200, r.text
    assert r.json()["paid"] == 1 and r.json()["skipped"] == 2 and r.json()["total"] == 60.0
    for t in (good, pending, empty):
        await db_session.refresh(t)
    assert (good.status, pending.status, empty.status) == (TicketStatus.PAID, TicketStatus.ACTIVE, TicketStatus.ACTIVE)


# ======================================================================== annulation

@pytest.mark.asyncio
async def test_single_cancel_and_refusals(panel, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, "100")
    await db_session.commit()
    r = await panel.post(f"/admin/api/tickets/{ticket.id}/cancel", json={"reason": "Erreur"})
    assert r.status_code == 200, r.text
    await db_session.refresh(ticket)
    assert ticket.status == TicketStatus.CANCELLED
    again = await panel.post(f"/admin/api/tickets/{ticket.id}/cancel", json={"reason": "Encore"})
    assert again.status_code == 400 and "actifs" in again.json()["error"]
    no_reason = await panel.post(f"/admin/api/tickets/{ticket.id}/cancel", json={})
    assert no_reason.status_code == 422


@pytest.mark.asyncio
async def test_bulk_cancel_message(panel, db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ok = await _ticket(db_session, make_ticket, agent, "40")
    locked = await _ticket(db_session, make_ticket, agent, "100")
    await _with_pending_bet(db_session, fake_redis, agent, locked, launch=True)
    r = await panel.post("/admin/api/tickets/bulk/cancel", json={"ticket_ids": [ok.id, locked.id], "reason": "Test"})
    assert r.status_code == 200 and r.json()["skipped"] == 1 and "tirage déjà lancé" in r.json()["message"]


# ======================================================================== export

def _rows(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))


@pytest.mark.asyncio
async def test_export_follows_filters_and_selection(panel, db_session, make_agent, make_ticket):
    agent = await make_agent()
    one = await _ticket(db_session, make_ticket, agent, "100")
    two = await _ticket(db_session, make_ticket, agent, "200")
    two.status = TicketStatus.PAID
    await db_session.commit()

    rows = _rows(await panel.get("/admin/api/tickets/export"))
    numbers = {r[0] for r in rows[1:]} | {r[1] for r in rows[1:]}
    assert one.ticket_number in numbers and two.ticket_number in numbers
    paid = _rows(await panel.get("/admin/api/tickets/export?status=paid"))
    assert two.ticket_number in str(paid) and one.ticket_number not in str(paid)
    picked = _rows(await panel.get(f"/admin/api/tickets/export?ids={one.id}"))
    assert one.ticket_number in str(picked) and two.ticket_number not in str(picked)  # les tickets cochés
    assert (await panel.client.get("/admin/api/tickets/export?status=zzz")).status_code == 200


@pytest.mark.asyncio
async def test_bulk_export_action_has_a_route(panel):
    """La page propose « Exporter » dans les actions groupées : elle télécharge le CSV des tickets cochés."""
    html = (await panel.get("/admin/tickets")).text
    assert "bulkAction('export')" in html
