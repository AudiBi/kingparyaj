# app/services/fairness.py
"""Équité vérifiable (« provably fair ») commune à tous les jeux.

Principe (engagement / révélation) :
1. avant le pari, le serveur tire un seed secret et publie seulement son
   empreinte sha256(seed) ;
2. le résultat est calculé de façon déterministe à partir du seed ;
3. après le jeu, le seed est révélé : n'importe qui peut vérifier que
   sha256(seed) correspond à l'empreinte publiée et recalculer le résultat.
Le serveur ne peut donc pas choisir le résultat après avoir vu le pari.

Utilisé par Horse Races et Keno.
"""

import hashlib
import hmac
import secrets


def new_server_seed() -> str:
    """Seed secret : 256 bits issus du générateur cryptographique du système."""
    return secrets.token_hex(32)


def seed_hash(server_seed: str) -> str:
    return hashlib.sha256(server_seed.encode()).hexdigest()


def hmac_uniform(server_seed: str, message: str) -> float:
    """Nombre uniforme dans [0, 1) dérivé du seed et d'un message (56 bits)."""
    digest = hmac.new(server_seed.encode(), message.encode(), hashlib.sha256).digest()
    return int.from_bytes(digest[:7], "big") / float(1 << 56)
