# app/payments/natcash.py
"""Intégration NatCash (NatCom Haïti).

Simulée pour l'instant (cf. app/payments/base.py) : NATCASH_API_KEY est vide
dans .env. Remplacer le corps de create_payment/transfer_to_user par de
vrais appels httpx vers settings.NATCASH_API_URL une fois des identifiants
disponibles, sans changer l'interface PaymentGateway ni le reste de
l'application.
"""

from app.config import settings
from app.payments.base import SimulatedGateway


class NatCashGateway(SimulatedGateway):
    provider = "natcash"

    def __init__(self):
        super().__init__(webhook_secret=settings.NATCASH_WEBHOOK_SECRET)
