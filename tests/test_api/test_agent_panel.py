# tests/test_api/test_agent_panel.py
"""Tests du panel HTML agent (app/routes/agent.py), jusqu'ici entièrement
orphelin : les 8 templates de app/templates/agent/ existaient sans aucune
route pour les rendre, et les endpoints JSON qu'ils appellent (ouverture de
caisse, encaissement/paiement ticket ou compte, création de ticket) n'
existaient pas non plus sous cette forme unifiée. Couvre le parcours cash
critique de bout en bout : connexion agent -> ouverture de caisse ->
encaissement (ticket et compte) -> paiement (ticket et compte), plus un
rendu basique de chaque page HTML pour détecter toute régression de type
TemplateNotFound (cf. le bug de racine Jinja corrigé sur app/routes/admin.py
la même session)."""

import re
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.models.transaction import Transaction
from app.services.wallet_service import WalletService


@pytest.fixture(autouse=True)
def _stub_keno_tables(db_session, monkeypatch):
    """KenoDraw/KenoBet utilisent des colonnes ARRAY (Postgres-only, non
    créables sur la base SQLite de test, cf. TEST_TABLES dans conftest.py).
    Le dashboard, l'historique, les rapports, le profil et la page Keno du
    panel agent interrogent ces tables (paris du jour, prochain tirage) :
    on intercepte ces requêtes précises pour renvoyer un résultat vide
    plutôt que de lever OperationalError, afin de pouvoir quand même
    exercer le reste de chaque route (qui ne dépend pas de ces données pour
    les assertions des tests ci-dessous)."""
    original_execute = db_session.execute

    async def _patched_execute(statement, *args, **kwargs):
        sql = str(statement)
        if "keno_draws" in sql or "keno_bets" in sql:
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            result.scalar.return_value = 0
            result.scalars.return_value.all.return_value = []
            return result
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", _patched_execute)


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]*)"', html)
    assert match, "champ csrf_token introuvable"
    return match.group(1)


