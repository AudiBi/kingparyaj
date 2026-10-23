# app/core/session_expired.py
"""Session expirée dans les panels /admin et /agent : déconnexion automatique.

Quand le jeton (cookie) est invalide, expiré ou révoqué, l'API répond 401. Sans traitement,
le navigateur affiche `{"detail":"Token invalide ou expiré"}`. Ici :
- page HTML (navigation) -> redirection vers la page de connexion, avec un message ;
- appel d'API (fetch) -> 401 JSON + adresse de connexion (la page se redirige elle-même,
  voir le script dans admin/base.html et agent/base.html) ;
- dans les deux cas, les cookies de session périmés sont effacés.
Les clients de l'API publique (/api/v1, jeton en en-tête) gardent le 401 JSON habituel.
"""

from urllib.parse import quote

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

MESSAGE = "Votre session a expiré, veuillez vous reconnecter."

_PANELS = (
    ("/agent", "/agent/login", ("agent_token", "agent_refresh")),
    ("/admin", "/admin/login", ("admin_token", "admin_refresh")),
)


def panel_for(path: str):
    """(adresse de connexion, cookies de session) du panel visé par `path`, ou None."""
    for prefix, login, cookies in _PANELS:
        if path == prefix or path.startswith(prefix + "/"):
            if path.startswith(login):  # la page de connexion elle-même ne se redirige pas
                return None
            return login, cookies
    return None


def _wants_page(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return request.method == "GET" and "text/html" in accept and "/api/" not in request.url.path


async def session_expired_handler(request: Request, exc: StarletteHTTPException):
    panel = panel_for(request.url.path)
    if exc.status_code != 401 or panel is None:
        return await http_exception_handler(request, exc)

    login, cookies = panel
    if _wants_page(request):
        response = RedirectResponse(f"{login}?error={quote(MESSAGE)}", status_code=303)
    else:
        response = JSONResponse(
            status_code=401,
            content={"detail": exc.detail, "session_expired": True, "login_url": login},
        )
    for name in cookies:
        response.delete_cookie(name, path="/")
    return response
