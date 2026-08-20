# app/payments/webhooks.py
"""Forme des événements webhook envoyés par les passerelles de paiement.

Format propre à cette application (les vrais MonCash/NatCash ont chacun
leur propre format) : à adapter le jour où on branche un vrai fournisseur,
en normalisant son payload vers ces mêmes événements dans le routeur
(app/api/v1/payments.py) pour ne pas avoir à toucher WalletService.
"""

from enum import Enum


class WebhookEvent(str, Enum):
    PAYMENT_COMPLETED = "payment.completed"
    PAYMENT_FAILED = "payment.failed"
    PAYOUT_COMPLETED = "payout.completed"
    PAYOUT_FAILED = "payout.failed"
