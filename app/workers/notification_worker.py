# app/workers/notification_worker.py
"""Worker pour l'envoi de notifications - VERSION COMPLÈTE (Keno + Lucky)"""

from celery import Task
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, and_, func, or_
from datetime import datetime, timedelta
from typing import Optional, List
import httpx
import asyncio
import logging

from app.config import settings
from app.core.redis_client import redis_client
from app.core.logger import get_logger
from app.workers.celery import celery_app
from app.models.notification import Notification, NotificationChannel, NotificationType, NotificationStatus
from app.models.user import User
from app.models.ticket import Ticket
from app.models.promotion import Promotion, PromotionStatus
from app.models.lucky import LuckyPlay

logger = get_logger(__name__)

# Connexion à la base de données
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=5,
    max_overflow=10,
)
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class NotificationTask(Task):
    """Tâche de notification avec gestion d'erreurs"""
    
    async def get_db(self):
        return AsyncSessionLocal()
    
    async def _run(self, *args, **kwargs):
        try:
            return await super()._run(*args, **kwargs)
        except Exception as e:
            logger.error(f"Notification task failed: {e}", exc_info=True)
            raise


# ==================== TÂCHES DE BASE ====================

@celery_app.task(
    bind=True,
    base=NotificationTask,
    name="app.workers.notification_worker.send_sms_notification",
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True
)
def send_sms_notification(self, phone: str, message: str, template_data: dict = None):
    """Envoie un SMS à un numéro haïtien"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_send_sms_notification_async(phone, message, template_data))
    else:
        return loop.run_until_complete(_send_sms_notification_async(phone, message, template_data))


async def _send_sms_notification_async(phone: str, message: str, template_data: dict = None):
    """Logique asynchrone d'envoi SMS"""
    logger.info(f"📱 Envoi SMS à {phone}")
    
    phone = phone.replace(' ', '').replace('-', '')
    if not phone.startswith('+509'):
        phone = f"+509{phone}"
    
    if template_data:
        try:
            message = message.format(**template_data)
        except KeyError as e:
            logger.warning(f"Template key missing: {e}")
    
    try:
        if settings.SMS_PROVIDER == "twilio":
            from twilio.rest import Client
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            
            message_obj = await client.messages.create_async(
                body=message,
                from_=settings.TWILIO_PHONE_NUMBER,
                to=phone
            )
            
            logger.info(f"✅ SMS envoyé à {phone}: {message_obj.sid}")
            return {"success": True, "sid": message_obj.sid}
        
        elif settings.SMS_PROVIDER == "digicel":
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://api.digicelhaiti.com/v1/sms",
                    json={
                        "phone": phone,
                        "message": message,
                        "sender": "PARIER",
                    },
                    headers={"Authorization": f"Bearer {settings.DIGICEL_SMS_API_KEY}"}
                )
                
                if response.status_code == 200:
                    logger.info(f"✅ SMS envoyé à {phone} via Digicel")
                    return {"success": True, "data": response.json()}
                else:
                    logger.error(f"❌ Digicel SMS failed: {response.text}")
                    return {"success": False, "error": response.text}
        else:
            logger.info(f"📝 SMS (simulé) à {phone}: {message}")
            return {"success": True, "simulated": True}
            
    except Exception as e:
        logger.error(f"❌ Erreur envoi SMS à {phone}: {e}")
        raise


@celery_app.task(
    bind=True,
    base=NotificationTask,
    name="app.workers.notification_worker.send_email_notification",
    max_retries=3,
    retry_backoff=True
)
def send_email_notification(self, email: str, subject: str, body: str, html_body: str = None):
    """Envoie un email"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_send_email_notification_async(email, subject, body, html_body))
    else:
        return loop.run_until_complete(_send_email_notification_async(email, subject, body, html_body))


async def _send_email_notification_async(email: str, subject: str, body: str, html_body: str = None):
    """Logique asynchrone d'envoi email"""
    logger.info(f"📧 Envoi email à {email}")
    
    try:
        if settings.EMAIL_ENABLED:
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = settings.SMTP_FROM
            msg['To'] = email
            
            part1 = MIMEText(body, 'plain')
            msg.attach(part1)
            
            if html_body:
                part2 = MIMEText(html_body, 'html')
                msg.attach(part2)
            
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.starttls()
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_FROM, email, msg.as_string())
            
            logger.info(f"✅ Email envoyé à {email}")
            return {"success": True}
        else:
            logger.info(f"📝 Email (simulé) à {email}: {subject}")
            return {"success": True, "simulated": True}
            
    except Exception as e:
        logger.error(f"❌ Erreur envoi email à {email}: {e}")
        raise


