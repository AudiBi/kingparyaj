# app/workers/cleanup_worker.py
"""Worker pour le nettoyage des données expirées - VERSION COMPLÈTE (Keno + Lucky)"""

from celery import Task
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, delete, update, and_, func, or_
from datetime import datetime, timedelta
import asyncio
import logging

from app.config import settings
from app.core.redis_client import redis_client
from app.core.logger import get_logger
from app.workers.celery import celery_app
from app.models.ticket import Ticket, TicketStatus
from app.models.bureau import CashierSession
from app.models.audit import AuditLog
from app.models.keno import KenoDraw, KenoDrawStatus
from app.models.lucky import LuckyPlay, LuckyWheelConfig
from app.models.notification import Notification, NotificationStatus
from app.models.session import UserSession
from app.models.transaction import Transaction

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


class CleanupTask(Task):
    """Tâche de nettoyage avec gestion d'erreurs"""
    
    async def get_db(self):
        return AsyncSessionLocal()
    
    async def _run(self, *args, **kwargs):
        try:
            return await super()._run(*args, **kwargs)
        except Exception as e:
            logger.error(f"Cleanup task failed: {e}", exc_info=True)
            raise


# ==================== KENO - NETTOYAGE ====================

@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_old_keno_draws",
    max_retries=2
)
def cleanup_old_keno_draws(self):
    """Archive les anciens tirages Keno (> 90 jours)"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_old_keno_draws_async())
    else:
        return loop.run_until_complete(_cleanup_old_keno_draws_async())


async def _cleanup_old_keno_draws_async():
    """Logique d'archivage des tirages Keno"""
    logger.info("🧹 Archivage des anciens tirages Keno...")
    
    try:
        async with AsyncSessionLocal() as db:
            archive_date = datetime.utcnow() - timedelta(days=90)
            
            result = await db.execute(
                select(KenoDraw)
                .where(
                    and_(
                        KenoDraw.status == KenoDrawStatus.COMPLETED,
                        KenoDraw.draw_time < archive_date
                    )
                )
                .limit(1000)
            )
            old_draws = result.scalars().all()
            
            if old_draws:
                for draw in old_draws:
                    draw.is_deleted = True
                    draw.metadata = draw.metadata or {}
                    draw.metadata["archived_at"] = datetime.utcnow().isoformat()
                
                await db.commit()
                logger.info(f"📦 {len(old_draws)} tirages Keno archivés")
                
    except Exception as e:
        logger.error(f"❌ Erreur archivage tirages Keno: {e}")
        raise


# ==================== LUCKY - NETTOYAGE ====================

@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_old_lucky_plays",
    max_retries=2
)
def cleanup_old_lucky_plays(self):
    """Nettoie les anciennes parties Lucky (> 90 jours)"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_old_lucky_plays_async())
    else:
        return loop.run_until_complete(_cleanup_old_lucky_plays_async())


async def _cleanup_old_lucky_plays_async():
    """Logique de nettoyage des parties Lucky"""
    logger.info("🧹 Nettoyage des anciennes parties Lucky...")
    
    try:
        async with AsyncSessionLocal() as db:
            archive_date = datetime.utcnow() - timedelta(days=90)
            
            result = await db.execute(
                select(LuckyPlay)
                .where(LuckyPlay.played_at < archive_date)
                .limit(1000)
            )
            old_plays = result.scalars().all()
            
            if old_plays:
                for play in old_plays:
                    play.is_deleted = True
                    play.metadata = play.metadata or {}
                    play.metadata["archived_at"] = datetime.utcnow().isoformat()
                
                await db.commit()
                logger.info(f"📦 {len(old_plays)} parties Lucky archivées")
                
    except Exception as e:
        logger.error(f"❌ Erreur nettoyage Lucky: {e}")
        raise


@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_inactive_wheel_configs",
    max_retries=2
)
def cleanup_inactive_wheel_configs(self):
    """Nettoie les configurations de roue inactives"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_inactive_wheel_configs_async())
    else:
        return loop.run_until_complete(_cleanup_inactive_wheel_configs_async())


