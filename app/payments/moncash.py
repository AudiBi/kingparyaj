# app/payments/moncash.py
import httpx
import hmac
import hashlib
from decimal import Decimal
from app.config import settings


class MonCashService:
    """Intégration MonCash (Digicel Haïti)"""
    
    def __init__(self):
        self.api_url = settings.MONCASH_API_URL
        self.merchant_id = settings.MONCASH_MERCHANT_ID
        self.api_key = settings.MONCASH_API_KEY
        self.api_secret = settings.MONCASH_API_SECRET
    
    async def create_payment(self, phone: str, amount: Decimal, reference: str) -> dict:
        """Crée une demande de paiement"""
        # À implémenter selon la documentation MonCash
        pass
    
    async def check_payment_status(self, transaction_id: str) -> dict:
        """Vérifie le statut d'un paiement"""
        pass
    
    async def transfer_to_user(self, phone: str, amount: Decimal, reference: str) -> dict:
        """Envoie de l'argent à un utilisateur"""
        pass