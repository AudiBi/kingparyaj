# tests/test_api/test_ticket_play.py
"""Test de bout en bout : un joueur sans compte paie cash à un agent de
bureau (ticket), joue avec ce ticket, puis se fait payer en cash.

Régression pour un bug bloquant trouvé en auditant ce parcours :
`ticket.status != "ACTIVE"` (chaîne littérale) alors que TicketStatus.ACTIVE
vaut "active" (minuscule) — un ticket pourtant actif était donc TOUJOURS
rejeté par POST /keno/ticket-bets et POST /lucky/wheel/spin-ticket, qui
répondaient à tort "Ticket expiré ou déjà payé". Les tests ci-dessous
vérifient qu'un ticket actif est désormais accepté par les deux routes.

KenoDraw/KenoBet utilisent des colonnes ARRAY (Postgres-only, non créables
sur la base SQLite de test) : le test Keno s'arrête donc volontairement à la
vérification "solde ticket insuffisant" (qui répond avant toute requête sur
ces tables) pour prouver que le contrôle de statut est passé. Le test Lucky,
lui, va jusqu'au bout (LuckyPlay n'utilise que du JSON, portable).
"""

from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1 import keno as keno_module
from app.api.v1 import lucky as lucky_module
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import create_access_token
from app.models.lucky import LuckyWheelConfig
from app.services.ticket_service import TicketService


@pytest.fixture
def ticket_play_app(db_session, fake_redis) -> FastAPI:
    app = FastAPI()
    app.include_router(keno_module.router, prefix="/api/v1")
    app.include_router(lucky_module.router, prefix="/api/v1")

    async def _get_db():
        yield db_session

    async def _get_redis():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_redis] = _get_redis
    return app


@pytest.fixture
async def ticket_play_client(ticket_play_app):
    transport = ASGITransport(app=ticket_play_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _agent_headers(agent_id: str) -> dict:
    token = create_access_token({"sub": agent_id, "role": "agent"})
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_active_ticket_is_accepted_by_keno_ticket_bet_route(
    ticket_play_client, make_agent, make_ticket
):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("5"))  # volontairement petit

    response = await ticket_play_client.post(
        "/api/v1/keno/ticket-bets",
        params={"ticket_number": ticket["ticket_number"]},
        json={"draw_id": "does-not-need-to-exist-yet", "picks": [1, 2, 3], "stake": 1000},
        headers=_agent_headers(agent.id),
    )

    # Avant le correctif : 400 "Ticket expiré ou déjà payé" pour N'IMPORTE
    # QUEL ticket, même actif. Après : le contrôle de statut passe, et c'est
    # le solde insuffisant (mise 1000 > solde 5) qui est détecté ensuite.
    assert response.status_code == 400
    assert "insuffisant" in response.json()["detail"]


@pytest.mark.asyncio
async def test_expired_or_paid_ticket_is_still_rejected_by_keno_route(
    ticket_play_client, make_agent, make_ticket, db_session, fake_redis
):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("500"))

    ticket_service = TicketService(db_session, fake_redis)
    await ticket_service.payout_ticket(ticket["ticket_number"], agent_id=agent.id)

    response = await ticket_play_client.post(
        "/api/v1/keno/ticket-bets",
        params={"ticket_number": ticket["ticket_number"]},
        json={"draw_id": "whatever", "picks": [1, 2, 3], "stake": 10},
        headers=_agent_headers(agent.id),
    )

    assert response.status_code == 400
    assert "expiré ou déjà payé" in response.json()["detail"]


@pytest.mark.asyncio
async def test_active_ticket_can_spin_the_lucky_wheel_and_get_paid_out(
    ticket_play_client, make_agent, make_ticket, db_session, fake_redis
):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("100"))

    # Roue à un seul segment (poids 100%) : résultat déterministe, quel que
    # soit le tirage aléatoire interne.
    config = LuckyWheelConfig(
        name="Roue de test",
        segments=[{"label": "x2", "multiplier": 2, "weight": 100, "color": "#000000"}],
        min_bet=Decimal("10"),
        max_bet=Decimal("10000"),
        is_active=True,
        is_default=True,
    )
    db_session.add(config)
    await db_session.flush()

    response = await ticket_play_client.post(
        "/api/v1/lucky/wheel/spin-ticket",
        params={"ticket_number": ticket["ticket_number"], "stake": 50},
        headers=_agent_headers(agent.id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["multiplier"] == 2.0
    assert data["winnings"] == 100.0
    # Solde ticket : 100 (initial) - 50 (mise) + 100 (gain x2) = 150
    assert data["new_balance"] == 150.0

    ticket_service = TicketService(db_session, fake_redis)
    payout = await ticket_service.payout_ticket(ticket["ticket_number"], agent_id=agent.id)
    assert payout["amount"] == 150.0
