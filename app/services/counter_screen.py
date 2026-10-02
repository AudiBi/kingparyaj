# app/services/counter_screen.py
"""Écran du joueur au guichet (2e moniteur ou TV) — commun à tous les jeux.

Chaque guichet (agent) a, pour chaque jeu, un code d'écran non devinable
(HMAC du secret de l'application). Ce code sert :
- de canal WebSocket : /ws/draws/<jeu>-screen-<code> ;
- de clé Redis pour le dernier état (reprise après coupure ou rechargement).
Rien n'est joué ni débité ici : affichage uniquement, sans donnée d'identité.
"""

import hashlib
import hmac
import json
from typing import Any, Dict, Optional

from app.core.logger import get_logger
from app.core.timezone import now_utc

logger = get_logger("counter_screen")
STATE_TTL = 6 * 3600


def screen_code(game: str, agent_id: str) -> str:
    from app.config import settings

    return hmac.new(settings.SECRET_KEY.encode(), f"{game}-screen:{agent_id}".encode(), hashlib.sha256).hexdigest()[:20]


def is_screen_code(code: str) -> bool:
    return isinstance(code, str) and len(code) == 20 and all(c in "0123456789abcdef" for c in code)


def _key(game: str, code: str) -> str:
    return f"{game}:screen:{code}"


async def get_state(redis_client, game: str, code: str) -> Optional[Dict[str, Any]]:
    if not is_screen_code(code):
        return None
    try:
        raw = await redis_client.get(_key(game, code))
    except Exception:
        raw = None
    return json.loads(raw) if raw else None


async def publish(redis_client, game: str, agent_id: str, payload: Dict[str, Any], merge: bool = False) -> Dict[str, Any]:
    """Mémorise et diffuse l'état de l'écran du guichet. `merge` complète
    l'état précédent (ex. liste des tickets) au lieu de le remplacer."""
    from app.api.websockets.manager import publish as ws_publish

    code = screen_code(game, agent_id)
    key = _key(game, code)
    base: Dict[str, Any] = {}
    if merge:
        base = await get_state(redis_client, game, code) or {}
    try:
        seq = int(await redis_client.incr(f"{key}:seq"))
    except Exception:
        seq = int(now_utc().timestamp() * 1000)
    state = {**base, **payload, "seq": seq, "at": now_utc().isoformat() + "Z"}
    try:
        await redis_client.set(key, json.dumps(state, default=str), ex=STATE_TTL)
    except Exception as e:
        logger.warning(f"Écran {game} : état non mémorisé ({e})")
    try:
        await ws_publish({"type": f"{game}_screen", "data": state}, draw_id=f"{game}-screen-{code}")
    except Exception as e:
        logger.warning(f"Écran {game} : diffusion impossible ({e})")
    return state
