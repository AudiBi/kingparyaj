# tests/test_api/test_ticket_play.py
"""Test de bout en bout : un joueur sans compte paie cash à un agent de
bureau (ticket), joue avec ce ticket, puis se fait payer en cash.

Régression pour un bug bloquant trouvé en auditant ce parcours :
`ticket.status != "ACTIVE"` (chaîne littérale) alors que TicketStatus.ACTIVE
vaut "active" (minuscule) — un ticket pourtant actif était donc TOUJOURS
rejeté par POST /keno/ticket-bets, qui
répondaient à tort "Ticket expiré ou déjà payé". Les tests ci-dessous
vérifient qu'un ticket actif est désormais accepté par les deux routes.

Le test Keno s'arrête volontairement à la vérification "solde ticket
insuffisant" pour prouver que le contrôle de statut est passé (le règlement
Keno complet est couvert par tests/test_services/test_keno_service.py).
"""

from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1 import keno as keno_module
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import create_access_token
from app.services.ticket_service import TicketService


@pytest.fixture
def ticket_play_app(db_session, fake_redis) -> FastAPI:
    app = FastAPI()
    app.include_router(keno_module.router, prefix="/api/v1")

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


async def _open_keno_draw(db_session, fake_redis=None, agent_id="agent"):
    """Tirage instantané préparé (empreinte publiée avant le pari)."""
    import json

    from app.services import keno_engine
    from app.services.keno_service import CONFIG_KEY, KenoService

    # option « instantané » (le mode par défaut est partagé)
    await fake_redis.set(CONFIG_KEY, json.dumps(keno_engine.validate_config({"mode": "instant"})))
    return await KenoService(db_session, fake_redis).prepare_instant_draw(agent_id)


@pytest.mark.asyncio
async def test_active_ticket_is_accepted_by_keno_ticket_bet_route(
    ticket_play_client, make_agent, make_ticket, db_session, fake_redis
):
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("5"))  # volontairement petit
    draw = await _open_keno_draw(db_session, fake_redis, agent.id)

    response = await ticket_play_client.post(
        "/api/v1/keno/ticket-bets",
        params={"ticket_number": ticket["ticket_number"]},
        json={"draw_id": draw.id, "picks": [1, 2, 3], "stake": 1000},
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
    draw = await _open_keno_draw(db_session, fake_redis, agent.id)

    response = await ticket_play_client.post(
        "/api/v1/keno/ticket-bets",
        params={"ticket_number": ticket["ticket_number"]},
        json={"draw_id": draw.id, "picks": [1, 2, 3], "stake": 10},
        headers=_agent_headers(agent.id),
    )

    assert response.status_code == 400
    assert "inactif" in response.json()["detail"]