# ==================== KENO - NOTIFICATIONS ====================

@celery_app.task(
    name="app.workers.notification_worker.send_bet_confirmation",
    max_retries=3
)
def send_bet_confirmation(phone: str, bet_id: str, stake: float, game: str = "Keno"):
    """Envoie une confirmation de pari Keno"""
    message = (
        f"✅ Pari {game} confirmé!\n"
        f"ID: {bet_id[:8]}\n"
        f"Mise: {stake} HTG\n"
        f"Bonne chance! 🍀\n"
        f"Parier Keno Haïti"
    )
    return send_sms_notification.delay(phone, message)


@celery_app.task(
    name="app.workers.notification_worker.send_win_notification",
    max_retries=3
)
def send_win_notification(phone: str, name: str, amount: float, game: str = "Keno"):
    """Envoie une notification de gain Keno"""
    message = (
        f"🎉 FÉLICITATIONS {name}!\n"
        f"Vous avez gagné {amount:,.0f} HTG au {game}!\n"
        f"Le montant a été crédité sur votre compte.\n"
        f"Parier Keno Haïti"
    )
    return send_sms_notification.delay(phone, message)


# ==================== LUCKY - NOTIFICATIONS ====================

@celery_app.task(
    name="app.workers.notification_worker.send_lucky_win_notification",
    max_retries=3
)
def send_lucky_win_notification(phone: str, name: str, amount: float, segment: str):
    """Envoie une notification de gain au Lucky Wheel"""
    message = (
        f"🎉 FÉLICITATIONS {name}!\n"
        f"Vous avez gagné {amount:,.0f} HTG au Lucky Wheel!\n"
        f"Segment: {segment}\n"
        f"Le montant a été crédité sur votre compte.\n"
        f"Parier Keno Haïti"
    )
    return send_sms_notification.delay(phone, message)


@celery_app.task(
    name="app.workers.notification_worker.send_lucky_daily_reminder",
    max_retries=2
)
def send_lucky_daily_reminder():
    """
    Envoie un rappel quotidien pour jouer au Lucky Wheel.
    Aux joueurs qui n'ont pas joué depuis 3 jours.
    """
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_send_lucky_daily_reminder_async())
    else:
        return loop.run_until_complete(_send_lucky_daily_reminder_async())


async def _send_lucky_daily_reminder_async():
    """Logique d'envoi de rappel Lucky"""
    logger.info("🎡 Envoi des rappels Lucky...")
    
    try:
        async with AsyncSessionLocal() as db:
            three_days_ago = datetime.utcnow() - timedelta(days=3)
            
            users_result = await db.execute(
                select(User)
                .where(
                    and_(
                        User.is_active == True,
                        User.is_locked == False,
                        User.phone.isnot(None)
                    )
                )
                .limit(100)
            )
            users = users_result.scalars().all()
            
            for user in users:
                plays_result = await db.execute(
                    select(LuckyPlay)
                    .where(
                        and_(
                            LuckyPlay.user_id == user.id,
                            LuckyPlay.played_at > three_days_ago
                        )
                    )
                    .limit(1)
                )
                recent_play = plays_result.scalar_one_or_none()
                
                if not recent_play:
                    message = (
                        f"🎡 Le Lucky Wheel vous attend!\n"
                        f"Faites tourner la roue et gagnez jusqu'à 500x!\n"
                        f"Jouez maintenant sur Parier Keno Haïti"
                    )
                    send_sms_notification.delay(user.phone, message)
                    
            logger.info(f"✅ Rappels Lucky envoyés")
            
    except Exception as e:
        logger.error(f"❌ Erreur envoi rappels Lucky: {e}")
        raise


# ==================== NOTIFICATIONS COMMUNES ====================

@celery_app.task(
    name="app.workers.notification_worker.send_deposit_confirmation",
    max_retries=3
)
def send_deposit_confirmation(phone: str, amount: float, method: str):
    """Envoie une confirmation de dépôt"""
    message = (
        f"💰 Dépôt confirmé!\n"
        f"Montant: {amount:,.0f} HTG\n"
        f"Méthode: {method}\n"
        f"Nouveau solde disponible.\n"
        f"Parier Keno Haïti"
    )
    return send_sms_notification.delay(phone, message)


