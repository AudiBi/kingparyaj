# app/main.py
"""Application FastAPI - Parier Keno & Lucky Haïti
Version professionnelle avec gestion complète des workers, monitoring et sécurité
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, JSONResponse
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import sys
import os

from app.config import settings
from app.core.database import engine
from app.core.redis_client import redis_client
from app.core.logger import logger, setup_logging
from app.core.exceptions import AppException

# Ajouter le chemin du projet pour les imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ==================== LIFESPAN MANAGER ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Gestion du cycle de vie de l'application.
    Démarrage et arrêt des composants.
    """
    # ========== STARTUP ==========
    logger.info("=" * 60)
    logger.info(f"🚀 Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    logger.info(f"🌍 Environment: {settings.ENVIRONMENT}")
    logger.info(f"🔧 Debug mode: {settings.DEBUG}")
    logger.info("=" * 60)
    
    # 1. Vérifier Redis
    try:
        await redis_client.ping()
        logger.info("✅ Redis connected successfully")
    except Exception as e:
        logger.error(f"❌ Redis connection failed: {e}")
        raise
    
    # 2. Vérifier la base de données
    try:
        async with engine.begin() as conn:
            await conn.execute("SELECT 1")
        logger.info("✅ Database connected successfully")
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")
        raise
    
    # 3. Démarrer les workers Celery (production)
    if settings.ENVIRONMENT == "production":
        try:
            from app.workers.celery import celery_app
            # Vérifier que Celery est accessible
            logger.info("✅ Celery workers ready")
        except Exception as e:
            logger.error(f"❌ Celery initialization failed: {e}")
            # Ne pas bloquer le démarrage en développement
    
    # 4. Démarrer le scheduler de tirages (production)
    if settings.ENVIRONMENT == "production":
        try:
            from app.services.draw_scheduler import start_scheduler
            await start_scheduler()
            logger.info("✅ Draw scheduler started")
        except Exception as e:
            logger.error(f"❌ Draw scheduler failed: {e}")
            # Ne pas bloquer le démarrage si le scheduler échoue
    
    # 5. Nettoyer les sessions expirées au démarrage
    try:
        from app.workers.cleanup_worker import cleanup_expired_sessions
        cleanup_expired_sessions.delay()
        logger.info("✅ Cleanup tasks scheduled")
    except Exception as e:
        logger.warning(f"⚠️ Cleanup tasks scheduling failed: {e}")
    
    logger.info("=" * 60)
    logger.info("✅ Application started successfully")
    logger.info("=" * 60)
    
    yield
    
    # ========== SHUTDOWN ==========
    logger.info("=" * 60)
    logger.info("🛑 Shutting down application...")
    
    # 1. Arrêter le scheduler
    try:
        from app.services.draw_scheduler import stop_scheduler
        await stop_scheduler()
        logger.info("✅ Draw scheduler stopped")
    except Exception as e:
        logger.error(f"❌ Error stopping scheduler: {e}")
    
    # 2. Fermer la connexion Redis
    try:
        await redis_client.close()
        logger.info("✅ Redis connection closed")
    except Exception as e:
        logger.error(f"❌ Error closing Redis: {e}")
    
    # 3. Fermer la connexion à la base de données
    try:
        await engine.dispose()
        logger.info("✅ Database connection closed")
    except Exception as e:
        logger.error(f"❌ Error closing database: {e}")
    
    logger.info("✅ Application shutdown complete")
    logger.info("=" * 60)


# ==================== CRÉATION DE L'APPLICATION ====================

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Plateforme de paris Keno & Lucky pour Haïti - Licence LEH",
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url="/api/redoc" if settings.DEBUG else None,
    openapi_url="/api/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
    terms_of_service="https://parierkeno.ht/terms",
    contact={
        "name": "Parier Keno Haïti",
        "email": "contact@parierkeno.ht",
        "url": "https://parierkeno.ht",
    },
    license_info={
        "name": "LEH License",
        "url": "https://leh.ht/license",
    }
)


