# app/services/base.py
"""Service de base avec patterns communs"""

import logging
from typing import Optional, TypeVar, Generic, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, delete
from sqlalchemy.sql import Select

from app.core.exceptions import AppException, NotFoundException
from app.core.logger import get_logger

ModelType = TypeVar("ModelType")
CreateSchemaType = TypeVar("CreateSchemaType")
UpdateSchemaType = TypeVar("UpdateSchemaType")


class BaseService(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    """
    Service de base avec méthodes CRUD génériques.
    Tous les services doivent hériter de cette classe.
    """
    
    def __init__(self, db: AsyncSession, model_class: type[ModelType]):
        self.db = db
        self.model_class = model_class
        self.logger = get_logger(self.__class__.__name__)
    
    async def get_by_id(self, id: str) -> Optional[ModelType]:
        """Récupère un élément par son ID"""
        result = await self.db.execute(
            select(self.model_class).where(
                self.model_class.id == id,
                self.model_class.is_deleted == False
            )
        )
        return result.scalar_one_or_none()
    
    async def get_or_raise(self, id: str) -> ModelType:
        """Récupère un élément par son ID ou lève une exception"""
        item = await self.get_by_id(id)
        if not item:
            raise NotFoundException(
                resource=self.model_class.__name__,
                identifier=id
            )
        return item
    
    async def get_all(
        self,
        skip: int = 0,
        limit: int = 100,
        order_by: str = "created_at",
        descending: bool = True
    ) -> List[ModelType]:
        """Récupère tous les éléments avec pagination"""
        query = select(self.model_class).where(self.model_class.is_deleted == False)
        
        # Ordre
        order_column = getattr(self.model_class, order_by, self.model_class.created_at)
        if descending:
            query = query.order_by(order_column.desc())
        else:
            query = query.order_by(order_column.asc())
        
        # Pagination
        query = query.offset(skip).limit(limit)
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    async def count(self, filters: Optional[dict] = None) -> int:
        """Compte le nombre d'éléments"""
        query = select(func.count()).select_from(self.model_class).where(
            self.model_class.is_deleted == False
        )
        
        if filters:
            for key, value in filters.items():
                if hasattr(self.model_class, key) and value is not None:
                    query = query.where(getattr(self.model_class, key) == value)
        
        result = await self.db.execute(query)
        return result.scalar() or 0
    
    async def create(self, data: CreateSchemaType, user_id: str = None) -> ModelType:
        """Crée un nouvel élément"""
        try:
            obj = self.model_class(**data.model_dump())
            if user_id:
                obj.created_by = user_id
            self.db.add(obj)
            await self.db.flush()
            self.logger.info(f"Created {self.model_class.__name__}: {obj.id}")
            return obj
        except Exception as e:
            self.logger.error(f"Error creating {self.model_class.__name__}: {e}")
            raise
    
    async def update(
        self,
        id: str,
        data: UpdateSchemaType,
        user_id: str = None
    ) -> ModelType:
        """Met à jour un élément"""
        obj = await self.get_or_raise(id)
        
        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(obj, key, value)
        
        if user_id:
            obj.updated_by = user_id
        obj.updated_at = datetime.utcnow()
        
        await self.db.flush()
        self.logger.info(f"Updated {self.model_class.__name__}: {id}")
        
        return obj
    
    async def delete(self, id: str, user_id: str = None, soft: bool = True) -> bool:
        """Supprime un élément (soft delete par défaut)"""
        if soft:
            obj = await self.get_or_raise(id)
            obj.soft_delete(user_id)
            await self.db.flush()
        else:
            await self.db.execute(
                delete(self.model_class).where(self.model_class.id == id)
            )
        
        self.logger.info(f"Deleted {self.model_class.__name__}: {id}")
        return True
    
    async def exists(self, **filters) -> bool:
        """Vérifie si un élément existe"""
        query = select(self.model_class.id).where(
            self.model_class.is_deleted == False
        )
        for key, value in filters.items():
            if hasattr(self.model_class, key):
                query = query.where(getattr(self.model_class, key) == value)
        
        result = await self.db.execute(query.limit(1))
        return result.scalar_one_or_none() is not None
    
    def _apply_filters(self, query: Select, filters: dict) -> Select:
        """Applique des filtres à une requête"""
        for key, value in filters.items():
            if value is not None and hasattr(self.model_class, key):
                query = query.where(getattr(self.model_class, key) == value)
        return query