# tests/test_api/test_session_expired.py
"""Session expirée dans les panels : redirection vers la connexion au lieu du JSON brut."""

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.session_expired import MESSAGE, panel_for, session_expired_handler

HTML = {"accept": "text/html,application/xhtml+xml"}
EXPIRED = {"admin_token": "vieux", "admin_refresh": "vieux", "agent_token": "vieux", "agent_refresh": "vieux"}


def _expired():
    raise HTTPException(status_code=401, detail="Token invalide ou expiré")


@pytest_asyncio.fixture
async def client():
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, session_expired_handler)
    for path in ("/admin/dashboard", "/admin/api/users", "/agent/dashboard", "/agent/api/keno/live", "/api/v1/wallet/balance"):
        app.add_api_route(path, lambda: _expired(), methods=["GET", "POST"])
    app.add_api_route("/admin/forbidden", lambda: (_ for _ in ()).throw(HTTPException(403, "Accès réservé")), methods=["GET"])
    app.add_api_route("/admin/login", lambda: _expired(), methods=["GET"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", cookies=EXPIRED) as c:
        yield c


def test_panel_detection():
    assert panel_for("/admin/dashboard")[0] == "/admin/login"
    assert panel_for("/agent/api/keno/live")[0] == "/agent/login"
    assert panel_for("/agent") [0] == "/agent/login"
    assert panel_for("/admin/login") is None and panel_for("/agent/login") is None
    assert panel_for("/api/v1/wallet") is None and panel_for("/administration") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("path,login", [("/admin/dashboard", "/admin/login"), ("/agent/dashboard", "/agent/login")])
async def test_page_with_expired_token_redirects_to_login(client, path, login):
    response = await client.get(path, headers=HTML, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith(login + "?error=")
    cookies = " ".join(response.headers.get_list("set-cookie"))
    assert "admin_token=" in cookies or "agent_token=" in cookies  # cookies périmés effacés
    assert "Max-Age=0" in cookies or "expires=" in cookies.lower()


@pytest.mark.asyncio
async def test_api_call_gets_json_with_login_address(client):
    response = await client.get("/agent/api/keno/live", headers={"accept": "application/json"})
    assert response.status_code == 401
    body = response.json()
    assert body["session_expired"] is True and body["login_url"] == "/agent/login"
    assert body["detail"] == "Token invalide ou expiré"
    assert "agent_token=" in " ".join(response.headers.get_list("set-cookie"))
    # une page qui appelle /api/ en navigation reste une réponse JSON (pas de redirection)
    page_like = await client.get("/admin/api/users", headers=HTML, follow_redirects=False)
    assert page_like.status_code == 401 and page_like.json()["login_url"] == "/admin/login"
    posted = await client.post("/admin/dashboard", headers=HTML, follow_redirects=False)
    assert posted.status_code == 401  # seules les navigations GET sont redirigées


@pytest.mark.asyncio
async def test_other_errors_and_public_api_are_untouched(client):
    forbidden = await client.get("/admin/forbidden", headers=HTML, follow_redirects=False)
    assert forbidden.status_code == 403 and forbidden.json() == {"detail": "Accès réservé"}
    public = await client.get("/api/v1/wallet/balance", headers=HTML, follow_redirects=False)
    assert public.status_code == 401 and public.json() == {"detail": "Token invalide ou expiré"}
    assert "set-cookie" not in public.headers
    login = await client.get("/admin/login", headers=HTML, follow_redirects=False)
    assert login.status_code == 401  # la page de connexion ne se redirige pas elle-même (pas de boucle)


def test_message_is_french():
    assert "expiré" in MESSAGE
