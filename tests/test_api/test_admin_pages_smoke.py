# tests/test_api/test_admin_pages_smoke.py
"""Vérifie que chaque page HTML admin s'affiche (200) sans planter.

Cette suite a été ajoutée pendant l'audit des templates admin, qui a mis en
évidence plusieurs bugs qui cassaient le rendu de (quasiment) toutes les
pages :
  - `templates.TemplateResponse("nom.html", {...})` (ancienne signature à 2
    arguments) n'est plus supporté par la version de Starlette installée
    (1.3.1+, cf. app/routes/admin.py) : le nom du template et le contexte
    sont alors interprétés comme (request, name), et `env.get_template()`
    reçoit un dict à la place d'une chaîne -> TypeError plus loin dans le
    cache Jinja. Seules les pages utilisant la forme à 3 arguments
    (request, name, context) fonctionnaient.
  - `url_for('admin_x', page=2, **request.args)` : `request` (Starlette) n'a
    pas d'attribut `.args` (c'est l'API Flask) -> UndefinedError dès qu'un
    template tentait de construire un lien de pagination.
  - plusieurs `url_for(...)` pointaient vers des noms de route qui n'ont
    jamais existé (agents/bureaux/promotions détail·édition, paramètres,
    statistiques des jeux) : ces pages existaient en template mais n'étaient
    reliées à aucune route - elles ont depuis été implémentées (cf. classe de
    tests ci-dessous) et les url_for() corrigés pour pointer dessus.
  - `stats.xxx`/`filters.xxx` référencés par les templates de liste alors que
    la route ne les passait pas du tout au contexte -> UndefinedError dès la
    première carte de statistiques (Jinja lève une erreur sur un accès
    d'attribut sur une variable de premier niveau non définie, contrairement
    à une simple impression `{{ x }}` qui elle est silencieuse).
"""

import re
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.models.bureau import Bureau
from app.models.enums import PromotionStatus, PromotionType, TransactionStatus, TransactionType, UserRole
from app.models.promotion import Promotion
from app.models.ticket import Ticket
from app.models.transaction import Transaction
from app.models.wallet import Wallet
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


class _ZeroRow:
    """Ligne factice dont tout attribut numérique vaut 0 (utilisée pour les
    `.one()` sur des agrégats multi-colonnes qu'on ne peut pas exécuter en
    SQLite - cf. _install_fake_keno_counts)."""

    def __getattr__(self, name):
        return 0


def _install_fake_keno_counts(db_session) -> None:
    """KenoBet/KenoDraw utilisent des colonnes ARRAY (Postgres-only) et ne
    peuvent pas être créées sur la base SQLite de test (cf.
    tests/conftest.py) : on intercepte leurs requêtes en renvoyant un
    résultat vide/zéro quelle que soit sa forme (.scalar(), .scalars().all(),
    .all(), .one()). Le reste (Transaction, User, ...) passe par la vraie
    base. Même principe que test_admin_dashboard.py::_install_fake_game_counts,
    étendu aux agrégats multi-colonnes utilisés par les pages de
    statistiques/tirages Keno."""
    original_execute = db_session.execute

    async def patched_execute(statement, *args, **kwargs):
        compiled = str(statement)
        if "keno_bets" in compiled or "keno_draws" in compiled:
            result = MagicMock()
            result.scalar.return_value = 0
            result.scalar_one_or_none.return_value = None
            result.one.return_value = _ZeroRow()
            result.all.return_value = []
            result.scalars.return_value.all.return_value = []
            return result
        return await original_execute(statement, *args, **kwargs)

    db_session.execute = patched_execute


# Pages avec une route HTML réellement enregistrée et dont toutes les
# dépendances (tables) sont portables sur la base SQLite de test.
@pytest.mark.parametrize(
    "path",
    [
        "/admin/users",
        "/admin/agents",
        "/admin/bureaus",
        "/admin/games/lucky/config",
        "/admin/transactions",
        "/admin/tickets",
        "/admin/audit/logs",
        "/admin/promotions",
    ],
)
@pytest.mark.asyncio
async def test_admin_list_pages_render(admin_client, admin_user, path):
    await _login(admin_client)
    response = await admin_client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"


@pytest.mark.parametrize("path", ["/admin/dashboard", "/admin/games/keno/config"])
@pytest.mark.asyncio
async def test_admin_pages_depending_on_keno_tables_render(admin_client, admin_user, db_session, path):
    """Mêmes pages que ci-dessus, mais dépendent de KenoBet/KenoDraw
    (colonnes ARRAY Postgres-only) : comptages simulés à zéro."""
    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    response = await admin_client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"


@pytest.mark.asyncio
async def test_admin_pagination_second_page_does_not_crash(admin_client, admin_user):
    """Avant la correction, url_for(name, page=2, **request.args) plantait
    dès que la query string contenait déjà `page` (TypeError: multiple
    values), et `request.args` seul plantait tout le temps (AttributeError
    via Jinja Undefined) - reproductible dès qu'un filtre est présent."""
    await _login(admin_client)
    response = await admin_client.get("/admin/users?page=2&search=test")
    assert response.status_code == 200, response.text[:2000]


@pytest.mark.asyncio
async def test_admin_settings_link_does_not_crash_sidebar(admin_client, admin_user):
    """Le lien 'Paramètres' de la sidebar (présente sur CHAQUE page admin,
    via admin/base.html) pointait vers url_for('admin_settings') alors
    qu'aucune route de ce nom n'existe : Jinja levait NoMatchFound sur
    absolument toutes les pages admin. /admin/settings existe maintenant
    (redirige vers /admin/settings/general)."""
    await _login(admin_client)
    response = await admin_client.get("/admin/users")
    assert response.status_code == 200
    assert "/admin/settings" in response.text


