# app/api/v1/horse_races.py
"""API joueur Horse Races (/api/v1/horse-races)."""

from typing import Optional

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import AppException, NotFoundException
from app.core.redis_client import get_redis
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.horse_race import HorseRaceQuoteRequest
from app.services import horse_race_engine as engine
from app.services.horse_race_service import HorseRaceService

router = APIRouter(prefix="/horse-races", tags=["Horse Races"])


@router.get("/races/current", summary="Course en cours ou prochaine course")
async def current_race(db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await HorseRaceService(db, redis_client).live_state()


@router.get("/races/history", summary="Courses terminées")
async def race_history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = HorseRaceService(db, redis_client)
    races = await service.get_history(limit=limit, offset=offset)
    return {"races": [service.serialize_race(r) for r in races]}


@router.get("/races/{race_id}", summary="Détail d'une course (participants, cotes, statut)")
async def race_detail(race_id: str, db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    service = HorseRaceService(db, redis_client)
    return service.serialize_race(await service.get_race(race_id))


@router.get("/races/{race_id}/result", summary="Classement final (disponible au départ de la course)")
async def race_result(race_id: str, db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    service = HorseRaceService(db, redis_client)
    data = service.serialize_race(await service.get_race(race_id))
    if data["results"] is None:
        raise AppException(409, "Résultat pas encore disponible", "RESULT_NOT_AVAILABLE")
    return {"race_id": data["race_id"], "race_number": data["race_number"], "status": data["status"], "results": data["results"]}


@router.get("/races/{race_id}/verify", summary="Vérification de l'équité (seed révélé)")
async def race_verify(race_id: str, db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    service = HorseRaceService(db, redis_client)
    race = await service.get_race(race_id)
    data = service.serialize_race(race)
    if not data["server_seed"] or not race.result:
        raise AppException(409, "Le seed est révélé à l'arrivée de la course", "SEED_NOT_REVEALED")
    check = engine.verify_race(
        race.server_seed, race.server_seed_hash,
        [p["player_number"] for p in race.participants],
        [p["probability"] for p in race.participants],
        race.round_number, race.nonce, race.result,
    )
    return {
        "race_id": race.id,
        "race_number": race.round_number,
        "server_seed": race.server_seed,
        "server_seed_hash": race.server_seed_hash,
        "nonce": race.nonce,
        "probabilities": {p["player_number"]: p["probability"] for p in race.participants},
        "algorithm": "HMAC-SHA256(server_seed, 'round:nonce:finish:position') -> tirage Plackett-Luce",
        **check,
    }


@router.post("/quote", summary="Cote serveur d'une sélection (sans parier)")
async def quote(payload: HorseRaceQuoteRequest, db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    service = HorseRaceService(db, redis_client)
    race = await service.get_race(payload.race_id)
    selection, odds = service.quote(race, payload.bet_type, payload.selection)
    return {"race_id": race.id, "bet_type": payload.bet_type, "selection": selection, "odds": float(odds)}


# Pas de POST /bets : les paris se prennent uniquement chez un agent
# (cf. /agent/api/horse-races/bet). Le joueur suit ses paris ci-dessous.


@router.get("/lookup", summary="Suivre un pari (code du reçu ou numéro de ticket)")
async def lookup_bets(
    code: str = Query(..., min_length=6, max_length=40),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Public : le code imprimé sur le reçu (ou le numéro du ticket) suffit.
    Aucune donnée personnelle ni solde n'est renvoyé."""
    service = HorseRaceService(db, redis_client)
    bets = await service.lookup_bets(code)
    races = {}
    items = []
    for bet in bets:
        if bet.round_id not in races:
            races[bet.round_id] = service.serialize_race(await service.get_race(bet.round_id))
        race = races[bet.round_id]
        names = {p["player_number"]: p["player_name"] for p in race["participants"]}
        items.append({
            **service.serialize_bet(bet),
            "selection_names": [names.get(n) for n in bet.selection],
            "race_number": race["race_number"],
            "race_status": race["status"],
            "race_scheduled_at": race["scheduled_at"],
            "race_results": race["results"],
        })
    return {"bets": items}


@router.get("/bets/history", summary="Mes paris")
async def my_bets(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = HorseRaceService(db, redis_client)
    bets = await service.get_user_bets(current_user.id, limit=limit, offset=offset)
    return {"bets": [service.serialize_bet(b) for b in bets]}


@router.get("/bets/{bet_id}", summary="Détail d'un de mes paris")
async def my_bet(
    bet_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    from app.models.game import GameBet

    bet = await db.get(GameBet, bet_id)
    if bet is None or bet.user_id != current_user.id:  # jamais le pari d'un autre joueur
        raise NotFoundException("Pari", bet_id)
    return HorseRaceService.serialize_bet(bet)
