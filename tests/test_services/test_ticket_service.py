# tests/test_services/test_ticket_service.py
"""Tests du cycle de vie d'un ticket : un joueur sans compte paie cash à un
agent de bureau (création du ticket), joue avec ce solde, puis se fait payer
le reste en cash (TicketService.payout_ticket)."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.exceptions import AppException
from app.models.enums import TicketStatus
from app.services.ticket_service import TicketService


@pytest.mark.asyncio
async def test_create_ticket_credits_bureau_cash_and_ticket_balance(
    db_session, fake_redis, make_agent
):
    agent = await make_agent()
    service = TicketService(db_session, fake_redis)

    ticket = await service.create_ticket(
        agent_id=agent.id, bureau_id=agent.bureau_id, amount=Decimal("500")
    )

    assert ticket["balance"] == 500.0
    assert ticket["status"] == TicketStatus.ACTIVE
    assert ticket["qr_code"].startswith("data:image/png;base64,")

    from app.models.bureau import Bureau

    bureau = await db_session.get(Bureau, agent.bureau_id)
    assert bureau.cash_balance == Decimal("500")


@pytest.mark.asyncio
async def test_payout_ticket_pays_full_balance_and_debits_bureau_cash(
    db_session, fake_redis, make_agent
):
    agent = await make_agent()
    service = TicketService(db_session, fake_redis)
    ticket = await service.create_ticket(
        agent_id=agent.id, bureau_id=agent.bureau_id, amount=Decimal("500")
    )

    result = await service.payout_ticket(ticket["ticket_number"], agent_id=agent.id)

    assert result["success"] is True
    assert result["amount"] == 500.0

    paid_ticket = await service.get_by_number(ticket["ticket_number"])
    assert paid_ticket.status == TicketStatus.PAID
    assert paid_ticket.balance == Decimal("0")

    from app.models.bureau import Bureau

    bureau = await db_session.get(Bureau, agent.bureau_id)
    assert bureau.cash_balance == Decimal("0")  # remis en caisse puis ressorti


@pytest.mark.asyncio
async def test_payout_ticket_twice_is_rejected(db_session, fake_redis, make_agent):
    agent = await make_agent()
    service = TicketService(db_session, fake_redis)
    ticket = await service.create_ticket(
        agent_id=agent.id, bureau_id=agent.bureau_id, amount=Decimal("500")
    )
    await service.payout_ticket(ticket["ticket_number"], agent_id=agent.id)

    with pytest.raises(AppException):
        await service.payout_ticket(ticket["ticket_number"], agent_id=agent.id)


@pytest.mark.asyncio
async def test_payout_ticket_no_balance_is_rejected(db_session, fake_redis, make_agent):
    agent = await make_agent()
    service = TicketService(db_session, fake_redis)
    ticket = await service.create_ticket(
        agent_id=agent.id, bureau_id=agent.bureau_id, amount=Decimal("0")
    )

    with pytest.raises(AppException):
        await service.payout_ticket(ticket["ticket_number"], agent_id=agent.id)


@pytest.mark.asyncio
async def test_payout_expired_ticket_is_rejected_and_marks_it_expired(
    db_session, fake_redis, make_agent
):
    agent = await make_agent()
    service = TicketService(db_session, fake_redis)
    ticket = await service.create_ticket(
        agent_id=agent.id, bureau_id=agent.bureau_id, amount=Decimal("500")
    )
    ticket_row = await service.get_by_number(ticket["ticket_number"])
    ticket_row.expires_at = datetime.utcnow() - timedelta(days=1)
    await db_session.flush()

    with pytest.raises(AppException):
        await service.payout_ticket(ticket["ticket_number"], agent_id=agent.id)

    assert ticket_row.status == TicketStatus.EXPIRED