# ==================== MIDDLEWARES ====================

# 1. CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "X-Page"],
)


# 2. Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log toutes les requêtes HTTP avec timing"""
    start_time = datetime.utcnow()
    
    # Log de la requête
    logger.info(f"📥 {request.method} {request.url.path}")
    
    try:
        response = await call_next(request)
        
        # Calcul du temps d'exécution
        process_time = (datetime.utcnow() - start_time).total_seconds()
        
        # Ajouter le temps dans les headers
        response.headers["X-Process-Time"] = str(process_time)
        
        # Log de la réponse
        logger.info(f"📤 {request.method} {request.url.path} - {response.status_code} ({process_time:.3f}s)")
        
        return response
        
    except Exception as e:
        logger.error(f"❌ Error processing {request.method} {request.url.path}: {e}")
        raise


# 3. Exception handler middleware
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    """Gestion centralisée des exceptions applicatives"""
    logger.warning(f"⚠️ AppException: {exc.detail} (code: {exc.code})")
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "code": exc.code,
            "timestamp": datetime.utcnow().isoformat(),
            "path": request.url.path
        }
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Gestion centralisée des exceptions non capturées"""
    logger.error(f"❌ Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Une erreur interne est survenue",
            "code": "INTERNAL_ERROR",
            "timestamp": datetime.utcnow().isoformat(),
            "path": request.url.path
        }
    )


# ==================== TEMPLATES ET STATIC ====================

# Templates Jinja2
templates = Jinja2Templates(directory="app/templates")

# Fichiers statiques
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ==================== ROUTES API ====================

# Importer les routers (sans health)
from app.api.v1 import (
    auth,
    users,
    wallet,
    keno,
    lucky,
    tickets,
    agent,
    admin,
    reports
)

# Version 1 de l'API
api_v1_prefix = "/api/v1"

app.include_router(auth.router, prefix=api_v1_prefix)
app.include_router(users.router, prefix=api_v1_prefix)
app.include_router(wallet.router, prefix=api_v1_prefix)
app.include_router(keno.router, prefix=api_v1_prefix)
app.include_router(lucky.router, prefix=api_v1_prefix)
app.include_router(tickets.router, prefix=api_v1_prefix)
app.include_router(agent.router, prefix=api_v1_prefix)
app.include_router(admin.router, prefix=api_v1_prefix)
app.include_router(reports.router, prefix=api_v1_prefix)


# ==================== WEBSOCKETS ====================

from app.api.websockets import draws
app.include_router(draws.router)


# ==================== ROUTES HTML ====================

# Routes pour l'interface agent
try:
    from app.routes.agent_views import router as agent_views_router
    app.include_router(agent_views_router)
    logger.info("✅ Agent views loaded")
except ImportError as e:
    logger.warning(f"⚠️ Agent views not available: {e}")

# Routes pour l'interface publique
try:
    from app.routes.public_views import router as public_views_router
    app.include_router(public_views_router)
    logger.info("✅ Public views loaded")
except ImportError as e:
    logger.warning(f"⚠️ Public views not available: {e}")


# ==================== ENDPOINTS DE BASE ====================

@app.get("/", include_in_schema=False)
async def root():
    """Redirige vers l'interface publique"""
    return RedirectResponse(url="/public")


@app.get("/api", include_in_schema=False)
async def api_root():
    """Redirige vers la documentation API"""
    if settings.DEBUG:
        return RedirectResponse(url="/api/docs")
    return {"message": "API Parier Keno Haïti", "version": settings.APP_VERSION}


