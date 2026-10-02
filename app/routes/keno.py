# app/routes/keno.py
"""Keno : pages publiques (sans connexion).

Les pages agent et admin du Keno restent dans app/routes/agent.py et
app/routes/admin.py ; l'API dans app/api/v1/keno.py."""

from typing import Optional

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import NotFoundException
from app.core.redis_client import get_redis

public_router = APIRouter(prefix="/keno", tags=["Keno - Pages publiques"])


@public_router.get("/verifier", response_class=HTMLResponse)
async def public_keno_verify(request: Request, tirage: Optional[str] = None):
    """Vérification d'un tirage par le joueur : empreinte publiée avant le
    pari, seed révélé, 20 numéros recalculés dans le navigateur ET par le serveur."""
    from app.routes.agent import templates

    return templates.TemplateResponse(request, "public/keno_verify.html", {"draw_id": tirage or ""})


@public_router.get("/ecran", response_class=HTMLResponse)
async def public_keno_screen(request: Request, c: str = ""):
    """Écran du joueur au guichet (2e moniteur ou TV) : numéros demandés,
    mise, lancement du jeu et tireuse de boules. Lecture seule."""
    from app.routes.agent import templates
    from app.services.keno_service import KenoService

    if not KenoService.is_screen_code(c):
        raise NotFoundException("Écran", c)
    return templates.TemplateResponse(request, "public/keno_screen.html", {"code": c})


@public_router.get("/api/ecran/{code}")
async def public_keno_screen_state(
    code: str,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Dernier état de l'écran (reprise après rechargement ou coupure)."""
    from app.services.keno_service import KenoService

    if not KenoService.is_screen_code(code):
        raise NotFoundException("Écran", code)
    return {"state": await KenoService(db, redis_client).screen_state(code)}


# ==================== KENO PARTAGÉ : écrans ====================

@public_router.get("/salle", response_class=HTMLResponse)
async def public_keno_live_screen(request: Request, c: str = ""):
    """Écran des tirages partagés. Sans code : TV de la salle. Avec ?c=<code> :
    écran du joueur au guichet (ticket en saisie + ses tickets). Lecture seule."""
    from app.routes.agent import templates
    from app.services.keno_service import KenoService

    if c and not KenoService.is_screen_code(c):
        raise NotFoundException("Écran", c)
    return templates.TemplateResponse(request, "public/keno_live.html", {"code": c})


@public_router.get("/api/live")
async def public_keno_live(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Tirage en cours / prochain tirage, et derniers résultats (aucune donnée de joueur)."""
    from app.services.keno_service import KenoService

    service = KenoService(db, redis_client)
    config = await service.get_config()
    state = await service.live_state()
    state["history"] = [service.serialize_draw(d, config) for d in await service.shared_history(10)]
    return state


@public_router.get("/api/salle/{code}")
async def public_keno_live_counter(
    code: str,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    from app.services.keno_service import KenoService

    if not KenoService.is_screen_code(code):
        raise NotFoundException("Écran", code)
    return {"state": await KenoService(db, redis_client).shared_screen_state(code)}