@pytest.mark.asyncio
async def test_admin_settings_redirects_to_general(admin_client, admin_user):
    await _login(admin_client)
    response = await admin_client.get("/admin/settings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/admin/settings/general")


@pytest.mark.parametrize("path", ["/admin/settings/general", "/admin/settings/security"])
@pytest.mark.asyncio
async def test_admin_settings_pages_render(admin_client, admin_user, path):
    await _login(admin_client)
    response = await admin_client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"


@pytest.mark.parametrize("path", ["/admin/agents/create", "/admin/bureaus/create", "/admin/promotions/create"])
@pytest.mark.asyncio
async def test_admin_create_pages_render(admin_client, admin_user, path):
    await _login(admin_client)
    response = await admin_client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"


@pytest.mark.asyncio
async def test_admin_agent_detail_edit_and_sessions_pages_render(admin_client, admin_user, db_session):
    from app.models.user import User

    agent = User(
        phone="40011111",
        password_hash="x",
        first_name="Jean",
        last_name="Baptiste",
        role=UserRole.AGENT,
        is_active=True,
    )
    db_session.add(agent)
    await db_session.flush()

    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    for path in (
        f"/admin/agents/{agent.id}",
        f"/admin/agents/{agent.id}/edit",
        f"/admin/agents/{agent.id}/sessions",
    ):
        response = await admin_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"


@pytest.mark.asyncio
async def test_admin_bureau_detail_and_edit_pages_render(admin_client, admin_user, db_session):
    bureau = Bureau(name="Bureau Test", code="TEST01", city="Port-au-Prince")
    db_session.add(bureau)
    await db_session.flush()

    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    for path in (f"/admin/bureaus/{bureau.id}", f"/admin/bureaus/{bureau.id}/edit"):
        response = await admin_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"


@pytest.mark.asyncio
async def test_admin_promotion_detail_and_edit_pages_render(admin_client, admin_user, db_session):
    promotion = Promotion(
        name="Bonus test",
        type=PromotionType.DEPOSIT_BONUS,
        config={"bonus_percent": 100, "min_deposit": 100},
        start_date=datetime.utcnow(),
        end_date=datetime.utcnow() + timedelta(days=30),
        status=PromotionStatus.ACTIVE,
        created_by=admin_user.id,
    )
    db_session.add(promotion)
    await db_session.flush()

    await _login(admin_client)
    for path in (f"/admin/promotions/{promotion.id}", f"/admin/promotions/{promotion.id}/edit"):
        response = await admin_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"


@pytest.mark.asyncio
async def test_admin_ticket_detail_page_renders(admin_client, admin_user, db_session):
    bureau = Bureau(name="Bureau Ticket", code="TICKET1", city="Delmas")
    db_session.add(bureau)
    await db_session.flush()

    ticket = Ticket(
        bureau_id=bureau.id,
        ticket_number="TK-0001",
        initial_amount=Decimal("500"),
        balance=Decimal("500"),
        expires_at=datetime.utcnow() + timedelta(days=7),
    )
    db_session.add(ticket)
    await db_session.flush()

    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    response = await admin_client.get(f"/admin/tickets/{ticket.id}")
    assert response.status_code == 200, response.text[:2000]


@pytest.mark.asyncio
async def test_admin_transaction_detail_page_renders(admin_client, admin_user, make_user, db_session):
    player = await make_user(balance=Decimal("1000"))
    wallet_result = await db_session.execute(Wallet.__table__.select().where(Wallet.__table__.c.user_id == player.id))
    wallet_row = wallet_result.first()

    transaction = Transaction(
        user_id=player.id,
        wallet_id=wallet_row.id,
        reference="TX-TEST-0001",
        transaction_type=TransactionType.DEPOSIT,
        amount=Decimal("1000"),
        balance_before=Decimal("0"),
        balance_after=Decimal("1000"),
        status=TransactionStatus.COMPLETED,
    )
    db_session.add(transaction)
    await db_session.flush()

    await _login(admin_client)
    response = await admin_client.get(f"/admin/transactions/{transaction.id}")
    assert response.status_code == 200, response.text[:2000]


@pytest.mark.asyncio
async def test_admin_user_kyc_page_renders(admin_client, admin_user):
    await _login(admin_client)
    response = await admin_client.get(f"/admin/users/{admin_user.id}/kyc")
    assert response.status_code == 200, response.text[:2000]


@pytest.mark.parametrize("path", ["/admin/games/keno/draws", "/admin/games/keno/statistics"])
@pytest.mark.asyncio
async def test_admin_keno_draws_and_statistics_pages_render(admin_client, admin_user, db_session, path):
    """Dépendent de KenoDraw/KenoBet (ARRAY Postgres-only, cf. TEST_TABLES)."""
    await _login(admin_client)
    _install_fake_keno_counts(db_session)
    response = await admin_client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}\n{response.text[:2000]}"



# /admin/games/lucky/statistics et /admin/reports/financial utilisent
# func.date_trunc('day', ...) pour l'évolution quotidienne : cette fonction
# est Postgres-only (aucun équivalent portable simple côté SQLite), donc
# intestable sous la base SQLite de test - fonctionne en production
# (Postgres, cf. docker-compose.yml). Vérifié par lecture de code, pas par
# un test automatisé ici.
