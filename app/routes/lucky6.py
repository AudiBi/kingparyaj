# app/routes/lucky6.py
"""Lucky6 dans les panels agent (/agent/...), admin (/admin/...) et écrans publics (/lucky6/...).

Conventions identiques à routes/horse_races.py : authentification par cookie,
CSRF appliqué par AdminCsrfMiddleware sur /agent et /admin, erreurs AppException.
Paris UNIQUEMENT au bureau (agent). Aucune valeur de jeu n'est acceptée du
navigateur : cote, tirage et gain sont calculés par le serveur.
"""

from datetime import timedelta
from typing import Optional

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import NotFoundException, ValidationException
from app.core.redis_client import get_redis
from app.core.security import get_current_admin, get_current_agent
from app.core.timezone import now_utc
from app.models.game import GameBet, GameRound
from app.models.user import User
from app.schemas.lucky6 import (
    AgentLucky6BetCreate,
    Lucky6Cancel,
    Lucky6ConfigUpdate,
    Lucky6PaytablePreview,
    Lucky6RoundCreate,
)
from app.services import lucky6_engine as engine
from app.services.lucky6_service import GAME_TYPE, Lucky6Service

agent_router = APIRouter(prefix="/agent", tags=["Agent - Lucky6"])
admin_router = APIRouter(prefix="/admin", tags=["Admin - Lucky6"])
public_router = APIRouter(prefix="/lucky6", tags=["Lucky6 - Écrans publics"])


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


async def _bets_with_rounds(service: Lucky6Service, bets):
    ids = list({b.round_id for b in bets})
    rounds = {}
    if ids:
        rows = await service.db.execute(select(GameRound).where(GameRound.id.in_(ids)))
        rounds = {r.id: r for r in rows.scalars().all()}
    from app.services.cash_ticket import ticket_numbers

    numbers = await ticket_numbers(service.db, [b.ticket_id for b in bets])
    return [{**service.bet_details(b, rounds.get(b.round_id)), "ticket_number": numbers.get(b.ticket_id)} for b in bets]


# ==================== AGENT ====================

