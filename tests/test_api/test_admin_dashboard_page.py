# tests/test_api/test_admin_dashboard_page.py
"""Régression : après la connexion, /admin/dashboard doit afficher la page
du tableau de bord, pas une réponse JSON brute."""

from decimal import Decimal

import pytest

from app.services.wallet_service import WalletService
from tests.test_api.test_admin_pages_smoke import _install_fake_keno_counts, _login


@pytest.mark.asyncio
async def test_dashboard_is_an_html_page_with_real_figures(admin_client, admin_user, db_session, fake_redis, make_user):
    player = await make_user()
    await WalletService(db_session, fake_redis).credit(player.id, Decimal("750"), "DEPOSIT")
    await db_session.flush()

    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    response = await admin_client.get("/admin/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Tableau de bord" in response.text
    assert "/static/img/logo-64.png" in response.text      # logo dans la barre latérale
    assert "Dépôt" in response.text                         # dernière transaction, type reconnu
    assert "750 HTG" in response.text                       # volume du jour réellement calculé
    assert '"users"' not in response.text[:200]             # plus de JSON brut


@pytest.mark.asyncio
async def test_dashboard_summary_json_is_still_available(admin_client, admin_user, db_session):
    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    response = await admin_client.get("/admin/api/dashboard/summary")
    assert response.status_code == 200
    assert set(response.json()) == {"users", "finance", "games", "tickets"}


@pytest.mark.asyncio
async def test_dashboard_still_renders_if_horse_races_migration_not_applied(admin_client, admin_user, db_session):
    """Avant `alembic upgrade head`, la table game_bets n'existe pas : le
    tableau de bord doit quand même s'afficher (comptage Horse Races ignoré)."""
    from sqlalchemy import text

    await db_session.execute(text("DROP TABLE game_bets"))
    await db_session.commit()

    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    response = await admin_client.get("/admin/dashboard")
    assert response.status_code == 200, response.text[:500]
    assert "Tableau de bord" in response.text