async def _login(agent_client, agent) -> None:
    login_page = await agent_client.get("/agent/login")
    response = await agent_client.post(
        "/agent/login",
        data={
            "code": agent.phone,
            "password": "AgentPass123!",
            "csrf_token": _extract_csrf_token(login_page.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


async def _fresh_csrf_headers(agent_client) -> dict:
    """Le cookie CSRF est régénéré à chaque réponse (AdminCsrfMiddleware,
    désormais étendue à /agent) : il faut un jeton frais avant chaque appel
    fetch(), comme le ferait un vrai navigateur."""
    page = await agent_client.get("/agent/login")
    return {"X-CSRFToken": _extract_csrf_token(page.text)}


@pytest.mark.asyncio
async def test_agent_login_requires_agent_role(agent_client, make_user):
    """Un joueur normal ne doit pas pouvoir se connecter au panel agent."""
    player = await make_user(password="Secret123!")

    login_page = await agent_client.get("/agent/login")
    response = await agent_client.post(
        "/agent/login",
        data={
            "code": player.phone,
            "password": "Secret123!",
            "csrf_token": _extract_csrf_token(login_page.text),
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "réservé aux agents" in response.text


@pytest.mark.asyncio
async def test_agent_login_then_dashboard_renders(agent_client, make_agent):
    agent = await make_agent()
    await _login(agent_client, agent)

    response = await agent_client.get("/agent/dashboard")
    assert response.status_code == 200
    assert "Tableau de bord" in response.text


@pytest.mark.asyncio
async def test_open_session_then_cashier_page_renders(agent_client, make_agent):
    agent = await make_agent()
    await _login(agent_client, agent)

    response = await agent_client.post(
        "/agent/api/cashier/open",
        json={"starting_balance": 1000},
        headers=await _fresh_csrf_headers(agent_client),
    )
    assert response.status_code == 200
    assert response.json()["success"] is True

    page = await agent_client.get("/agent/cashier")
    assert page.status_code == 200
    assert "Ouverte" in page.text


@pytest.mark.asyncio
async def test_cashier_deposit_creates_ticket_and_updates_session(agent_client, make_agent):
    agent = await make_agent()
    await _login(agent_client, agent)
    await agent_client.post(
        "/agent/api/cashier/open", json={"starting_balance": 0}, headers=await _fresh_csrf_headers(agent_client)
    )

    response = await agent_client.post(
        "/agent/api/cashier/deposit",
        json={"player_type": "ticket", "amount": 250, "player_name": "Joueur Test"},
        headers=await _fresh_csrf_headers(agent_client),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    ticket_number = data["data"]["ticket_number"]
    assert ticket_number

    balance = await agent_client.get("/agent/api/cash-balance")
    assert balance.json()["balance"] == 250.0

    return ticket_number


@pytest.mark.asyncio
async def test_cashier_ticket_full_payout_via_unified_endpoint(agent_client, make_agent):
    agent = await make_agent()
    await _login(agent_client, agent)
    await agent_client.post(
        "/agent/api/cashier/open", json={"starting_balance": 0}, headers=await _fresh_csrf_headers(agent_client)
    )

    created = await agent_client.post(
        "/agent/api/cashier/deposit",
        json={"player_type": "ticket", "amount": 300, "player_name": "Joueur Ticket"},
        headers=await _fresh_csrf_headers(agent_client),
    )
    ticket_number = created.json()["data"]["ticket_number"]

    response = await agent_client.post(
        "/agent/api/cashier/payout",
        json={"payout_type": "ticket", "identifier": ticket_number},
        headers=await _fresh_csrf_headers(agent_client),
    )

    assert response.status_code == 200
    assert response.json()["success"] is True

    balance = await agent_client.get("/agent/api/cash-balance")
    # +300 à l'encaissement puis -300 au paiement -> caisse revenue à 0.
    assert balance.json()["balance"] == 0.0


@pytest.mark.asyncio
async def test_cashier_deposit_and_payout_to_player_account(agent_client, make_agent, make_user, db_session, fake_redis):
    agent = await make_agent()
    player = await make_user()
    await _login(agent_client, agent)
    await agent_client.post(
        "/agent/api/cashier/open", json={"starting_balance": 0}, headers=await _fresh_csrf_headers(agent_client)
    )

    deposit_response = await agent_client.post(
        "/agent/api/cashier/deposit",
        json={"player_type": "account", "phone": player.phone, "amount": 500},
        headers=await _fresh_csrf_headers(agent_client),
    )
    assert deposit_response.status_code == 200
    assert deposit_response.json()["success"] is True

    wallet_service = WalletService(db_session, fake_redis)
    assert await wallet_service.get_balance(player.id) == Decimal("500")

    # La transaction cash doit être attribuée à l'agent qui l'a traitée
    # (Transaction.created_by), pour l'historique/les rapports de l'agent.
    tx = (await db_session.execute(select(Transaction).where(Transaction.user_id == player.id))).scalar_one()
    assert tx.created_by == agent.id

    payout_response = await agent_client.post(
        "/agent/api/cashier/payout",
        json={"payout_type": "account", "identifier": player.phone, "amount": 200},
        headers=await _fresh_csrf_headers(agent_client),
    )
    assert payout_response.status_code == 200
    assert await wallet_service.get_balance(player.id) == Decimal("300")


@pytest.mark.asyncio
async def test_cashier_payout_without_open_session_rejected(agent_client, make_agent):
    agent = await make_agent()
    await _login(agent_client, agent)

    response = await agent_client.post(
        "/agent/api/cashier/payout",
        json={"payout_type": "account", "identifier": "40000001"},
        headers=await _fresh_csrf_headers(agent_client),
    )

    # L'app de test (agent_app) ne monte pas le handler global AppException
    # de app.main : seul le code de statut est garanti ici, comme pour les
    # tests admin équivalents (cf. test_admin_transactions.py).
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_create_ticket_and_view_and_qr(agent_client, make_agent):
    agent = await make_agent()
    await _login(agent_client, agent)
    await agent_client.post(
        "/agent/api/cashier/open", json={"starting_balance": 0}, headers=await _fresh_csrf_headers(agent_client)
    )

    create_response = await agent_client.post(
        "/agent/api/tickets",
        json={"amount": 150, "player_name": "Joueur QR", "player_phone": None},
        headers=await _fresh_csrf_headers(agent_client),
    )
    assert create_response.status_code == 200
    ticket_id = create_response.json()["data"]["id"]
    ticket_number = create_response.json()["data"]["ticket_number"]

    detail = await agent_client.get(f"/agent/api/tickets/{ticket_id}")
    assert detail.status_code == 200
    assert detail.json()["number"] == ticket_number
    assert detail.json()["status"] == "active"

    qr = await agent_client.get(f"/agent/api/tickets/{ticket_number}/qr")
    assert qr.status_code == 200
    assert qr.json()["success"] is True
    assert qr.json()["qr_code"].startswith("data:image/png;base64,")

    page = await agent_client.get("/agent/tickets")
    assert page.status_code == 200
    assert ticket_number in page.text


@pytest.mark.parametrize("path", ["/agent/history", "/agent/reports", "/agent/profile", "/agent/keno", "/agent/lucky"])
@pytest.mark.asyncio
async def test_agent_pages_render_without_error(agent_client, make_agent, path):
    """Détecte toute régression de type TemplateNotFound/erreur de contexte
    sur chacune des pages du panel agent (toutes orphelines avant cette
    session : aucune route ne les rendait)."""
    agent = await make_agent()
    await _login(agent_client, agent)

    response = await agent_client.get(path)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_agent_logout_clears_cookie(agent_client, make_agent):
    agent = await make_agent()
    await _login(agent_client, agent)

    response = await agent_client.post(
        "/agent/logout", headers=await _fresh_csrf_headers(agent_client), follow_redirects=False
    )
    assert response.status_code == 303

    dashboard = await agent_client.get("/agent/dashboard", follow_redirects=False)
    assert dashboard.status_code in (401, 303)
