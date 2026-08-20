# app/payments/base.py
"""Abstraction commune aux passerelles de paiement mobile money (MonCash,
NatCash).

Aucune des deux n'a de clés API configurées (voir .env : MONCASH_API_KEY,
NATCASH_API_KEY, ... sont vides) : `SimulatedGateway` fournit donc une
implémentation simulée mais réaliste — même interface, même cycle de vie
(paiement en attente puis confirmé/échoué via webhook) qu'aurait une vraie
intégration HTTP. Pour brancher les vrais appels réseau plus tard, il suffit
d'écraser `create_payment`/`transfer_to_user` dans une sous-classe (ou de
remplacer le corps de `SimulatedGateway`) sans toucher au reste de
l'application : WalletService et le routeur webhook ne connaissent que
`PaymentGateway`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class PaymentInitiation:
    """Résultat de la création d'une demande de paiement (dépôt)."""

    external_reference: str
    status: str  # "pending" | "failed"
    payment_url: Optional[str] = None


@dataclass
class PayoutResult:
    """Résultat d'une demande de virement vers l'utilisateur (retrait)."""

    external_reference: str
    status: str  # "pending" | "completed" | "failed"


class PaymentGateway(ABC):
    """Interface qu'implémentent MonCashGateway et NatCashGateway."""

    provider: str

    @abstractmethod
    async def create_payment(
        self, *, amount: Decimal, reference: str, phone: Optional[str] = None
    ) -> PaymentInitiation:
        """Initie un dépôt. Ne doit jamais créditer directement : la
        confirmation arrive de façon asynchrone via webhook."""
        raise NotImplementedError

    @abstractmethod
    async def transfer_to_user(
        self, *, amount: Decimal, phone: str, reference: str
    ) -> PayoutResult:
        """Initie un virement vers l'utilisateur (retrait)."""
        raise NotImplementedError

    @abstractmethod
    def verify_webhook_signature(self, *, payload: bytes, signature: str) -> bool:
        """Vérifie qu'un webhook entrant provient bien du fournisseur."""
        raise NotImplementedError


class SimulatedGateway(PaymentGateway):
    """Passerelle simulée : aucun appel réseau. Les paiements/virements sont
    créés `pending` et ne changent d'état que lorsqu'on déclenche
    explicitement leur issue — via le webhook signé (`sign()` génère une
    signature valide, comme le ferait le vrai fournisseur) ou via l'endpoint
    `/api/v1/payments/{provider}/simulate` réservé au hors-production.
    """

    def __init__(self, webhook_secret: Optional[str] = None):
        # Secret de secours utilisable même sans configuration .env, pour
        # que le simulateur fonctionne "out of the box" en développement.
        self._webhook_secret = webhook_secret or f"simulated-{self.provider}-secret"

    async def create_payment(
        self, *, amount: Decimal, reference: str, phone: Optional[str] = None
    ) -> PaymentInitiation:
        external_reference = f"{self.provider.upper()}-{secrets.token_hex(8).upper()}"
        return PaymentInitiation(
            external_reference=external_reference,
            status="pending",
            payment_url=f"/api/v1/payments/{self.provider}/simulate?external_reference={external_reference}",
        )

    async def transfer_to_user(
        self, *, amount: Decimal, phone: str, reference: str
    ) -> PayoutResult:
        external_reference = f"{self.provider.upper()}-PAYOUT-{secrets.token_hex(8).upper()}"
        return PayoutResult(external_reference=external_reference, status="pending")

    def sign(self, payload: bytes) -> str:
        """Signe un corps de webhook comme le ferait le vrai fournisseur
        (HMAC-SHA256 sur le secret configuré) — utilisé par les tests et par
        le simulateur pour produire des webhooks vérifiables."""
        return hmac.new(self._webhook_secret.encode(), payload, hashlib.sha256).hexdigest()

    def verify_webhook_signature(self, *, payload: bytes, signature: str) -> bool:
        if not signature:
            return False
        expected = self.sign(payload)
        return hmac.compare_digest(expected, signature)


_GATEWAYS: dict[str, PaymentGateway] = {}


def get_gateway(provider: str) -> PaymentGateway:
    """Retourne l'instance (mise en cache) de la passerelle pour ce
    fournisseur. `provider` accepte aussi bien "moncash"/"natcash" que les
    valeurs des enums PaymentMethod (mêmes chaînes)."""
    provider = str(provider).lower()
    if provider not in _GATEWAYS:
        if provider == "moncash":
            from app.payments.moncash import MonCashGateway

            _GATEWAYS[provider] = MonCashGateway()
        elif provider == "natcash":
            from app.payments.natcash import NatCashGateway

            _GATEWAYS[provider] = NatCashGateway()
        else:
            raise ValueError(f"Aucune passerelle de paiement pour '{provider}'")
    return _GATEWAYS[provider]
