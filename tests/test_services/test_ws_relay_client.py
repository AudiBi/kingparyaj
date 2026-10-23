# tests/test_services/test_ws_relay_client.py
"""La connexion Redis d'écoute des WebSocket ne doit pas avoir de délai de lecture
(redis-py 8 en met un de 5 s par défaut : le relais était coupé et relancé toutes les 10 s)."""

import pytest

from app.api.websockets.manager import new_relay_client
from app.core.redis_client import redis_client


def test_shared_client_has_the_short_default_timeout():
    assert redis_client.connection_pool.make_connection().socket_timeout == 5  # d'où le problème


@pytest.mark.asyncio
async def test_relay_client_never_times_out_while_idle():
    client = new_relay_client()
    try:
        connection = client.connection_pool.make_connection()
        assert connection.socket_timeout is None
        assert connection.socket_keepalive is True and connection.health_check_interval == 30
        assert client is not redis_client
    finally:
        await client.aclose()