@celery_app.task(
    name="app.workers.notification_worker.send_withdrawal_confirmation",
    max_retries=3
)
def send_withdrawal_confirmation(phone: str, amount: float, method: str):
    """Envoie une confirmation de retrait"""
    message = (
        f"💸 Retrait confirmé!\n"
        f"Montant: {amount:,.0f} HTG\n"
        f"Méthode: {method}\n"
        f"Veuillez vérifier votre compte.\n"
        f"Parier Keno Haïti"
    )
    return send_sms_notification.delay(phone, message)


@celery_app.task(
    name="app.workers.notification_worker.send_self_exclusion_confirmation",
    max_retries=3
)
def send_self_exclusion_confirmation(user_id: str):
    """Envoie une confirmation d'auto-exclusion"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_send_self_exclusion_confirmation_async(user_id))
    else:
        return loop.run_until_complete(_send_self_exclusion_confirmation_async(user_id))


async def _send_self_exclusion_confirmation_async(user_id: str):
    """Logique d'envoi de confirmation d'auto-exclusion"""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.id == user_id)
            )
            user = result.scalar_one_or_none()
            
            if not user or not user.phone:
                logger.warning(f"Utilisateur {user_id} non trouvé")
                return
            
            message = (
                f"🔒 Auto-exclusion confirmée\n"
                f"Votre compte est désactivé.\n"
                f"Pour toute question, contactez le support.\n"
                f"Parier Keno Haïti - Jouez responsablement"
            )
            
            send_sms_notification.delay(user.phone, message)
            
    except Exception as e:
        logger.error(f"Erreur envoi confirmation exclusion: {e}")


@celery_app.task(
    name="app.workers.notification_worker.send_promotion_notification",
    max_retries=3
)
def send_promotion_notification(user_id: str, promotion_id: str):
    """Envoie une notification pour une promotion"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_send_promotion_notification_async(user_id, promotion_id))
    else:
        return loop.run_until_complete(_send_promotion_notification_async(user_id, promotion_id))


async def _send_promotion_notification_async(user_id: str, promotion_id: str):
    """Logique d'envoi de notification promotion"""
    try:
        async with AsyncSessionLocal() as db:
            user_result = await db.execute(
                select(User).where(User.id == user_id)
            )
            user = user_result.scalar_one_or_none()
            
            if not user or not user.phone:
                return
            
            promo_result = await db.execute(
                select(Promotion).where(Promotion.id == promotion_id)
            )
            promotion = promo_result.scalar_one_or_none()
            
            if not promotion or promotion.status != PromotionStatus.ACTIVE:
                return
            
            message = (
                f"🎁 Nouvelle promotion!\n"
                f"{promotion.name}\n"
                f"{promotion.description}\n"
                f"Utilisez le code: {promotion.code}\n"
                f"Parier Keno Haïti"
            )
            
            send_sms_notification.delay(user.phone, message)
            
    except Exception as e:
        logger.error(f"Erreur envoi notification promotion: {e}")


@celery_app.task(
    name="app.workers.notification_worker.send_kyc_reminder",
    max_retries=2
)
def send_kyc_reminder():
    """Rappelle aux joueurs de compléter leur KYC"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_send_kyc_reminder_async())
    else:
        return loop.run_until_complete(_send_kyc_reminder_async())


async def _send_kyc_reminder_async():
    """Logique d'envoi de rappel KYC"""
    logger.info("🔔 Envoi des rappels KYC...")
    
    try:
        async with AsyncSessionLocal() as db:
            from app.models.enums import KYCStatus
            
            cutoff_date = datetime.utcnow() - timedelta(days=7)
            
            result = await db.execute(
                select(User)
                .where(
                    and_(
                        User.kyc_status == KYCStatus.PENDING,
                        User.created_at < cutoff_date,
                        User.phone.isnot(None)
                    )
                )
                .limit(100)
            )
            users = result.scalars().all()
            
            if users:
                logger.info(f"📋 {len(users)} utilisateurs sans KYC")
                
                for user in users:
                    message = (
                        f"⚠️ Action requise: KYC\n"
                        f"Complétez votre vérification d'identité.\n"
                        f"Retraits suspendus tant que KYC non validé.\n"
                        f"Parier Keno Haïti"
                    )
                    send_sms_notification.delay(user.phone, message)
                
    except Exception as e:
        logger.error(f"Erreur envoi rappels KYC: {e}")


