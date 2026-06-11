# app/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
from app.config import settings
from app.core.database import engine
from app.core.redis_client import redis_client
from app.core.logger import logger
from app.api.v1 import (
    auth, users, wallet, keno, lucky, tickets, agent, admin, reports
)
from app.api.websockets import draws
from app.services.draw_scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestion du cycle de vie"""
    # Startup
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    
    # Vérifier Redis
    await redis_client.ping()
    logger.info("Redis connected")
    
    # Démarrer le scheduler de tirages
    if settings.ENVIRONMENT == "production":
        await start_scheduler()
        logger.info("Draw scheduler started")
    
    yield
    
    # Shutdown
    logger.info("Shutting down...")
    await stop_scheduler()
    await engine.dispose()
    await redis_client.close()


# Création de l'application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Plateforme de paris Keno & Lucky pour Haïti",
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url="/api/redoc" if settings.DEBUG else None,
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Templates et static
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Routers API
app.include_router(auth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(wallet.router, prefix="/api/v1")
app.include_router(keno.router, prefix="/api/v1")
app.include_router(lucky.router, prefix="/api/v1")
app.include_router(tickets.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")

# WebSockets
app.include_router(draws.router)

# Routes HTML
from app.routes.agent_views import router as agent_views_router
from app.routes.public_views import router as public_views_router
app.include_router(agent_views_router)
app.include_router(public_views_router)


@app.get("/health")
async def health_check():
    """Health check pour monitoring"""
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT
    }


@app.get("/")
async def root():
    """Racine - redirige vers interface publique"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/public")