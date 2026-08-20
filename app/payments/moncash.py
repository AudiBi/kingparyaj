# app/payments/moncash.py
"""Intégration MonCash (Digicel Haïti).

Simulée pour l'instant (cf. app/payments/base.py) : MONCASH_API_KEY/
MONCASH_API_SECRET sont vides dans .env, il n'y a donc pas de vrai
environnement à appeler. Quand des identifiants seront disponibles,
remplacer le corps de create_payment/transfer_to_user par de vrais appels
httpx vers settings.MONCASH_API_URL (OAuth2 client_credentials puis
CreatePayment/RetrieveTransactionPayment côté MonCash) sans changer
l'interface PaymentGateway ni le reste de l'application.
"""

from app.config import settings
from app.payments.base import SimulatedGateway


class MonCashGateway(SimulatedGateway):
    provider = "moncash"

    def __init__(self):
        super().__init__(webhook_secret=settings.MONCASH_WEBHOOK_SECRET)
