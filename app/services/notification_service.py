# app/services/notification_service.py
"""Service pour les notifications (SMS, Email, Push)"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, update
import redis.asyncio as redis
import httpx

from app.core.logger import get_logger
from app.models.notification import Notification
from app.models.enums import NotificationChannel, NotificationType, NotificationStatus
from app.services.base import BaseService
from app.config import settings


class NotificationService(BaseService[Notification, None, None]):
    """
    Service pour l'envoi de notifications.
    Supporte SMS, Email et In-App.
    """
    
    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        super().__init__(db, Notification)
        self.redis = redis_client
        self.logger = get_logger("NotificationService")
    
    async def create_notification(
        self,
        user_id: str,
        notification_type: NotificationType,
        title: str,
        message: str,
        channel: NotificationChannel,
        payload: Dict[str, Any] = None
    ) -> Notification:
        """Crée une notification"""
        notification = Notification(
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            message=message,
            channel=channel,
            payload=payload or {},
            status=NotificationStatus.PENDING,
            created_at=datetime.utcnow()
        )
        
        self.db.add(notification)
        await self.db.flush()
        
        return notification
    
    async def send_sms(self, phone: str, message: str) -> bool:
        """Envoie un SMS via Twilio"""
        if not settings.SMS_ENABLED:
            self.logger.warning(f"SMS disabled - would send to {phone}: {message}")
            return True
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json",
                    auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                    data={
                        "To": f"+509{phone}",
                        "From": settings.TWILIO_PHONE_NUMBER,
                        "Body": message
                    }
                )
                return response.status_code == 201
        except Exception as e:
            self.logger.error(f"SMS sending failed: {e}")
            return False
    
    async def send_email(self, to_email: str, subject: str, body: str) -> bool:
        """Envoie un email via SMTP"""
        if not settings.EMAIL_ENABLED:
            self.logger.warning(f"Email disabled - would send to {to_email}: {subject}")
            return True
        
        try:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            
            msg = MIMEMultipart()
            msg["From"] = settings.SMTP_FROM
            msg["To"] = to_email
            msg["Subject"] = subject
            
            msg.attach(MIMEText(body, "html"))
            
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.starttls()
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.send_message(msg)
            
            return True
        except Exception as e:
            self.logger.error(f"Email sending failed: {e}")
            return False
    
    async def send_notification(self, notification_id: str) -> bool:
        """Envoie une notification"""
        notification = await self.get_or_raise(notification_id)
        
        if notification.status != NotificationStatus.PENDING:
            return False
        
        success = False
        
        try:
            if notification.channel == NotificationChannel.SMS:
                # Récupérer le téléphone de l'utilisateur
                from app.services.user_service import UserService
                user_service = UserService(self.db, self.redis)
                user = await user_service.get_by_id(notification.user_id)
                
                if user and user.phone:
                    success = await self.send_sms(user.phone, notification.message)
            
            elif notification.channel == NotificationChannel.EMAIL:
                from app.services.user_service import UserService
                user_service = UserService(self.db, self.redis)
                user = await user_service.get_by_id(notification.user_id)
                
                if user and user.email:
                    success = await self.send_email(user.email, notification.title, notification.message)
            
            elif notification.channel == NotificationChannel.IN_APP:
                # In-app - toujours succès
                success = True
            
            if success:
                notification.status = NotificationStatus.SENT
                notification.sent_at = datetime.utcnow()
            else:
                notification.status = NotificationStatus.FAILED
                notification.retry_count += 1
            
            await self.db.flush()
            
        except Exception as e:
            self.logger.error(f"Notification sending failed: {e}")
            notification.status = NotificationStatus.FAILED
            notification.error = str(e)
            notification.retry_count += 1
            await self.db.flush()
        
        return success
    
    async def mark_as_read(self, notification_id: str, user_id: str) -> bool:
        """Marque une notification comme lue"""
        notification = await self.get_or_raise(notification_id)
        
        if notification.user_id != user_id:
            return False
        
        notification.is_read = True
        notification.read_at = datetime.utcnow()
        notification.status = NotificationStatus.READ
        
        await self.db.flush()
        
        return True
    
    async def mark_all_as_read(self, user_id: str) -> int:
        """Marque toutes les notifications d'un utilisateur comme lues"""
        result = await self.db.execute(
            update(Notification)
            .where(
                and_(
                    Notification.user_id == user_id,
                    Notification.is_read == False
                )
            )
            .values(is_read=True, read_at=datetime.utcnow(), status=NotificationStatus.READ)
        )
        
        await self.db.flush()
        
        return result.rowcount
    
    async def get_user_notifications(
        self,
        user_id: str,
        skip: int = 0,
        limit: int = 50,
        unread_only: bool = False
    ) -> List[Notification]:
        """Récupère les notifications d'un utilisateur"""
        query = select(Notification).where(Notification.user_id == user_id)
        
        if unread_only:
            query = query.where(Notification.is_read == False)
        
        query = query.order_by(Notification.created_at.desc()).offset(skip).limit(limit)
        
        result = await self.db.execute(query)
        return result.scalars().all()
    
    async def get_unread_count(self, user_id: str) -> int:
        """Récupère le nombre de notifications non lues"""
        result = await self.db.execute(
            select(func.count(Notification.id))
            .where(
                and_(
                    Notification.user_id == user_id,
                    Notification.is_read == False
                )
            )
        )
        return result.scalar() or 0
    
    # ========== Notifications prédéfinies ==========
    
    async def notify_bet_won(
        self,
        user_id: str,
        bet_id: str,
        amount: float,
        game: str
    ) -> None:
        """Notifie un gain de pari"""
        await self.create_notification(
            user_id=user_id,
            notification_type=NotificationType.BET_WON,
            title="🎉 Vous avez gagné !",
            message=f"Félicitations ! Vous avez gagné {amount} HTG au {game}.",
            channel=NotificationChannel.IN_APP,
            payload={"bet_id": bet_id, "amount": amount, "game": game}
        )
    
    async def notify_deposit_confirmed(
        self,
        user_id: str,
        amount: float,
        method: str
    ) -> None:
        """Notifie un dépôt confirmé"""
        await self.create_notification(
            user_id=user_id,
            notification_type=NotificationType.DEPOSIT_CONFIRMED,
            title="✅ Dépôt confirmé",
            message=f"Votre dépôt de {amount} HTG via {method} a été confirmé.",
            channel=NotificationChannel.IN_APP,
            payload={"amount": amount, "method": method}
        )
    
    async def notify_draw_result(
        self,
        user_id: str,
        draw_id: str,
        draw_number: int,
        results: List[int]
    ) -> None:
        """Notifie les résultats d'un tirage"""
        await self.create_notification(
            user_id=user_id,
            notification_type=NotificationType.DRAW_RESULT,
            title=f"🎲 Résultat tirage Keno #{draw_number}",
            message=f"Les numéros gagnants sont: {', '.join(map(str, results))}",
            channel=NotificationChannel.IN_APP,
            payload={"draw_id": draw_id, "draw_number": draw_number, "numbers": results}
        )
    
    async def notify_promotion(
        self,
        user_id: str,
        promotion_name: str,
        bonus_amount: float
    ) -> None:
        """Notifie une promotion reçue"""
        await self.create_notification(
            user_id=user_id,
            notification_type=NotificationType.PROMOTION,
            title=f"🎁 Promotion: {promotion_name}",
            message=f"Vous avez reçu {bonus_amount} HTG de bonus !",
            channel=NotificationChannel.IN_APP,
            payload={"promotion_name": promotion_name, "bonus_amount": bonus_amount}
        )