async def _cleanup_inactive_wheel_configs_async():
    """Logique de nettoyage des configs inactives"""
    logger.info("🧹 Nettoyage des configs roue inactives...")
    
    try:
        async with AsyncSessionLocal() as db:
            cutoff_date = datetime.utcnow() - timedelta(days=30)
            
            result = await db.execute(
                select(LuckyWheelConfig)
                .where(
                    and_(
                        LuckyWheelConfig.is_active == False,
                        LuckyWheelConfig.updated_at < cutoff_date
                    )
                )
                .limit(100)
            )
            old_configs = result.scalars().all()
            
            if old_configs:
                for config in old_configs:
                    config.is_deleted = True
                
                await db.commit()
                logger.info(f"🗑️ {len(old_configs)} configs roue supprimées")
                
    except Exception as e:
        logger.error(f"❌ Erreur nettoyage configs: {e}")
        raise


# ==================== NETTOYAGE COMMUN ====================

@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_expired_tickets",
    max_retries=2
)
def cleanup_expired_tickets(self):
    """Nettoie les tickets expirés"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_expired_tickets_async())
    else:
        return loop.run_until_complete(_cleanup_expired_tickets_async())


async def _cleanup_expired_tickets_async():
    """Logique de nettoyage des tickets expirés"""
    logger.info("🧹 Nettoyage des tickets expirés...")
    
    try:
        async with AsyncSessionLocal() as db:
            now = datetime.utcnow()
            
            result = await db.execute(
                select(Ticket)
                .where(
                    and_(
                        Ticket.status == TicketStatus.ACTIVE,
                        Ticket.expires_at < now
                    )
                )
                .limit(1000)
            )
            expired_tickets = result.scalars().all()
            
            if expired_tickets:
                for ticket in expired_tickets:
                    ticket.status = TicketStatus.EXPIRED
                    if ticket.balance > 0:
                        logger.info(f"📌 Ticket {ticket.ticket_number} expiré, {ticket.balance:.0f} HTG non réclamés")
                
                await db.commit()
                logger.info(f"✅ {len(expired_tickets)} tickets marqués comme expirés")
                
                for ticket in expired_tickets:
                    if ticket.balance > 1000:
                        from app.workers.notification_worker import send_agent_alert
                        send_agent_alert.delay(
                            ticket.agent_id,
                            f"Ticket {ticket.ticket_number} expiré avec {ticket.balance:.0f} HTG"
                        )
            else:
                logger.info("Aucun ticket expiré")
                
    except Exception as e:
        logger.error(f"❌ Erreur nettoyage tickets: {e}")
        raise


@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_expired_sessions"
)
def cleanup_expired_sessions(self):
    """Nettoie les sessions de caisse ouvertes trop longtemps"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_expired_sessions_async())
    else:
        return loop.run_until_complete(_cleanup_expired_sessions_async())


async def _cleanup_expired_sessions_async():
    """Logique de nettoyage des sessions de caisse"""
    logger.info("🧹 Nettoyage des sessions de caisse...")
    
    try:
        async with AsyncSessionLocal() as db:
            expiry_time = datetime.utcnow() - timedelta(hours=24)
            
            result = await db.execute(
                select(CashierSession)
                .where(
                    and_(
                        CashierSession.status == "OPEN",
                        CashierSession.opened_at < expiry_time
                    )
                )
            )
            old_sessions = result.scalars().all()
            
            if old_sessions:
                for session in old_sessions:
                    session.status = "SUSPENDED"
                    session.closed_at = datetime.utcnow()
                    session.difference = session.current_balance - session.expected_balance
                    session.difference_reason = "Session automatiquement fermée (24h)"
                
                await db.commit()
                logger.info(f"✅ {len(old_sessions)} sessions de caisse fermées automatiquement")
            
            archive_date = datetime.utcnow() - timedelta(days=90)
            
            result = await db.execute(
                select(CashierSession)
                .where(
                    and_(
                        CashierSession.status.in_(["CLOSED", "SUSPENDED"]),
                        CashierSession.closed_at < archive_date
                    )
                )
                .limit(1000)
            )
            archive_sessions = result.scalars().all()
            
            for session in archive_sessions:
                session.is_deleted = True
            
            await db.commit()
            if archive_sessions:
                logger.info(f"📦 {len(archive_sessions)} sessions archivées")
                
    except Exception as e:
        logger.error(f"❌ Erreur nettoyage sessions: {e}")
        raise


