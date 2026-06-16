# app/api/v1/__init__.py
"""API v1 - Routes complètes pour Parier Keno & Lucky Haïti"""

from fastapi import APIRouter

# Import de tous les routers
from app.api.v1 import (
    auth,
    users,
    wallet,
    keno,
    lucky,
    tickets,
    agent,
    admin,
    reports,
    webhooks,
    health
)

# Router principal
router = APIRouter(prefix="/api/v1")

# Inclusion de tous les sous-routers
router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
router.include_router(users.router, prefix="/users", tags=["Users"])
router.include_router(wallet.router, prefix="/wallet", tags=["Wallet"])
router.include_router(keno.router, prefix="/keno", tags=["Keno"])
router.include_router(lucky.router, prefix="/lucky", tags=["Lucky"])
router.include_router(tickets.router, prefix="/tickets", tags=["Tickets"])
router.include_router(agent.router, prefix="/agent", tags=["Agent"])
router.include_router(admin.router, prefix="/admin", tags=["Admin"])
router.include_router(reports.router, prefix="/reports", tags=["Reports"])
router.include_router(webhooks.router, prefix="/webhooks", tags=["Webhooks"])
router.include_router(health.router, prefix="/health", tags=["Health"])

# Exportation explicite pour l'auto-complétion
__all__ = [
    "router",
    "auth",
    "users", 
    "wallet",
    "keno",
    "lucky",
    "tickets",
    "agent",
    "admin",
    "reports",
    "webhooks",
    "health"
]