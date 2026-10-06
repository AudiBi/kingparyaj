# app/api/websockets/manager.py
"""
Diffusion WebSocket — point d'entrée unique pour tout le code (routes, services,
workers Celery).

Problème corrigé : les clients se connectent au gestionnaire de
`app.api.websockets.draws` (route /ws/draws/{draw_id}), alors que le code
diffusait via un AUTRE gestionnaire, défini ici, sans aucune connexion. Et un
worker Celery tourne dans un autre processus : il ne peut pas atteindre les
WebSockets ouvertes dans le processus FastAPI.

Fonctionnement :
1. publish() publie le message sur le canal Redis `ws:broadcast` ;
2. le processus FastAPI écoute ce canal (run_relay(), lancé au démarrage)
   et transmet au gestionnaire de connexions de draws.py ;
3. si Redis est indisponible, on diffuse directement aux clients connectés
   au processus courant (utile en développement et dans les tests).
"""

import asyncio
import json
from typing import Any, Dict

from app.core.logger import get_logger
from app.core.redis_client import redis_client
from app.core.timezone import now_utc

logger = get_logger("websockets")

WS_BROADCAST_CHANNEL = "ws:broadcast"


def _get_connection_manager():
    # Import local : draws.py importe ce module.
    from app.api.websockets.draws import manager as connection_manager
    return connection_manager


class _ManagerProxy:
    """Compatibilité : `from app.api.websockets.manager import manager`
    renvoie désormais le gestionnaire réellement utilisé par les clients."""

    def __getattr__(self, name):
        return getattr(_get_connection_manager(), name)


manager = _ManagerProxy()


def _timestamp() -> str:
    return now_utc().isoformat() + "Z"


async def publish(message: Dict[str, Any], draw_id: str = "all") -> None:
    """Diffuse un message à tous les clients WebSocket, quel que soit le
    processus appelant (FastAPI ou worker Celery)."""
    envelope = json.dumps({"draw_id": draw_id, "message": message}, default=str)
    try:
        await redis_client.publish(WS_BROADCAST_CHANNEL, envelope)
        return
    except Exception as e:  # Redis absent, client de test sans pub/sub…
        logger.warning(f"WebSocket relay Redis indisponible, diffusion locale : {e}")
    await _get_connection_manager().broadcast(json.loads(envelope)["message"], draw_id)


async def run_relay() -> None:
    """Boucle d'écoute du canal Redis (à lancer dans le processus FastAPI)."""
    connection_manager = _get_connection_manager()
    while True:
        pubsub = None
        try:
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(WS_BROADCAST_CHANNEL)
            logger.info("✅ WebSocket relay Redis démarré")
            async for item in pubsub.listen():
                if item.get("type") != "message":
                    continue
                try:
                    payload = json.loads(item["data"])
                    await connection_manager.broadcast(payload["message"], payload.get("draw_id", "all"))
                except Exception as e:
                    logger.error(f"WebSocket relay : message ignoré ({e})")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"WebSocket relay interrompu, reconnexion dans 5 s : {e}")
            await asyncio.sleep(5)
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception:
                    pass


# ==================== FONCTIONS DE DIFFUSION ====================

async def broadcast_draw_result(draw_result: dict):
    """Diffuse les résultats d'un tirage à tous les clients connectés
    (le canal "all" couvre aussi les abonnés d'un tirage précis)."""
    await publish({"type": "draw_completed", "data": draw_result, "timestamp": _timestamp()}, draw_id="all")


async def broadcast_jackpot_alert(jackpot_data: dict):
    """Diffuse une alerte jackpot"""
    await publish({"type": "jackpot_alert", "data": jackpot_data, "timestamp": _timestamp()}, draw_id="all")
