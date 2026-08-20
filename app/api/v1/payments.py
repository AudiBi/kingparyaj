# app/api/v1/payments.py
"""Webhooks des passerelles de paiement (MonCash/NatCash) + endpoint de
simulation (hors production) pour déclencher leur issue sans vraie
passerelle connectée (cf. app/payments/base.py : aucune clé API n'est
configurée aujourd'hui, MonCash/NatCash sont simulés)."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

from app.config import settings
from app.core.database import get_db
from app.core.redis_client import get_redis
from app.payments.base import get_gateway
from app.payments.webhooks import WebhookEvent
from app.services.wallet_service import WalletService

router = APIRouter(prefix="/payments", tags=["Payments"])

_PROVIDERS = ("moncash", "natcash")


class WebhookPayload(BaseModel):
    event: WebhookEvent
    external_reference: str
    reason: Optional[str] = None


@router.post("/{provider}/webhook")
async def payment_webhook(
    provider: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Reçoit la confirmation/l'échec d'un dépôt ou d'un retrait.

    Format d'événement propre à cette application (cf.
    app/payments/webhooks.py) : le jour où une vraie passerelle est
    branchée, normaliser son payload vers ces mêmes événements ici plutôt
    que de changer WalletService.
    """
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=404, detail="Fournisseur inconnu")

    gateway = get_gateway(provider)
    body = await request.body()
    signature = request.headers.get("X-Signature", "")
    if not gateway.verify_webhook_signature(payload=body, signature=signature):
        raise HTTPException(status_code=401, detail="Signature invalide")

    payload = WebhookPayload.model_validate_json(body)
    wallet_service = WalletService(db, redis_client)

    if payload.event == WebhookEvent.PAYMENT_COMPLETED:
        transaction = await wallet_service.confirm_deposit(payload.external_reference)
    elif payload.event == WebhookEvent.PAYMENT_FAILED:
        transaction = await wallet_service.fail_deposit(payload.external_reference, reason=payload.reason)
    elif payload.event == WebhookEvent.PAYOUT_COMPLETED:
        transaction = await wallet_service.confirm_withdrawal(payload.external_reference)
    elif payload.event == WebhookEvent.PAYOUT_FAILED:
        transaction = await wallet_service.fail_withdrawal(payload.external_reference, reason=payload.reason)
    else:  # pragma: no cover - exhaustif vu l'enum WebhookEvent
        raise HTTPException(status_code=400, detail="Événement inconnu")

    await db.commit()
    return {"received": True, "reference": transaction.reference, "status": transaction.status.value}


class SimulateOutcomeRequest(BaseModel):
    external_reference: str
    kind: Literal["deposit", "withdrawal"]
    outcome: Literal["success", "failure"] = "success"
    reason: Optional[str] = None


@router.post("/{provider}/simulate")
async def simulate_payment_outcome(
    provider: str,
    data: SimulateOutcomeRequest,
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
):
    """Déclenche à la main l'issue d'un paiement/virement simulé : produit
    exactement le même effet que le webhook ci-dessus, sans dépendre d'une
    vraie passerelle MonCash/NatCash. Absent en production.
    """
    if settings.ENVIRONMENT == "production":
        raise HTTPException(status_code=404)
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=404, detail="Fournisseur inconnu")

    wallet_service = WalletService(db, redis_client)

    if data.kind == "deposit":
        if data.outcome == "success":
            transaction = await wallet_service.confirm_deposit(data.external_reference)
        else:
            transaction = await wallet_service.fail_deposit(data.external_reference, reason=data.reason)
    else:
        if data.outcome == "success":
            transaction = await wallet_service.confirm_withdrawal(data.external_reference)
        else:
            transaction = await wallet_service.fail_withdrawal(data.external_reference, reason=data.reason)

    await db.commit()
    return {"reference": transaction.reference, "status": transaction.status.value}
