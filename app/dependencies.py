# app/dependencies.py
"""Dépendances FastAPI partagées.

Ré-exporte les dépendances d'authentification définies dans
app.core.security, qui est la seule implémentation réelle. Ce module
existait auparavant comme une seconde implémentation indépendante et
divergente (elle appelait `user_service.get_user_by_id`, une méthode qui
n'a jamais existé sur UserService, et n'a jamais géré l'authentification
par cookie utilisée par le panel admin) : les deux pouvaient donner des
comportements différents selon celle importée par une route. On garde un
seul point de vérité pour éviter que ça se reproduise.
"""

from app.core.security import (
    get_current_user,
    get_current_active_user,
    get_current_admin,
    get_current_agent,
    get_optional_user,
    security,
)

__all__ = [
    "get_current_user",
    "get_current_active_user",
    "get_current_admin",
    "get_current_agent",
    "get_optional_user",
    "security",
]
