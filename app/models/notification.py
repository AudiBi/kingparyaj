# app/models/notification.py
"""Modèles pour les notifications (SMS, Email, Push)"""

from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, DateTime, ForeignKey, 
    Enum, Text, JSON, Boolean, Index
)
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
from app.models.enums import NotificationChannel, NotificationType, NotificationStatus


class Notification(BaseModel):
    """
    Notification envoyée à un utilisateur.
    Supporte SMS, Email, Push et In-App.
    """
    __tablename__ = "notifications"
    __table_args__ = (
        Index("idx_notifications_user_id", "user_id"),
        Index("idx_notifications_notification_type", "notification_type"),  # Renommé de 'type'
        Index("idx_notifications_channel", "channel"),
        Index("idx_notifications_status", "status"),
        Index("idx_notifications_created_at", "created_at"),
        Index("idx_notifications_is_read", "is_read"),
    )
    
    # ========== Clés étrangères ==========
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    
    # ========== Contenu ==========
    notification_type = Column(Enum(NotificationType), nullable=False)  # Renommé de 'type'
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    payload = Column(JSON, default={})  # Renommé de 'data'
    
    # ========== Canal ==========
    channel = Column(Enum(NotificationChannel), nullable=False)
    
    # ========== Statut ==========
    status = Column(Enum(NotificationStatus), default=NotificationStatus.PENDING, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False)
    read_at = Column(DateTime, nullable=True)
    
    # ========== Métadonnées d'envoi ==========
    sent_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    
    # ========== Pour tracking ==========
    external_message_id = Column(String(100), nullable=True)
    error = Column(Text, nullable=True)
    
    # ========== Retry ==========
    retry_count = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=3, nullable=False)
    
    # ========== Relations ==========
    user = relationship("User", back_populates="notifications")
    
    # ========== Méthodes ==========
    def mark_sent(self) -> None:
        """Marque la notification comme envoyée"""
        self.status = NotificationStatus.SENT
        self.sent_at = datetime.utcnow()
    
    def mark_delivered(self) -> None:
        """Marque la notification comme délivrée"""
        self.status = NotificationStatus.DELIVERED
        self.delivered_at = datetime.utcnow()
    
    def mark_failed(self, error_msg: str) -> None:
        """Marque la notification comme échouée"""
        self.status = NotificationStatus.FAILED
        self.error = error_msg
        self.retry_count += 1
    
    def mark_read(self) -> None:
        """Marque la notification comme lue"""
        self.is_read = True
        self.read_at = datetime.utcnow()
    
    def can_retry(self) -> bool:
        """Vérifie si on peut réessayer l'envoi"""
        return self.retry_count < self.max_retries
    
    def __repr__(self) -> str:
        return f"<Notification user={self.user_id} type={self.notification_type} status={self.status}>"