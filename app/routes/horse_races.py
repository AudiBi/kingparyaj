# app/routes/horse_races.py
"""Horse Races dans les panels agent (/agent/...) et admin (/admin/...).

Mêmes conventions que routes/agent.py et routes/admin.py : authentification
par cookie (get_current_agent / get_current_admin), protection CSRF appliquée
par AdminCsrfMiddleware sur ces préfixes, erreurs AppException.
"""

from typing import Optional

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import NotFoundException, ValidationException
from app.core.redis_client import get_redis
from app.core.security import get_current_admin, get_current_agent
from app.models.user import User
from app.schemas.horse_race import (
    AgentHorseRaceBetCreate,
    HorseRaceCancel,
    HorseRaceConfigUpdate,
    HorseRaceCreate,
)
from app.services.horse_race_service import HorseRaceService

agent_router = APIRouter(prefix="/agent", tags=["Agent - Horse Races"])
admin_router = APIRouter(prefix="/admin", tags=["Admin - Horse Races"])
public_router = APIRouter(prefix="/horse-races", tags=["Horse Races - Écran public"])


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


# ==================== AGENT ====================

@agent_router.get("/api/horse-races/current")
async def agent_current_race(
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    return await HorseRaceService(db, redis_client).live_state()


@agent_router.get("/api/horse-races/history")
async def agent_race_history(
    limit: int = Query(20, ge=1, le=100),
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = HorseRaceService(db, redis_client)
    return {"races": [service.serialize_race(r) for r in await service.get_history(limit=limit)]}


@agent_router.get("/api/horse-races/quote")
async def agent_quote(
    race_id: str,
    bet_type: str,
    selection: str = Query(..., description="Numéros séparés par des virgules, dans l'ordre"),
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Cote serveur d'une sélection (GET : pas de jeton CSRF à consommer)."""
    try:
        numbers = [int(x) for x in selection.split(",") if x.strip()]
    except ValueError:
        raise ValidationException("Sélection invalide")
    service = HorseRaceService(db, redis_client)
    race = await service.get_race(race_id)
    chosen, odds = service.quote(race, bet_type, numbers)
    return {"selection": chosen, "odds": float(odds)}


@agent_router.get("/api/horse-races/races/{race_id}/my-bets")
async def agent_race_bets(
    race_id: str,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Paris pris par cet agent sur une course."""
    from app.services.cash_ticket import ticket_numbers

    service = HorseRaceService(db, redis_client)
    bets = await service.get_agent_race_bets(race_id, current_agent.id)
    numbers = await ticket_numbers(db, [b.ticket_id for b in bets])
    return {"bets": [{**service.serialize_bet(b), "ticket_number": numbers.get(b.ticket_id)} for b in bets]}


@agent_router.get("/api/horse-races/csrf-token")
async def agent_csrf_token(request: Request, current_agent: User = Depends(get_current_agent)):
    """Jeton CSRF frais : le cookie CSRF est renouvelé à chaque réponse, une
    page qui a fait d'autres appels doit en redemander un avant un POST."""
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


@agent_router.post("/api/horse-races/bet")
async def agent_place_bet(
    payload: AgentHorseRaceBetCreate,
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Pari au bureau : pour un compte joueur (téléphone) ou un ticket cash."""
    from app.services.cash_ticket import resolve_funding

    service = HorseRaceService(db, redis_client)
    user_id, ticket_number = await resolve_funding(
        db, redis_client, current_agent, payload.player_type, payload.identifier, payload.stake, payload.player_name,
    )
    bet = await service.place_bet(
        race_id=payload.race_id,
        bet_type=payload.bet_type,
        selection=payload.selection,
        stake=payload.stake,
        user_id=user_id,
        ticket_number=ticket_number,
        agent_id=current_agent.id,
        ip_address=_ip(request),
    )
    from app.services.commission_service import freeze_commission

    await freeze_commission(db, redis_client, current_agent, bet, payload.player_type)  # commission figée à la vente
    await service.commit_and_publish()
    try:
        await service.screen_ticket(current_agent.id, bet)  # écran du joueur au guichet
    except Exception:
        pass  # l'affichage ne doit jamais bloquer un pari enregistré
    return {"success": True, "message": f"Pari enregistré{' — ticket ' + ticket_number if ticket_number else ''}",
            "bet": {**service.serialize_bet(bet), "ticket_number": ticket_number}}


@agent_router.post("/api/horse-races/screen")
async def agent_screen_preview(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Montre au joueur, sur son écran, le ticket en cours de saisie
    (type de pari, chevaux, mise, cote et gain potentiel). Affichage seulement."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or not payload.get("race_id"):
        raise ValidationException("Course manquante")
    state = await HorseRaceService(db, redis_client).screen_preview(
        current_agent.id, str(payload["race_id"]), payload.get("bet_type"), payload.get("selection"), payload.get("stake"),
    )
    return {"success": True, "seq": state["seq"]}


# ==================== ADMIN ====================

@admin_router.get("/api/horse-races/csrf-token")
async def admin_csrf_token(request: Request, admin: User = Depends(get_current_admin)):
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


@admin_router.get("/api/horse-races/config")
async def admin_get_config(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    return await HorseRaceService(db, redis_client).get_config()


@admin_router.put("/api/horse-races/config")
async def admin_update_config(
    payload: HorseRaceConfigUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = HorseRaceService(db, redis_client)
    config = await service.save_config(payload.model_dump(), admin_id=admin.id)
    await service.commit_and_publish()
    return {"success": True, "config": config}


@admin_router.get("/api/horse-races/races")
async def admin_list_races(
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    from sqlalchemy import select

    from app.models.game import GameRound

    service = HorseRaceService(db, redis_client)
    result = await db.execute(
        select(GameRound).where(GameRound.game_type == "horse_races").order_by(GameRound.scheduled_at.desc()).limit(limit)
    )
    rows = list(result.scalars().all())
    live = await service.bet_totals([r.id for r in rows])  # totaux en direct, même paris ouverts
    races = []
    for race in rows:
        data = service.serialize_race(race)
        totals = live.get(race.id, {"total_bets": race.total_bets or 0, "total_stake": race.total_stake or 0})
        data.update({
            "total_bets": totals["total_bets"],
            "total_stake": float(totals["total_stake"] or 0),
            "total_payout": float(race.total_payout or 0),
        })
        races.append(data)
    return {"races": races}


@admin_router.get("/api/horse-races/races/{race_id}")
async def admin_race_detail(
    race_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = HorseRaceService(db, redis_client)
    race = await service.get_race(race_id)
    bets = await service.get_race_bets(race.id)
    from app.services.cash_ticket import ticket_numbers

    numbers = await ticket_numbers(db, [b.ticket_id for b in bets])
    data = service.serialize_race(race)
    data.update({
        "total_bets": len(bets),
        "total_stake": float(sum((b.stake for b in bets), 0)),
        "total_payout": float(race.total_payout or 0),
        "bets": [
            {**service.serialize_bet(b), "user_id": b.user_id, "ticket_id": b.ticket_id, "agent_id": b.agent_id,
             "ticket_number": numbers.get(b.ticket_id)}
            for b in bets
        ],
    })
    return data


@admin_router.post("/api/horse-races/races")
async def admin_create_race(
    payload: HorseRaceCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = HorseRaceService(db, redis_client)
    scheduled_at = payload.scheduled_at
    if scheduled_at is not None and scheduled_at.tzinfo is not None:
        from datetime import timezone
        scheduled_at = scheduled_at.astimezone(timezone.utc).replace(tzinfo=None)
    race = await service.create_race(scheduled_at=scheduled_at, created_by=admin.id, open_betting=payload.open_betting)
    await service.commit_and_publish()
    return {"success": True, "race": service.serialize_race(race)}


async def _transition(action: str, race_id: str, admin: User, db: AsyncSession, redis_client: redis.Redis, **kwargs):
    service = HorseRaceService(db, redis_client)
    result = await getattr(service, action)(race_id, by=admin.id, **kwargs)
    await service.commit_and_publish()
    if isinstance(result, dict):
        return {"success": True, **result}
    return {"success": True, "race": service.serialize_race(result)}


@admin_router.post("/api/horse-races/races/{race_id}/open")
async def admin_open_betting(race_id: str, admin: User = Depends(get_current_admin),
                             db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("open_betting", race_id, admin, db, redis_client)


@admin_router.post("/api/horse-races/races/{race_id}/close")
async def admin_close_betting(race_id: str, admin: User = Depends(get_current_admin),
                              db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("close_betting", race_id, admin, db, redis_client)


@admin_router.post("/api/horse-races/races/{race_id}/start")
async def admin_start_race(race_id: str, admin: User = Depends(get_current_admin),
                           db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("start_race", race_id, admin, db, redis_client)


@admin_router.post("/api/horse-races/races/{race_id}/settle")
async def admin_settle_race(race_id: str, admin: User = Depends(get_current_admin),
                            db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    """Règle une course terminée (normalement fait automatiquement)."""
    return await _transition("settle_race", race_id, admin, db, redis_client)


@admin_router.post("/api/horse-races/races/{race_id}/cancel")
async def admin_cancel_race(race_id: str, payload: HorseRaceCancel, admin: User = Depends(get_current_admin),
                            db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("cancel_race", race_id, admin, db, redis_client, reason=payload.reason)


# ==================== PAGES HTML ====================

@agent_router.get("/horse-races", response_class=HTMLResponse)
async def agent_horse_races_page(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    from app.routes.agent import _base_context, templates

    base = await _base_context(db, current_agent, "horse_races")
    base["screen_code"] = HorseRaceService.screen_code(current_agent.id)
    return templates.TemplateResponse(request, "agent/horse_races.html", base)


@admin_router.get("/games/horse-races", response_class=HTMLResponse)
async def admin_horse_races_page(
    request: Request,
    admin: User = Depends(get_current_admin),
):
    from app.routes.admin import templates

    return templates.TemplateResponse(request, "admin/games/horse_races.html", {
        "active": "horse_races",
        "admin_name": admin.full_name or admin.email,
        "admin_role": getattr(admin.role, "value", admin.role),
        "version": "1.0.0",
    })


@public_router.get("/ecran", response_class=HTMLResponse)
async def public_screen(request: Request):
    """Écran de diffusion (TV du bureau) : course en direct, cotes, résultats.
    Lecture seule, sans connexion."""
    from app.routes.agent import templates

    return templates.TemplateResponse(request, "public/horse_races_screen.html", {})


@public_router.get("/mon-pari", response_class=HTMLResponse)
async def public_bet_lookup(request: Request, code: Optional[str] = None):
    """Suivi d'un pari par le joueur (code du reçu ou numéro de ticket)."""
    from app.routes.agent import templates

    return templates.TemplateResponse(request, "public/horse_races_bet.html", {"code": code or ""})


@public_router.get("/guichet", response_class=HTMLResponse)
async def public_counter_screen(request: Request, c: str = ""):
    """Écran du joueur au guichet : ticket en saisie, tickets enregistrés,
    course en direct et résultat de ses paris. Lecture seule."""
    from app.routes.agent import templates
    from app.services import counter_screen

    if not counter_screen.is_screen_code(c):
        raise NotFoundException("Écran", c)
    return templates.TemplateResponse(request, "public/horse_races_counter.html", {"code": c})


@public_router.get("/api/guichet/{code}")
async def public_counter_screen_state(
    code: str,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    from app.services import counter_screen

    if not counter_screen.is_screen_code(code):
        raise NotFoundException("Écran", code)
    return {"state": await HorseRaceService(db, redis_client).screen_state(code)}
