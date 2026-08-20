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


def _install_fake_game_counts(db_session, keno_count: int = 0, lucky_count: int = 0) -> None:
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
        if "lucky_plays" in compiled:
            result = MagicMock()
            result.scalar.return_value = lucky_count
            return result
        return await original_execute(statement, *args, **kwargs)

    db_session.execute = patched_execute


@pytest.mark.asyncio
async def test_dashboard_stats_api_requires_login(admin_client):
    response = await admin_client.get("/admin/api/dashboard/stats")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_stats_api_returns_flat_dotted_keys(
    admin_client, admin_user, db_session, fake_redis
):
    """Le JS lit `data['users.total']` (clé littérale avec un point), pas
    `data.users.total` : la réponse doit donc être aplatie, pas imbriquée."""
    await _login(admin_client)
    _install_fake_game_counts(db_session)  # _get_dashboard_stats() compte aussi les paris Keno du jour
    await WalletService(db_session, fake_redis).credit(admin_user.id, Decimal("500"), "WIN")

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
    assert data["transactions.total_wins"] == 500.0


@pytest.mark.asyncio
async def test_dashboard_charts_api_returns_expected_shape(
    admin_client, admin_user, db_session, fake_redis
):
    await _login(admin_client)
    _install_fake_game_counts(db_session, keno_count=3, lucky_count=5)
    await WalletService(db_session, fake_redis).credit(admin_user.id, Decimal("200"), "DEPOSIT")

    response = await admin_client.get("/admin/api/dashboard/charts?period=7")

    assert response.status_code == 200
    data = response.json()
    assert len(data["transactions"]["labels"]) == 7
    assert len(data["transactions"]["deposits"]) == 7
    assert len(data["transactions"]["withdrawals"]) == 7
    assert len(data["transactions"]["wins"]) == 7
    assert sum(data["transactions"]["deposits"]) == 200.0
    assert data["games"] == {"keno": 3, "lucky": 5}


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
