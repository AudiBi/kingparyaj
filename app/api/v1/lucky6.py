# app/api/v1/lucky6.py
"""API Lucky6 (/api/v1/lucky6) — lecture seule.

Les paris Lucky6 se prennent uniquement au bureau (panel agent) : cette API
ne permet pas de parier. Elle expose les règles, la manche en cours,
l'historique et la vérification d'équité."""

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import AppException
from app.core.redis_client import get_redis
from app.services import lucky6_engine as engine
from app.services.lucky6_service import Lucky6Service

router = APIRouter(prefix="/lucky6", tags=["Lucky6"])


@router.get("/config", summary="Règles, table de paiement et taux de redistribution")
async def config(db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return engine.public_config(await Lucky6Service(db, redis_client).get_config())


@router.get("/rounds/current", summary="Manche en cours et manche qui prend les paris")
async def current(db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    return await Lucky6Service(db, redis_client).live_state()


@router.get("/rounds/history", summary="Manches terminées")
async def history(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    service = Lucky6Service(db, redis_client)
    return {"rounds": [service.serialize_race(r) for r in await service.get_history(limit=limit, offset=offset)]}


@router.get("/rounds/{round_id}", summary="Détail d'une manche")
async def detail(round_id: str, db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    service = Lucky6Service(db, redis_client)
    return service.serialize_race(await service.get_race(round_id))


@router.get("/rounds/{round_id}/verify", summary="Vérification de l'équité (seed révélé après le tirage)")
async def verify(round_id: str, db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    data = await Lucky6Service(db, redis_client).verify(round_id)
    if not data["verification"]:
        raise AppException(409, "Le seed est révélé à la fin du tirage", "SEED_NOT_REVEALED")
    return data
