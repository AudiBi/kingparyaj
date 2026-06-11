# app/core/redis_client.py
import redis.asyncio as redis
from app.config import settings

# Connexion Redis
redis_client = redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    password=settings.REDIS_PASSWORD
)


async def get_redis() -> redis.Redis:
    """Dépendance pour obtenir le client Redis"""
    return redis_client