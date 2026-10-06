# tests/test_services/test_ticket_same_day.py
"""Un ticket se paie le jour même (minuit, heure d'Haïti) ; un ticket dont un
pari attend son tirage n'expire pas, et un gain tombé après minuit le rend
payable jusqu'à la fin de ce jour-là."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.timezone import local_date_end_utc, now_utc, today_haiti
from app.models.enums import TicketStatus
from app.models.game import GameBet
from app.models.ticket import Ticket
from app.services.keno_service import KenoService
from app.services.lucky6_service import Lucky6Service
from app.services.payout_service import PayoutService
from app.services.ticket_service import TicketService


async def _ticket(db, make_ticket, agent, balance="50"):
    info = await make_ticket(agent, balance=Decimal(balance))
    return (await db.execute(select(Ticket).where(Ticket.ticket_number == info["ticket_number"]))).scalar_one()


@pytest.mark.asyncio
async def test_new_ticket_expires_at_local_midnight(db_session, make_agent, make_ticket):
    ticket = await _ticket(db_session, make_ticket, await make_agent())
    assert ticket.expires_at == local_date_end_utc(today_haiti())
    assert ticket.expires_at - now_utc() <= timedelta(days=1)


@pytest.mark.asyncio
async def test_ticket_with_pending_bet_does_not_expire(db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    waiting = await _ticket(db_session, make_ticket, agent)
    idle = await _ticket(db_session, make_ticket, agent)
    race = await Lucky6Service(db_session, fake_redis).create_race()
    db_session.add(GameBet(round_id=race.id, game_type="lucky6", ticket_id=waiting.id, agent_id=agent.id, bet_type="SIX",
                           selection=[1, 2, 3, 4, 5, 6], stake=Decimal("50"), odds=Decimal("6"), potential_payout=Decimal("0"),
                           status="PENDING", placed_at=now_utc()))
    for t in (waiting, idle):
        t.expires_at = now_utc() - timedelta(minutes=1)  # minuit est passé
    await db_session.flush()
    assert await TicketService(db_session, fake_redis).expire_old_tickets() == 1
    assert waiting.status == TicketStatus.ACTIVE and idle.status == TicketStatus.EXPIRED


@pytest.mark.asyncio
async def test_win_after_midnight_is_payable_until_end_of_that_day(db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent, balance="0.01")
    ticket.expires_at = now_utc() - timedelta(minutes=5)   # pari pris hier soir…
    ticket.status = TicketStatus.EXPIRED                     # …ticket expiré entre-temps
    await db_session.flush()
    await KenoService(db_session, fake_redis)._credit_ticket(ticket.id, Decimal("300"))  # gain du tirage de 0 h 05
    assert ticket.status == TicketStatus.ACTIVE
    assert ticket.expires_at == local_date_end_utc(today_haiti())
    summary = await PayoutService(db_session, fake_redis).summary(ticket.ticket_number, agent)
    assert summary["can_pay"] and summary["amount_to_pay"] == 300.01


@pytest.mark.asyncio
async def test_expired_ticket_reason_mentions_same_day(db_session, fake_redis, make_agent, make_ticket):
    agent = await make_agent()
    ticket = await _ticket(db_session, make_ticket, agent)
    ticket.expires_at = now_utc() - timedelta(minutes=1)
    await db_session.flush()
    summary = await PayoutService(db_session, fake_redis).summary(ticket.ticket_number, agent)
    assert not summary["can_pay"] and "le jour même" in summary["reason"]
