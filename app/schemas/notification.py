# app/schemas/notification.py
"""Schémas pour les notifications"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class NotificationResponse(BaseModel):
    """Réponse notification"""
    id: str
    notification_type: str
    title: str
    message: str
    payload: Optional[Dict[str, Any]]
    channel: str
    status: str
    is_read: bool
    created_at: datetime
    read_at: Optional[datetime]
    sent_at: Optional[datetime]
    
    class Config:
        from_attributes = True


class NotificationListResponse(BaseModel):
    """Liste des notifications"""
    items: List[NotificationResponse]
    total: int
    page: int
    page_size: int
    unread_count: int


class NotificationMarkRead(BaseModel):
    """Marquer notification comme lue"""
    notification_ids: List[str]