# app/models/base.py
"""Modèle de base avec audit trail pour tous les modèles"""

from sqlalchemy import Column, String, DateTime, Boolean, func
from datetime import datetime
import uuid
from app.core.database import Base


class BaseModel(Base):
    """
    Modèle de base abstrait avec champs d'audit.
    Tous les modèles doivent hériter de cette classe.
    """
    __abstract__ = True
    
    id = Column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        nullable=False
    )
    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        server_default=func.now()
    )
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        server_default=func.now()
    )
    created_by = Column(String(36), nullable=True)
    updated_by = Column(String(36), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)
    
    def soft_delete(self, user_id: str = None) -> None:
        """
        Suppression logique (soft delete) pour conservation des données.
        Requis par la conformité LEH.
        """
        self.is_deleted = True
        self.updated_at = datetime.utcnow()
        self.updated_by = user_id
    
    def to_dict(self, exclude: list = None) -> dict:
        """Convertit le modèle en dictionnaire"""
        exclude = exclude or []
        result = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                if hasattr(value, 'isoformat'):
                    value = value.isoformat()
                result[column.name] = value
        return result
    
    def to_json(self, exclude: list = None) -> dict:
        """Alias de to_dict"""
        return self.to_dict(exclude)
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.id}>"