@agent_router.get("/api/lucky6/state")
async def agent_state(
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = Lucky6Service(db, redis_client)
    state = await service.live_state()
    state["config"] = engine.public_config(await service.get_config())
    return state


@agent_router.get("/api/lucky6/quick-pick")
async def agent_quick_pick(current_agent: User = Depends(get_current_agent)):
    """6 numéros au hasard (générateur cryptographique du serveur)."""
    return {"numbers": Lucky6Service.quick_pick()}


@agent_router.get("/api/lucky6/csrf-token")
async def agent_csrf_token(request: Request, current_agent: User = Depends(get_current_agent)):
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


@agent_router.post("/api/lucky6/bet")
async def agent_place_bet(
    payload: AgentLucky6BetCreate,
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    from app.services.cash_ticket import resolve_funding

    service = Lucky6Service(db, redis_client)
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
        await service.screen_ticket(current_agent.id, bet)
    except Exception:
        pass  # l'affichage ne bloque jamais un pari enregistré
    race = await service.get_race(bet.round_id)
    data = service.bet_details(bet, race)
    data.update({"server_seed_hash": race.server_seed_hash, "scheduled_at": service.serialize_race(race)["scheduled_at"],
                 "ticket_number": ticket_number})
    return {"success": True, "message": f"Pari enregistré{' — ticket ' + ticket_number if ticket_number else ''}", "bet": data}


@agent_router.post("/api/lucky6/screen")
async def agent_screen_preview(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Ticket en cours de saisie affiché sur l'écran du joueur (affichage seulement)."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict) or not payload.get("race_id"):
        raise ValidationException("Manche manquante")
    state = await Lucky6Service(db, redis_client).screen_preview(
        current_agent.id, str(payload["race_id"]), payload.get("bet_type"), payload.get("selection"), payload.get("stake"),
    )
    return {"success": True, "seq": state["seq"]}


@agent_router.get("/api/lucky6/my-bets")
async def agent_my_bets(
    limit: int = Query(30, ge=1, le=100),
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Derniers paris Lucky6 pris par cet agent (avec numéros trouvés)."""
    service = Lucky6Service(db, redis_client)
    rows = await db.execute(
        select(GameBet)
        .where(GameBet.agent_id == current_agent.id, GameBet.game_type == GAME_TYPE,
               GameBet.placed_at >= now_utc() - timedelta(days=2))
        .order_by(GameBet.placed_at.desc())
        .limit(limit)
    )
    return {"bets": await _bets_with_rounds(service, list(rows.scalars().all()))}


@agent_router.get("/api/lucky6/bets/{bet_id}")
async def agent_bet_receipt(
    bet_id: str,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Réimpression d'un reçu : uniquement les paris de cet agent."""
    bet = await db.get(GameBet, bet_id)
    # un pari d'un autre agent est traité comme introuvable (rien n'est divulgué)
    if bet is None or bet.game_type != GAME_TYPE or bet.agent_id != current_agent.id:
        raise NotFoundException("Pari", bet_id)
    service = Lucky6Service(db, redis_client)
    race = await service.get_race(bet.round_id)
    data = service.bet_details(bet, race)
    data.update({"server_seed_hash": race.server_seed_hash, "scheduled_at": service.serialize_race(race)["scheduled_at"]})
    return {"bet": data}


@agent_router.get("/api/lucky6/history")
async def agent_history(
    limit: int = Query(15, ge=1, le=100),
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = Lucky6Service(db, redis_client)
    return {"rounds": [service.serialize_race(r) for r in await service.get_history(limit=limit)]}


# ==================== ADMIN ====================

@admin_router.get("/api/lucky6/csrf-token")
async def admin_csrf_token(request: Request, admin: User = Depends(get_current_admin)):
    return {"csrf_token": getattr(request.state, "csrf_token", "")}


@admin_router.get("/api/lucky6/config")
async def admin_get_config(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    config = await Lucky6Service(db, redis_client).get_config()
    return {
        "config": config,
        "positions": engine.POSITIONS,
        "rtp": engine.rtp(config["paytable"]),
        "rtp_table": engine.rtp_table(config["paytable"]),
        "win_probability": engine.WIN_PROBABILITY,
        "default_paytable": engine.DEFAULT_PAYTABLE,
        "classic_shape": engine.CLASSIC_SHAPE,
    }


@admin_router.put("/api/lucky6/config")
async def admin_update_config(
    payload: Lucky6ConfigUpdate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Appliquée aux PROCHAINES manches ; une manche déjà créée garde ses réglages figés."""
    service = Lucky6Service(db, redis_client)
    config = await service.save_config(payload.model_dump(), admin_id=admin.id)
    await service.commit_and_publish()
    return {"success": True, "config": config, "rtp": engine.rtp(config["paytable"])}


@admin_router.post("/api/lucky6/paytable/preview")
async def admin_paytable_preview(payload: Lucky6PaytablePreview, admin: User = Depends(get_current_admin)):
    """Taux exact d'une table (et ajustement à un taux cible). N'enregistre rien."""
    table = payload.paytable
    if len(table) != len(engine.POSITIONS):
        raise ValidationException(f"La table doit avoir {len(engine.POSITIONS)} lignes")
    if payload.target_rtp:
        table = engine.scale_paytable(table, payload.target_rtp)
    return {"paytable": table, "rtp": engine.rtp(table), "rtp_table": engine.rtp_table(table)}


@admin_router.get("/api/lucky6/rounds")
async def admin_list_rounds(
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = Lucky6Service(db, redis_client)
    result = await db.execute(
        select(GameRound).where(GameRound.game_type == GAME_TYPE).order_by(GameRound.scheduled_at.desc()).limit(limit)
    )
    rows = list(result.scalars().all())
    live = await service.bet_totals([r.id for r in rows])
    rounds = []
    for r in rows:
        data = service.serialize_race(r)
        totals = live.get(r.id, {"total_bets": r.total_bets or 0, "total_stake": r.total_stake or 0})
        data.update({
            "total_bets": totals["total_bets"],
            "total_stake": float(totals["total_stake"] or 0),
            "total_payout": float(r.total_payout or 0),
        })
        rounds.append(data)
    return {"rounds": rounds}


@admin_router.get("/api/lucky6/rounds/{race_id}")
async def admin_round_detail(
    race_id: str,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = Lucky6Service(db, redis_client)
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
            {**service.bet_details(b, race), "user_id": b.user_id, "ticket_id": b.ticket_id, "agent_id": b.agent_id,
             "ticket_number": numbers.get(b.ticket_id)}
            for b in bets
        ],
    })
    return data


@admin_router.post("/api/lucky6/rounds")
async def admin_create_round(
    payload: Lucky6RoundCreate,
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = Lucky6Service(db, redis_client)
    config = await service.get_config()
    now = now_utc()
    scheduled_at = now + timedelta(minutes=payload.minutes) if payload.minutes else service.next_scheduled_at(config, now)
    race = await service.create_race(scheduled_at=scheduled_at, created_by=admin.id, open_betting=payload.open_betting)
    await service.commit_and_publish()
    return {"success": True, "round": service.serialize_race(race)}


async def _transition(action: str, race_id: str, admin: User, db: AsyncSession, redis_client: redis.Redis, **kwargs):
    service = Lucky6Service(db, redis_client)
    result = await getattr(service, action)(race_id, by=admin.id, **kwargs)
    await service.commit_and_publish()
    if isinstance(result, dict):
        return {"success": True, **result}
    return {"success": True, "round": service.serialize_race(result)}


@admin_router.post("/api/lucky6/rounds/{race_id}/open")
async def admin_open(race_id: str, admin: User = Depends(get_current_admin),
                     db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("open_betting", race_id, admin, db, redis_client)


@admin_router.post("/api/lucky6/rounds/{race_id}/close")
async def admin_close(race_id: str, admin: User = Depends(get_current_admin),
                      db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("close_betting", race_id, admin, db, redis_client)


@admin_router.post("/api/lucky6/rounds/{race_id}/start")
async def admin_start(race_id: str, admin: User = Depends(get_current_admin),
                      db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("start_race", race_id, admin, db, redis_client)


@admin_router.post("/api/lucky6/rounds/{race_id}/settle")
async def admin_settle(race_id: str, admin: User = Depends(get_current_admin),
                       db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("settle_race", race_id, admin, db, redis_client)


@admin_router.post("/api/lucky6/rounds/{race_id}/cancel")
async def admin_cancel(race_id: str, payload: Lucky6Cancel, admin: User = Depends(get_current_admin),
                       db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await _transition("cancel_race", race_id, admin, db, redis_client, reason=payload.reason)


# ==================== PAGES HTML ====================

@agent_router.get("/lucky6", response_class=HTMLResponse)
async def agent_lucky6_page(
    request: Request,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
):
    from app.routes.agent import _base_context, templates

    base = await _base_context(db, current_agent, "lucky6")
    base["screen_code"] = Lucky6Service.screen_code(current_agent.id)
    return templates.TemplateResponse(request, "agent/lucky6.html", base)


@admin_router.get("/games/lucky6", response_class=HTMLResponse)
async def admin_lucky6_page(request: Request, admin: User = Depends(get_current_admin)):
    from app.routes.admin import templates

    return templates.TemplateResponse(request, "admin/games/lucky6.html", {
        "active": "lucky6",
        "admin_name": admin.full_name or admin.email,
        "admin_role": getattr(admin.role, "value", admin.role),
        "version": "1.0.0",
    })


@public_router.get("/ecran", response_class=HTMLResponse)
async def public_screen(request: Request, c: str = ""):
    """Écran de diffusion. Sans code : TV de la salle. Avec ?c=<code> : écran du
    joueur au guichet (ticket en saisie + ses tickets). Lecture seule."""
    from app.routes.agent import templates
    from app.services import counter_screen

    if c and not counter_screen.is_screen_code(c):
        raise NotFoundException("Écran", c)
    return templates.TemplateResponse(request, "public/lucky6_screen.html", {"code": c})


@public_router.get("/api/live")
async def public_live(db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    service = Lucky6Service(db, redis_client)
    state = await service.live_state()
    state["history"] = [service.serialize_race(r) for r in await service.get_history(limit=10)]
    return state


@public_router.get("/api/guichet/{code}")
async def public_counter_state(
    code: str,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    from app.services import counter_screen

    if not counter_screen.is_screen_code(code):
        raise NotFoundException("Écran", code)
    return {"state": await Lucky6Service(db, redis_client).screen_state(code)}


@public_router.get("/verifier", response_class=HTMLResponse)
async def public_verify_page(request: Request, manche: str = ""):
    from app.routes.agent import templates

    return templates.TemplateResponse(request, "public/lucky6_verify.html", {"round_id": manche})
