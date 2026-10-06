# tests/test_api/test_admin_dashboard.py
"""Tests des endpoints JSON consommés par le JS de
app/templates/admin/dashboard.html (auto-refresh des statistiques et
graphiques) : GET /admin/api/dashboard/stats et /admin/api/dashboard/charts.
Ces deux routes n'existaient pas du tout avant (404 systématique)."""

import re
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.wallet_service import WalletService
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


async def _login(admin_client) -> None:
    login_page = await admin_client.get("/admin/login")
    match = re.search(r'name="csrf_token"\s+value="([^"]*)"', login_page.text)
    assert match, "champ csrf_token introuvable"

    response = await admin_client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": match.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303, "échec de connexion admin dans le fixture de test"


def _install_fake_game_counts(db_session, keno_count: int = 0) -> None:
    """KenoBet utilise des colonnes ARRAY (Postgres-only) et ne peut pas être
    créée sur la base SQLite de test (cf. tests/conftest.py) : on intercepte
    juste ses requêtes de comptage. Le reste (Transaction) passe par la
    vraie base."""
    original_execute = db_session.execute

    async def patched_execute(statement, *args, **kwargs):
        compiled = str(statement)
        if "keno_bets" in compiled:
            result = MagicMock()
            result.scalar.return_value = keno_count
            return result
        return await original_execute(statement, *args, **kwargs)

    db_session.execute = patched_execute


@pytest.mark.asyncio
async def test_dashboard_stats_api_requires_login(admin_client):
    response = await admin_client.get("/admin/api/dashboard/stats")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_stats_api_returns_flat_dotted_keys(
    admin_client, admin_user, db_session, fake_redis, make_agent, make_ticket
):
    """Le JS lit `data['users.total']` (clé littérale avec un point), pas
    `data.users.total` : la réponse doit donc être aplatie, pas imbriquée.

    Gains des joueurs = gains des paris réglés de TOUS les jeux, joués avec un
    compte (500) OU un ticket au guichet (300) : les transactions WIN seules
    (comptes) en oubliaient la plus grande partie."""
    from sqlalchemy import select

    from app.core.timezone import now_utc
    from app.models.enums import TicketStatus
    from app.models.game import GameBet
    from app.models.ticket import Ticket
    from app.services.lucky6_service import Lucky6Service

    await _login(admin_client)
    _install_fake_game_counts(db_session)  # _get_dashboard_stats() compte aussi les paris Keno du jour
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("50"))
    ticket_id = (await db_session.execute(select(Ticket.id).where(Ticket.ticket_number == ticket["ticket_number"]))).scalar()
    race = await Lucky6Service(db_session, fake_redis).create_race()
    for funding, win in (({"user_id": admin_user.id}, "500"), ({"ticket_id": ticket_id}, "300")):
        db_session.add(GameBet(round_id=race.id, game_type="lucky6", agent_id=agent.id, bet_type="SIX", selection=[1, 2, 3, 4, 5, 6],
                               stake=Decimal("50"), odds=Decimal("6"), potential_payout=Decimal(win), status="WON",
                               winnings=Decimal(win), placed_at=now_utc(), **funding))
    # gain du ticket encaissé au guichet -> compté comme payé (un gain encore dû ne l'est pas)
    (await db_session.get(Ticket, ticket_id)).status = TicketStatus.PAID
    await db_session.flush()

    response = await admin_client.get("/admin/api/dashboard/stats")

    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {
        "users.total",
        "transactions.total_volume",
        "transactions.total_wins",
        "games.today_bets",
    }
    assert data["users.total"] >= 1
    # gains PAYÉS : le ticket encaissé (300) ; le gain crédité sur le compte (500)
    # reste dû tant que le joueur ne l'a pas retiré
    assert data["transactions.total_wins"] == 300.0


@pytest.mark.asyncio
async def test_dashboard_charts_api_returns_expected_shape(
    admin_client, admin_user, db_session, fake_redis
):
    await _login(admin_client)
    _install_fake_game_counts(db_session, keno_count=3)
    await WalletService(db_session, fake_redis).credit(admin_user.id, Decimal("200"), "DEPOSIT")

    response = await admin_client.get("/admin/api/dashboard/charts?period=7")

    assert response.status_code == 200
    data = response.json()
    assert len(data["transactions"]["labels"]) == 7
    assert len(data["transactions"]["deposits"]) == 7
    assert len(data["transactions"]["withdrawals"]) == 7
    assert len(data["transactions"]["wins"]) == 7
    assert sum(data["transactions"]["deposits"]) == 200.0
    assert data["games"] == {"keno": 3, "lucky6": 0, "horse_races": 0}  # Lucky Wheel retirée


@pytest.mark.asyncio
async def test_dashboard_charts_api_default_period_is_30_days(admin_client, admin_user, db_session):
    await _login(admin_client)
    _install_fake_game_counts(db_session)

    response = await admin_client.get("/admin/api/dashboard/charts")

    assert response.status_code == 200
    assert len(response.json()["transactions"]["labels"]) == 30


@pytest.mark.asyncio
async def test_dashboard_charts_api_requires_login(admin_client):
    response = await admin_client.get("/admin/api/dashboard/charts")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_chart_wins_include_ticket_winnings(admin_client, admin_user, db_session, fake_redis, make_agent, make_ticket):
    """La courbe « Gains » compte aussi les gains des tickets joués au guichet."""
    from sqlalchemy import select

    from app.core.timezone import now_utc
    from app.models.game import GameBet
    from app.models.ticket import Ticket
    from app.services.lucky6_service import Lucky6Service

    await _login(admin_client)
    _install_fake_game_counts(db_session)
    agent = await make_agent()
    ticket = await make_ticket(agent, balance=Decimal("50"))
    ticket_id = (await db_session.execute(select(Ticket.id).where(Ticket.ticket_number == ticket["ticket_number"]))).scalar()
    race = await Lucky6Service(db_session, fake_redis).create_race()
    db_session.add(GameBet(round_id=race.id, game_type="lucky6", ticket_id=ticket_id, agent_id=agent.id, bet_type="SIX",
                           selection=[1, 2, 3, 4, 5, 6], stake=Decimal("50"), odds=Decimal("6"), potential_payout=Decimal("300"),
                           status="WON", winnings=Decimal("300"), placed_at=now_utc()))
    await db_session.flush()

    data = (await admin_client.get("/admin/api/dashboard/charts?period=7")).json()
    assert sum(data["transactions"]["wins"]) == 0.0  # gain encore dû : pas encore payé
    from app.models.enums import TicketStatus

    (await db_session.get(Ticket, ticket_id)).status = TicketStatus.PAID
    await db_session.flush()
    data = (await admin_client.get("/admin/api/dashboard/charts?period=7")).json()
    assert data["transactions"]["wins"][-1] == 300.0 and sum(data["transactions"]["wins"]) == 300.0
