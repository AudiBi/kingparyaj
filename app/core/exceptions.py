# app/core/exceptions.py
from fastapi import HTTPException, status


class AppException(HTTPException):
    """Exception de base pour l'application"""
    def __init__(self, status_code: int, detail: str, code: str = None):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


class NotFoundException(AppException):
    """Ressource non trouvée"""
    def __init__(self, resource: str, identifier: str = None):
        detail = f"{resource} non trouvé"
        if identifier:
            detail += f": {identifier}"
        super().__init__(status.HTTP_404_NOT_FOUND, detail, "NOT_FOUND")


class ValidationException(AppException):
    """Erreur de validation"""
    def __init__(self, detail: str):
        super().__init__(status.HTTP_400_BAD_REQUEST, detail, "VALIDATION_ERROR")


class InsufficientBalanceException(AppException):
    """Solde insuffisant"""
    def __init__(self, required: float, available: float):
        detail = f"Solde insuffisant. Nécessaire: {required} HTG, Disponible: {available} HTG"
        super().__init__(status.HTTP_400_BAD_REQUEST, detail, "INSUFFICIENT_BALANCE")


class GameException(AppException):
    """Erreur pendant le jeu"""
    def __init__(self, detail: str):
        super().__init__(status.HTTP_400_BAD_REQUEST, detail, "GAME_ERROR")


class DrawInProgressException(AppException):
    """Tirage déjà en cours"""
    def __init__(self):
        super().__init__(status.HTTP_409_CONFLICT, "Un tirage est déjà en cours", "DRAW_IN_PROGRESS")


class UnauthorizedException(AppException):
    """Non autorisé"""
    def __init__(self, detail: str = "Non autorisé"):
        super().__init__(status.HTTP_401_UNAUTHORIZED, detail, "UNAUTHORIZED")