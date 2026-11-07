# tests/test_api/conftest.py
"""Outils communs aux tests des listes du panel admin."""

import re

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.csrf import AdminCsrfMiddleware
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.session_expired import session_expired_handler
from app.routes.admin import router as admin_router
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


def _csrf(html: str) -> str:
    return re.search(r'name="csrf_token"\s+value="([^"]*)"', html).group(1)


class Panel:
    """Navigateur admin connecté : chaque requête qui modifie envoie un jeton CSRF frais."""

    def __init__(self, client: AsyncClient):
        self.client = client

    async def headers(self) -> dict:
        return {"X-CSRFToken": _csrf((await self.client.get("/admin/login")).text)}

    async def get(self, url, **kw):
        return await self.client.get(url, **kw)

    async def post(self, url, **kw):
        return await self.client.post(url, headers=await self.headers(), **kw)

    async def put(self, url, **kw):
        return await self.client.put(url, headers=await self.headers(), **kw)

    async def delete(self, url, **kw):
        return await self.client.delete(url, headers=await self.headers(), **kw)


@pytest_asyncio.fixture
async def panel(db_session, fake_redis, admin_user):
    """Panel admin complet (routes admin + gestionnaire d'erreurs de app.main), admin connecté."""
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(StarletteHTTPException, session_expired_handler)
    app.add_middleware(AdminCsrfMiddleware)

    async def _db():
        yield db_session

    async def _redis():
        return fake_redis

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        page = await c.get("/admin/login")
        await c.post("/admin/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": _csrf(page.text)})
        yield Panel(c)
