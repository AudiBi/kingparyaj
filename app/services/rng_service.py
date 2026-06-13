# app/services/rng_service.py
"""Service de génération aléatoire sécurisée"""

import secrets
import hashlib
import hmac
from typing import List, Tuple
from datetime import datetime

from app.core.logger import get_logger


class RNGService:
    """
    Service de génération aléatoire cryptographiquement sécurisée.
    Utilise secrets pour un vrai aléatoire.
    """
    
    def __init__(self):
        self.logger = get_logger("RNGService")
    
    def generate_keno_numbers(self) -> List[int]:
        """
        Génère 20 numéros uniques entre 1 et 80.
        Utilise Fisher-Yates shuffle cryptographique.
        """
        numbers = list(range(1, 81))
        
        # Fisher-Yates shuffle avec secrets.randbelow
        for i in range(len(numbers) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            numbers[i], numbers[j] = numbers[j], numbers[i]
        
        return sorted(numbers[:20])
    
    def generate_lucky_numbers(self, min_num: int = 1, max_num: int = 10, count: int = 3) -> List[int]:
        """Génère des numéros aléatoires pour Lucky Numbers"""
        numbers = list(range(min_num, max_num + 1))
        
        for i in range(len(numbers) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            numbers[i], numbers[j] = numbers[j], numbers[i]
        
        return sorted(numbers[:count])
    
    def weighted_random(self, items: List[Tuple[any, float]]) -> any:
        """
        Sélection aléatoire pondérée.
        items: liste de (valeur, poids)
        """
        total_weight = sum(weight for _, weight in items)
        roll = secrets.randbelow(int(total_weight * 100)) / 100
        
        cumulative = 0
        for value, weight in items:
            cumulative += weight
            if roll < cumulative:
                return value
        
        return items[0][0]
    
    def generate_seed(self) -> str:
        """Génère une seed aléatoire pour les preuves d'équité"""
        return secrets.token_hex(32)
    
    def generate_verification_hash(self, seed: str, stake: float, timestamp: str) -> str:
        """Génère un hash de vérification pour prouver l'équité"""
        return hashlib.sha256(f"{seed}{stake}{timestamp}".encode()).hexdigest()
    
    def generate_otp(self, length: int = 6) -> str:
        """Génère un code OTP à chiffres"""
        return ''.join(str(secrets.randbelow(10)) for _ in range(length))
    
    def generate_referral_code(self) -> str:
        """Génère un code de parrainage unique"""
        return secrets.token_hex(4).upper()
    
    def generate_transaction_reference(self, prefix: str = "TX") -> str:
        """Génère une référence de transaction unique"""
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        random = secrets.token_hex(4).upper()
        return f"{prefix}-{timestamp}-{random}"