@celery_app.task(
    name="app.workers.notification_worker.send_daily_summary",
    max_retries=2
)
def send_daily_summary():
    """Envoie un résumé quotidien aux agents et admins"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_send_daily_summary_async())
    else:
        return loop.run_until_complete(_send_daily_summary_async())


async def _send_daily_summary_async():
    """Logique d'envoi du résumé quotidien"""
    logger.info("📊 Envoi du résumé quotidien...")
    
    try:
        async with AsyncSessionLocal() as db:
            today = datetime.utcnow().date()
            today_start = datetime.combine(today, datetime.min.time())
            today_end = datetime.combine(today, datetime.max.time())
            
            from app.models.keno import KenoDraw, KenoBet
            from app.models.lucky import LuckyPlay
            from app.models.transaction import Transaction
            from app.models.enums import UserRole
            
            # Keno
            keno_result = await db.execute(
                select(
                    func.count(KenoDraw.id).filter(KenoDraw.status == "completed").label("draws"),
                    func.count(KenoBet.id).label("bets"),
                    func.coalesce(func.sum(KenoBet.stake), 0).label("stake")
                )
                .where(KenoBet.placed_at >= today_start)
            )
            keno_stats = keno_result.one()
            
            # Lucky
            lucky_result = await db.execute(
                select(
                    func.count(LuckyPlay.id).label("plays"),
                    func.coalesce(func.sum(LuckyPlay.stake), 0).label("stake")
                )
                .where(LuckyPlay.played_at >= today_start)
            )
            lucky_stats = lucky_result.one()
            
            # Transactions
            tx_result = await db.execute(
                select(
                    func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == "deposit"), 0).label("deposits"),
                    func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == "withdrawal"), 0).label("withdrawals")
                )
                .where(Transaction.created_at >= today_start)
            )
            tx_stats = tx_result.one()
            
            # Récupérer les staff
            staff_result = await db.execute(
                select(User)
                .where(User.role.in_([UserRole.AGENT, UserRole.MANAGER, UserRole.ADMIN, UserRole.SUPER_ADMIN]))
            )
            staff = staff_result.scalars().all()
            
            for agent in staff:
                if agent.phone:
                    message = (
                        f"📊 Résumé quotidien - {today.isoformat()}\n"
                        f"Keno: {keno_stats.draws or 0} tirages, {keno_stats.bets or 0} paris, {float(keno_stats.stake):.0f} HTG\n"
                        f"Lucky: {lucky_stats.plays or 0} parties, {float(lucky_stats.stake):.0f} HTG\n"
                        f"Dépôts: {float(tx_stats.deposits):.0f} HTG\n"
                        f"Retraits: {float(tx_stats.withdrawals):.0f} HTG\n"
                        f"Parier Keno Haïti"
                    )
                    send_sms_notification.delay(agent.phone, message)
            
            logger.info(f"✅ Résumé quotidien envoyé à {len(staff)} personnes")
            
    except Exception as e:
        logger.error(f"Erreur envoi résumé quotidien: {e}")


@celery_app.task(
    name="app.workers.notification_worker.send_push_notification",
    max_retries=3
)
def send_push_notification(user_id: str, title: str, body: str, data: dict = None):
    """Envoie une notification push (pour future app mobile)"""
    logger.info(f"📲 Push notification à {user_id}: {title}")
    return {"success": True, "simulated": True}


@celery_app.task(
    name="app.workers.notification_worker.send_agent_alert",
    max_retries=3
)
def send_agent_alert(agent_id: str, message: str, alert_type: str = "info"):
    """Envoie une alerte à un agent"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_send_agent_alert_async(agent_id, message, alert_type))
    else:
        return loop.run_until_complete(_send_agent_alert_async(agent_id, message, alert_type))


async def _send_agent_alert_async(agent_id: str, message: str, alert_type: str):
    """Logique d'envoi d'alerte agent"""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(User).where(User.id == agent_id)
            )
            agent = result.scalar_one_or_none()
            
            if not agent or not agent.phone:
                logger.warning(f"Agent {agent_id} non trouvé ou sans téléphone")
                return
            
            if alert_type == "emergency":
                prefix = "🚨 URGENT - "
            elif alert_type == "warning":
                prefix = "⚠️ ALERTE - "
            else:
                prefix = "ℹ️ INFO - "
            
            full_message = f"{prefix}{message}"
            send_sms_notification.delay(agent.phone, full_message)
            
    except Exception as e:
        logger.error(f"Erreur envoi alerte agent: {e}")