@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_old_audit_logs"
)
def cleanup_old_audit_logs(self):
    """Archive les vieux logs d'audit (7 ans - conformité LEH)"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_old_audit_logs_async())
    else:
        return loop.run_until_complete(_cleanup_old_audit_logs_async())


async def _cleanup_old_audit_logs_async():
    """Logique d'archivage des logs d'audit"""
    logger.info("🧹 Archivage des logs d'audit...")
    
    try:
        async with AsyncSessionLocal() as db:
            archive_date = datetime.utcnow() - timedelta(days=365 * 7)
            
            result = await db.execute(
                select(AuditLog)
                .where(
                    and_(
                        AuditLog.created_at < archive_date,
                        AuditLog.leh_exported == True
                    )
                )
                .limit(1000)
            )
            old_logs = result.scalars().all()
            
            if old_logs:
                for log in old_logs:
                    log.metadata = log.metadata or {}
                    log.metadata["archived_at"] = datetime.utcnow().isoformat()
                
                await db.commit()
                logger.info(f"📦 {len(old_logs)} logs d'audit archivés")
                
    except Exception as e:
        logger.error(f"❌ Erreur archivage logs: {e}")
        raise


@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_orphaned_data"
)
def cleanup_orphaned_data(self):
    """Nettoie les données orphelines"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_orphaned_data_async())
    else:
        return loop.run_until_complete(_cleanup_orphaned_data_async())


async def _cleanup_orphaned_data_async():
    """Logique de nettoyage des données orphelines"""
    logger.info("🧹 Nettoyage des données orphelines...")
    
    try:
        async with AsyncSessionLocal() as db:
            # Notifications orphelines
            result = await db.execute(
                select(Notification)
                .where(
                    and_(
                        Notification.status == NotificationStatus.PENDING,
                        Notification.created_at < datetime.utcnow() - timedelta(hours=24)
                    )
                )
                .limit(500)
            )
            old_notifications = result.scalars().all()
            
            if old_notifications:
                for notif in old_notifications:
                    notif.status = NotificationStatus.FAILED
                    notif.error = "Notification expirée"
                
                await db.commit()
                logger.info(f"📬 {len(old_notifications)} notifications orphelines nettoyées")
            
            # Tirages en attente trop vieux
            stale_date = datetime.utcnow() - timedelta(hours=1)
            result = await db.execute(
                select(KenoDraw)
                .where(
                    and_(
                        KenoDraw.status == KenoDrawStatus.PENDING,
                        KenoDraw.draw_time < stale_date
                    )
                )
            )
            stale_draws = result.scalars().all()
            
            if stale_draws:
                for draw in stale_draws:
                    draw.status = KenoDrawStatus.CANCELLED
                    draw.closed_at = datetime.utcnow()
                    draw.closed_by = "system"
                
                await db.commit()
                logger.info(f"🎯 {len(stale_draws)} tirages en attente annulés")
                
    except Exception as e:
        logger.error(f"❌ Erreur nettoyage données orphelines: {e}")
        raise


@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_old_sessions"
)
def cleanup_old_sessions(self):
    """Nettoie les sessions utilisateur expirées"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_old_sessions_async())
    else:
        return loop.run_until_complete(_cleanup_old_sessions_async())


async def _cleanup_old_sessions_async():
    """Logique de nettoyage des sessions utilisateur"""
    logger.info("🧹 Nettoyage des sessions utilisateur...")
    
    try:
        async with AsyncSessionLocal() as db:
            expiry_date = datetime.utcnow() - timedelta(days=30)
            
            result = await db.execute(
                delete(UserSession)
                .where(UserSession.created_at < expiry_date)
            )
            
            await db.commit()
            if result.rowcount > 0:
                logger.info(f"✅ {result.rowcount} sessions utilisateur supprimées")
                
    except Exception as e:
        logger.error(f"❌ Erreur nettoyage sessions utilisateur: {e}")
        raise


@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.archive_old_transactions"
)
def archive_old_transactions(self):
    """Archive les transactions de plus de 10 ans"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_archive_old_transactions_async())
    else:
        return loop.run_until_complete(_archive_old_transactions_async())


