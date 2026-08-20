# tests/test_api/test_admin_login.py
"""Test de bout en bout du flux de connexion admin (app/routes/admin.py),
en particulier le branchement CSRF : ce mécanisme était configuré mais
jamais réellement invoqué (pas de handler d'erreur, pas de champ caché
dans le formulaire, mauvais `token_location` par défaut de la lib pour un
formulaire HTML classique). On vérifie ici que la protection est bien
active (un jeton absent/invalide est rejeté) et qu'elle n'empêche pas une
connexion légitime.

Le routeur admin est monté seul dans une app FastAPI minimale (sans le
lifespan de app.main, qui exige un vrai Postgres/Redis) ; get_db/get_redis
sont substitués par la session SQLite et le faux Redis de conftest.py.
"""

import re

import pytest

from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

# admin_app / admin_client / admin_user sont définies dans tests/conftest.py
# (partagées avec test_admin_dashboard.py).


def _extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]*)"', html)
    assert match, "champ csrf_token introuvable dans la page de connexion"
    return match.group(1)


@pytest.mark.asyncio
async def test_login_page_sets_csrf_cookie_and_field(admin_client):
    response = await admin_client.get("/admin/login")

    assert response.status_code == 200
    assert "fastapi-csrf-token" in response.cookies
    assert _extract_csrf_token(response.text)


@pytest.mark.asyncio
async def test_login_without_csrf_token_is_rejected(admin_client, admin_user):
    # On récupère le cookie CSRF mais on n'envoie PAS le champ de formulaire.
    await admin_client.get("/admin/login")

    response = await admin_client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        follow_redirects=True,
    )

    # Rejeté par AdminCsrfMiddleware avant même d'atteindre la route :
    # redirection 303 -> GET /admin/login?csrf_error=1, suivie ici.
    assert response.status_code == 200
    assert "admin_token" not in response.cookies
    assert "Session expir" in response.text


@pytest.mark.asyncio
async def test_login_with_tampered_csrf_token_is_rejected(admin_client, admin_user):
    await admin_client.get("/admin/login")

    response = await admin_client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": "invalide"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "admin_token" not in response.cookies
    assert "Session expir" in response.text


@pytest.mark.asyncio
async def test_login_with_valid_csrf_token_succeeds(admin_client, admin_user):
    login_page = await admin_client.get("/admin/login")
    csrf_token = _extract_csrf_token(login_page.text)

    response = await admin_client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/dashboard"
    assert "admin_token" in response.cookies
    assert "admin_refresh" in response.cookies


@pytest.mark.asyncio
async def test_login_wrong_password_with_valid_csrf_reshows_form(admin_client, admin_user):
    login_page = await admin_client.get("/admin/login")
    csrf_token = _extract_csrf_token(login_page.text)

    response = await admin_client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": "WrongPassword", "csrf_token": csrf_token},
    )

    assert response.status_code == 200
    assert "admin_token" not in response.cookies
    assert "incorrect" in response.text


# ========== Généralisation à tout le panel /admin ==========
# AdminCsrfMiddleware s'applique par préfixe d'URL, pas route par route :
# ces tests visent /admin/_test-protected (déclarée dans la fixture
# admin_app ci-dessus) pour prouver que la protection couvre bien
# n'importe quelle route mutante du panel, pas seulement /admin/login.

@pytest.mark.asyncio
async def test_any_admin_post_route_without_csrf_token_is_rejected(admin_client):
    await admin_client.get("/admin/login")  # pose le cookie CSRF

    response = await admin_client.post("/admin/_test-protected", follow_redirects=False)

    assert response.status_code == 303
    assert "csrf_error=1" in response.headers["location"]


@pytest.mark.asyncio
async def test_any_admin_post_route_with_valid_csrf_token_succeeds(admin_client):
    login_page = await admin_client.get("/admin/login")
    csrf_token = _extract_csrf_token(login_page.text)

    response = await admin_client.post(
        "/admin/_test-protected", data={"csrf_token": csrf_token}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_any_admin_post_route_with_valid_csrf_header_succeeds(admin_client):
    # C'est le chemin réellement emprunté par la quasi-totalité du panel
    # admin : les templates soumettent leurs actions en fetch() avec un
    # header 'X-CSRFToken', pas un champ de formulaire (cf. app/templates
    # /admin/**/*.html, ex. bureaus/create.html, users/index.html...).
    login_page = await admin_client.get("/admin/login")
    csrf_token = _extract_csrf_token(login_page.text)

    response = await admin_client.post(
        "/admin/_test-protected",
        json={"some": "payload"},
        headers={"X-CSRFToken": csrf_token},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_any_admin_post_route_with_wrong_csrf_header_is_rejected(admin_client):
    await admin_client.get("/admin/login")

    response = await admin_client.post(
        "/admin/_test-protected",
        json={"some": "payload"},
        headers={"X-CSRFToken": "invalide"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "csrf_error=1" in response.headers["location"]


@pytest.mark.asyncio
async def test_admin_get_route_is_never_csrf_blocked(admin_client):
    # Les méthodes sûres (GET/HEAD/OPTIONS) ne sont jamais validées : sinon
    # la toute première visite (sans cookie CSRF existant) serait bloquée.
    response = await admin_client.get("/admin/login")
    assert response.status_code == 200
