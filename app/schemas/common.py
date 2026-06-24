# app/schemas/common.py
"""Schémas communs pour l'API"""

from pydantic import BaseModel, Field
from typing import Generic, TypeVar, List, Optional, Any
from datetime import datetime

T = TypeVar("T")


class PaginationParams(BaseModel):
    """Paramètres de pagination"""
    page: int = Field(default=1, ge=1, description="Numéro de page")
    page_size: int = Field(default=20, ge=1, le=100, description="Nombre d'éléments par page")
    
    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class PaginatedResponse(BaseModel, Generic[T]):
    """Réponse paginée générique"""
    items: List[T]
    total: int = Field(..., description="Nombre total d'éléments")
    page: int = Field(..., description="Page actuelle")
    page_size: int = Field(..., description="Éléments par page")
    total_pages: int = Field(..., description="Nombre total de pages")
    
    @classmethod
    def create(cls, items: List[T], total: int, params: PaginationParams) -> "PaginatedResponse":
        return cls(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
            total_pages=(total + params.page_size - 1) // params.page_size
        )


# ========== RÉPONSES STANDARD ==========

class SuccessResponse(BaseModel):
    """Réponse de succès standardisée"""
    success: bool = Field(default=True)
    message: str = Field(..., description="Message de confirmation")
    data: Optional[Any] = Field(default=None, description="Données optionnelles")
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class MessageResponse(BaseModel):
    """Réponse simple avec message"""
    message: str = Field(..., description="Message de confirmation")
    success: bool = Field(default=True, description="Succès ou échec")


class ErrorResponse(BaseModel):
    """Réponse d'erreur standardisée"""
    success: bool = Field(default=False)
    error: str = Field(..., description="Code d'erreur")
    message: str = Field(..., description="Message d'erreur détaillé")
    details: Optional[Any] = Field(default=None, description="Détails supplémentaires")
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# ========== HEALTH CHECK ==========

class HealthResponse(BaseModel):
    """Réponse de health check"""
    status: str = Field(..., description="Statut (ok, degraded, down)")
    version: str = Field(..., description="Version de l'application")
    environment: str = Field(..., description="Environnement")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    checks: Optional[dict] = Field(default=None, description="Résultats des vérifications")


# ========== FILTRES ==========

class DateRangeFilter(BaseModel):
    """Filtre par plage de dates"""
    start_date: Optional[datetime] = Field(default=None, description="Date de début")
    end_date: Optional[datetime] = Field(default=None, description="Date de fin")


class SortParams(BaseModel):
    """Paramètres de tri"""
    sort_by: str = Field(default="created_at", description="Champ de tri")
    sort_desc: bool = Field(default=True, description="Tri décroissant")


# ========== AUTRES ==========

class IDResponse(BaseModel):
    """Réponse avec ID"""
    id: str
    success: bool = True
    message: Optional[str] = None


class EmptyResponse(BaseModel):
    """Réponse vide"""
    success: bool = True
    message: Optional[str] = None