async def _archive_old_transactions_async():
    """Logique d'archivage des transactions"""
    logger.info("📦 Archivage des transactions...")
    
    try:
        async with AsyncSessionLocal() as db:
            archive_date = datetime.utcnow() - timedelta(days=365 * 10)
            
            result = await db.execute(
                select(Transaction)
                .where(Transaction.created_at < archive_date)
                .limit(1000)
            )
            old_transactions = result.scalars().all()
            
            if old_transactions:
                for tx in old_transactions:
                    tx.metadata = tx.metadata or {}
                    tx.metadata["archived_at"] = datetime.utcnow().isoformat()
                    tx.metadata["archived"] = True
                
                await db.commit()
                logger.info(f"📦 {len(old_transactions)} transactions archivées")
                
    except Exception as e:
        logger.error(f"❌ Erreur archivage transactions: {e}")
        raise


@celery_app.task(
    bind=True,
    base=CleanupTask,
    name="app.workers.cleanup_worker.cleanup_duplicate_notifications"
)
def cleanup_duplicate_notifications(self):
    """Nettoie les notifications en double"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_cleanup_duplicate_notifications_async())
    else:
        return loop.run_until_complete(_cleanup_duplicate_notifications_async())


async def _cleanup_duplicate_notifications_async():
    """Logique de nettoyage des notifications en double"""
    logger.info("🧹 Nettoyage des notifications en double...")
    
    try:
        async with AsyncSessionLocal() as db:
            duplicates = await db.execute(
                select(
                    Notification.user_id,
                    Notification.type,
                    func.count(Notification.id).label("count")
                )
                .where(Notification.created_at > datetime.utcnow() - timedelta(days=1))
                .group_by(Notification.user_id, Notification.type)
                .having(func.count(Notification.id) > 1)
                .limit(100)
            )
            
            duplicate_groups = duplicates.all()
            
            if duplicate_groups:
                for group in duplicate_groups:
                    result = await db.execute(
                        select(Notification)
                        .where(
                            and_(
                                Notification.user_id == group.user_id,
                                Notification.type == group.type
                            )
                        )
                        .order_by(Notification.created_at.desc())
                    )
                    notifications = result.scalars().all()
                    
                    if len(notifications) > 1:
                        to_keep = notifications[0]
                        to_delete = notifications[1:]
                        
                        for notif in to_delete:
                            notif.status = NotificationStatus.FAILED
                            notif.error = "Notification en double supprimée"
                        
                        await db.commit()
                        logger.info(f"🗑️ {len(to_delete)} notifications en double supprimées")
                
    except Exception as e:
        logger.error(f"❌ Erreur nettoyage doublons: {e}")
        raise


@celery_app.task(
    name="app.workers.cleanup_worker.run_daily_cleanup"
)
def run_daily_cleanup():
    """Exécute tous les nettoyages quotidiens"""
    logger.info("🗑️ Début du nettoyage quotidien...")
    
    cleanup_expired_tickets.delay()
    cleanup_expired_sessions.delay()
    cleanup_old_audit_logs.delay()
    cleanup_orphaned_data.delay()
    cleanup_old_sessions.delay()
    archive_old_transactions.delay()
    cleanup_duplicate_notifications.delay()
    cleanup_old_keno_draws.delay()
    cleanup_old_lucky_plays.delay()
    cleanup_inactive_wheel_configs.delay()
    
    logger.info("✅ Nettoyage quotidien terminé")
    return {"success": True, "tasks_scheduled": 10}


@celery_app.task(
    name="app.workers.cleanup_worker.reset_daily_counts"
)
def reset_daily_counts():
    """Réinitialise les compteurs journaliers des wallets"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_reset_daily_counts_async())
    else:
        return loop.run_until_complete(_reset_daily_counts_async())


async def _reset_daily_counts_async():
    """Réinitialise les compteurs journaliers"""
    logger.info("🔄 Réinitialisation des compteurs journaliers...")
    
    try:
        async with AsyncSessionLocal() as db:
            from app.models.wallet import Wallet
            
            result = await db.execute(
                update(Wallet)
                .values(
                    today_deposits=0,
                    today_losses=0,
                    today_bets=0,
                    last_reset_date=datetime.utcnow()
                )
                .where(Wallet.last_reset_date < datetime.utcnow().date())
            )
            
            await db.commit()
            logger.info(f"✅ {result.rowcount} wallets réinitialisés")
            
    except Exception as e:
        logger.error(f"❌ Erreur réinitialisation compteurs: {e}")
        raise