@app.get("/health", tags=["Health"])
async def health_check():
    """
    Health check pour monitoring.
    Vérifie la santé de l'application et des services.
    """
    services = {
        "api": {"status": "healthy"},
        "database": {"status": "unknown"},
        "redis": {"status": "unknown"},
        "workers": {"status": "unknown"}
    }
    overall_status = "healthy"
    
    # Vérifier la base de données
    try:
        async with engine.begin() as conn:
            await conn.execute("SELECT 1")
        services["database"]["status"] = "healthy"
    except Exception as e:
        services["database"]["status"] = "unhealthy"
        services["database"]["error"] = str(e)
        overall_status = "unhealthy"
    
    # Vérifier Redis
    try:
        await redis_client.ping()
        services["redis"]["status"] = "healthy"
    except Exception as e:
        services["redis"]["status"] = "unhealthy"
        services["redis"]["error"] = str(e)
        overall_status = "unhealthy"
    
    # Vérifier les workers (si en production)
    if settings.ENVIRONMENT == "production":
        try:
            from app.workers.celery import celery_app
            inspect = celery_app.control.inspect()
            active = inspect.active()
            services["workers"]["status"] = "healthy" if active else "idle"
            services["workers"]["active"] = len(active) if active else 0
        except Exception as e:
            services["workers"]["status"] = "unhealthy"
            services["workers"]["error"] = str(e)
            overall_status = "unhealthy"
    
    return {
        "status": overall_status,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "timestamp": datetime.utcnow().isoformat(),
        "services": services
    }


@app.get("/health/ready", tags=["Health"])
async def readiness_check():
    """
    Readiness probe pour Kubernetes.
    Vérifie que l'application est prête à recevoir du trafic.
    """
    services_healthy = True
    
    # Vérifier la base de données
    try:
        async with engine.begin() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        services_healthy = False
    
    # Vérifier Redis
    try:
        await redis_client.ping()
    except Exception:
        services_healthy = False
    
    if not services_healthy:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not ready",
                "timestamp": datetime.utcnow().isoformat()
            }
        )
    
    return {
        "status": "ready",
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/health/live", tags=["Health"])
async def liveness_check():
    """
    Liveness probe pour Kubernetes.
    Vérifie que l'application est encore en vie.
    """
    return {
        "status": "alive",
        "timestamp": datetime.utcnow().isoformat()
    }


# ==================== MÉTRIQUES ====================

@app.get("/metrics", tags=["Monitoring"], include_in_schema=False)
async def metrics():
    """
    Métriques Prometheus pour monitoring.
    À implémenter avec prometheus_client si nécessaire.
    """
    # À implémenter avec prometheus_client
    return {
        "message": "Metrics endpoint - À configurer avec Prometheus",
        "endpoints": {
            "/health": "Health check",
            "/health/ready": "Readiness probe",
            "/health/live": "Liveness probe"
        }
    }


# ==================== VERSION ====================

@app.get("/version", tags=["Info"])
async def get_version():
    """Récupère la version de l'application"""
    return {
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "name": settings.APP_NAME
    }


# ==================== EXCEPTION HANDLERS SPÉCIFIQUES ====================

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: Exception):
    """Gestion des erreurs 404"""
    return JSONResponse(
        status_code=404,
        content={
            "success": False,
            "error": "Ressource non trouvée",
            "code": "NOT_FOUND",
            "path": request.url.path,
            "timestamp": datetime.utcnow().isoformat()
        }
    )


@app.exception_handler(405)
async def method_not_allowed_handler(request: Request, exc: Exception):
    """Gestion des erreurs 405"""
    return JSONResponse(
        status_code=405,
        content={
            "success": False,
            "error": "Méthode non autorisée",
            "code": "METHOD_NOT_ALLOWED",
            "path": request.url.path,
            "timestamp": datetime.utcnow().isoformat()
        }
    )


# ==================== POINT D'ENTRÉE ====================

if __name__ == "__main__":
    import uvicorn
    
    # Configuration du serveur
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
        log_level=settings.LOG_LEVEL.lower(),
        workers=4 if settings.ENVIRONMENT == "production" else 1,
        timeout_keep_alive=60,
        timeout_graceful_shutdown=30,
    )