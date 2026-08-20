from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer
from jinja2 import pass_context
from fastapi_csrf_protect import CsrfProtect
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import settings


class CsrfSettings(BaseModel):
    secret_key: str = settings.CSRF_SECRET_KEY
    cookie_samesite: str = "lax"
    cookie_secure: bool = not settings.DEBUG


@CsrfProtect.load_config
def get_csrf_config():
    return CsrfSettings()


CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_FORM_CONTENT_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")
# Nom déjà utilisé partout dans les templates admin existants (fetch() avec
# header 'X-CSRFToken', <input type="hidden" name="csrf_token">) : on s'y
# aligne plutôt que d'imposer une autre convention.
CSRF_HEADER_NAMES = ("X-CSRFToken", "X-CSRF-Token")
CSRF_FIELD_NAME = "csrf_token"


async def _extract_submitted_token(request: Request) -> str | None:
    """Cherche le jeton soumis, header d'abord (tous les appels fetch() des
    templates admin), puis corps de formulaire (login.html, ou tout futur
    <form> natif), puis JSON (au cas où)."""
    for header_name in CSRF_HEADER_NAMES:
        value = request.headers.get(header_name)
        if value:
            return value

    content_type = request.headers.get("content-type", "")
    if any(ct in content_type for ct in CSRF_FORM_CONTENT_TYPES):
        form = await request.form()
        value = form.get(CSRF_FIELD_NAME)
        return str(value) if value else None
    if "application/json" in content_type:
        try:
            data = await request.json()
        except Exception:
            return None
        return data.get(CSRF_FIELD_NAME) if isinstance(data, dict) else None
    return None


CSRF_PROTECTED_PREFIXES = ("/admin", "/agent")


class AdminCsrfMiddleware(BaseHTTPMiddleware):
    """Protection CSRF centralisée pour les panels HTML `/admin` et `/agent`.

    Plutôt que d'instrumenter individuellement chacune des ~95 routes
    (formulaires/actions AJAX dans app/routes/admin.py + templates), un seul
    point génère le jeton exposé aux templates (via le global Jinja
    `csrf_token`, cf. `register_csrf_globals` ci-dessous) et valide les
    soumissions des méthodes mutantes.

    La validation ne passe pas par `CsrfProtect.validate_csrf()` : cette
    méthode ne lit le jeton soumis que dans UN seul emplacement fixé par la
    config (header OU corps). Or les templates admin existants mélangent
    les deux selon la page : `<input type="hidden" name="csrf_token">` pour
    les formulaires natifs (login, quelques pages de settings), et un header
    `X-CSRFToken` pour la quasi-totalité des actions en fetch()/AJAX. On
    réimplémente donc la vérification (même sérialiseur/sel/cookie que la
    lib) en acceptant les deux.

    Les routes JSON sous /api/v1/admin ne sont pas concernées (préfixe
    différent) : elles s'authentifient par Bearer token, pas par cookie de
    session, donc pas exposées au CSRF de la même façon.
    """

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(CSRF_PROTECTED_PREFIXES):
            return await call_next(request)

        csrf_protect = CsrfProtect()

        if request.method not in CSRF_SAFE_METHODS:
            # IMPORTANT (ordre) : Starlette._CachedRequest ne rejoue le corps
            # pour la route en aval que si `self._body` a été peuplé par un
            # appel à `.body()` (pas `.form()`/`.json()` seuls, qui lisent le
            # flux sans le peupler). On l'appelle donc avant toute lecture,
            # pour que `.form()`/`.json()` réutilisent ensuite ce cache au
            # lieu de relire un flux ASGI déjà consommé.
            await request.body()

            if not await self._is_valid(request, csrf_protect):
                target = request.headers.get("referer") or str(request.url)
                separator = "&" if "?" in target else "?"
                return RedirectResponse(f"{target}{separator}csrf_error=1", status_code=303)

        csrf_token, signed_token = csrf_protect.generate_csrf_tokens()
        request.state.csrf_token = csrf_token

        response = await call_next(request)
        csrf_protect.set_csrf_cookie(signed_token, response)
        return response

    @staticmethod
    async def _is_valid(request: Request, csrf_protect: CsrfProtect) -> bool:
        signed_token = request.cookies.get(csrf_protect._cookie_key)
        if not signed_token:
            return False

        try:
            submitted_token = await _extract_submitted_token(request)
        except Exception:
            return False
        if not submitted_token:
            return False

        serializer = URLSafeTimedSerializer(csrf_protect._secret_key, salt="fastapi-csrf-token")
        try:
            expected_token = serializer.loads(signed_token, max_age=csrf_protect._max_age)
        except (BadData, SignatureExpired):
            return False

        return submitted_token == expected_token


@pass_context
def csrf_token_global(context) -> str:
    """Fonction globale Jinja `csrf_token()`, déjà appelée telle quelle dans
    tous les templates admin (formulaires cachés et headers fetch())."""
    request = context.get("request")
    return getattr(request.state, "csrf_token", "") if request else ""


def register_csrf_globals(templates) -> None:
    """À appeler une fois sur le Jinja2Templates du panel admin."""
    templates.env.globals["csrf_token"] = csrf_token_global
