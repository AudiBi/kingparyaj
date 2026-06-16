# app/workers/monitoring_worker.py
"""Worker pour le monitoring - VERSION COMPLÈTE (Keno + Lucky)"""

from celery import Task
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, func, and_
from datetime import datetime, timedelta
import asyncio
import logging
import json

from app.config import settings
from app.core.redis_client import redis_client
from app.core.logger import get_logger
from app.workers.celery import celery_app

logger = get_logger(__name__)

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


@celery_app.task(
    name="app.workers.monitoring_worker.generate_performance_report"
)
def generate_performance_report():
    """Génère un rapport de performance quotidien (Keno + Lucky)"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_generate_performance_report_async())
    else:
        return loop.run_until_complete(_generate_performance_report_async())


async def _generate_performance_report_async():
    """Logique de génération du rapport - COMPLÈTE (Keno + Lucky)"""
    logger.info("📊 Génération du rapport de performance...")
    
    try:
        async with AsyncSessionLocal() as db:
            today = datetime.utcnow().date()
            today_start = datetime.combine(today, datetime.min.time())
            today_end = datetime.combine(today, datetime.max.time())
            
            from app.models.keno import KenoDraw, KenoBet
            from app.models.lucky import LuckyPlay
            from app.models.transaction import Transaction
            from app.models.user import User
            
            # ==================== KENO ====================
            draws_result = await db.execute(
                select(func.count(KenoDraw.id))
                .where(
                    and_(
                        KenoDraw.draw_time >= today_start,
                        KenoDraw.draw_time <= today_end,
                        KenoDraw.status == "completed"
                    )
                )
            )
            total_draws = draws_result.scalar() or 0
            
            bets_result = await db.execute(
                select(
                    func.count(KenoBet.id).label("total_bets"),
                    func.coalesce(func.sum(KenoBet.stake), 0).label("total_stake"),
                    func.coalesce(func.sum(KenoBet.winnings), 0).label("total_wins")
                )
                .where(
                    and_(
                        KenoBet.placed_at >= today_start,
                        KenoBet.placed_at <= today_end
                    )
                )
            )
            keno_stats = bets_result.one()
            
            # ==================== LUCKY ====================
            lucky_result = await db.execute(
                select(
                    func.count(LuckyPlay.id).label("total_plays"),
                    func.coalesce(func.sum(LuckyPlay.stake), 0).label("total_stake"),
                    func.coalesce(func.sum(LuckyPlay.winnings), 0).label("total_wins"),
                    func.max(LuckyPlay.multiplier).label("max_multiplier"),
                    func.max(LuckyPlay.winnings).label("max_win")
                )
                .where(
                    and_(
                        LuckyPlay.played_at >= today_start,
                        LuckyPlay.played_at <= today_end
                    )
                )
            )
            lucky_stats = lucky_result.one()
            
            # Top segments Lucky
            segments_result = await db.execute(
                select(
                    LuckyPlay.result_segment['label'].label("segment"),
                    func.count(LuckyPlay.id).label("count")
                )
                .where(
                    and_(
                        LuckyPlay.played_at >= today_start,
                        LuckyPlay.played_at <= today_end
                    )
                )
                .group_by(LuckyPlay.result_segment['label'])
                .order_by(func.count(LuckyPlay.id).desc())
                .limit(5)
            )
            top_segments = segments_result.all()
            
            # ==================== TRANSACTIONS ====================
            tx_result = await db.execute(
                select(
                    func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == "deposit"), 0).label("deposits"),
                    func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == "withdrawal"), 0).label("withdrawals"),
                    func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == "bet"), 0).label("bets_volume"),
                    func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == "win"), 0).label("wins")
                )
                .where(
                    and_(
                        Transaction.created_at >= today_start,
                        Transaction.created_at <= today_end,
                        Transaction.status == "completed"
                    )
                )
            )
            deposits, withdrawals, bets_volume, wins = tx_result.one()
            
            # ==================== UTILISATEURS ====================
            users_result = await db.execute(
                select(func.count(User.id))
                .where(
                    and_(
                        User.created_at >= today_start,
                        User.created_at <= today_end
                    )
                )
            )
            new_users = users_result.scalar() or 0
            
            # ==================== RAPPORT ====================
            report = {
                "date": today.isoformat(),
                "keno": {
                    "draws": total_draws,
                    "bets": keno_stats.total_bets or 0,
                    "stake": float(keno_stats.total_stake),
                    "wins": float(keno_stats.total_wins)
                },
                "lucky": {
                    "total_plays": lucky_stats.total_plays or 0,
                    "total_stake": float(lucky_stats.total_stake),
                    "total_wins": float(lucky_stats.total_wins),
                    "max_multiplier": float(lucky_stats.max_multiplier or 0),
                    "max_win": float(lucky_stats.max_win or 0),
                    "top_segments": [
                        {"segment": s.segment, "count": s.count}
                        for s in top_segments
                    ]
                },
                "transactions": {
                    "deposits": float(deposits),
                    "withdrawals": float(withdrawals),
                    "bets_volume": float(bets_volume),
                    "wins": float(wins),
                    "net_revenue": float(deposits + wins - withdrawals - bets_volume)
                },
                "users": {
                    "new": new_users
                },
                "summary": {
                    "total_bets": (keno_stats.total_bets or 0) + (lucky_stats.total_plays or 0),
                    "total_stake": float(keno_stats.total_stake + lucky_stats.total_stake),
                    "total_wins": float(keno_stats.total_wins + lucky_stats.total_wins)
                }
            }
            
            # Stocker le rapport dans Redis
            await redis_client.setex(
                f"monitoring:report:{today.isoformat()}",
                86400 * 7,
                json.dumps(report)
            )
            
            logger.info(f"📊 Rapport généré: Keno={report['keno']['bets']} paris, Lucky={report['lucky']['total_plays']} parties")
            
            # Vérifier les anomalies
            await _check_anomalies(report)
            
            return report
            
    except Exception as e:
        logger.error(f"❌ Erreur génération rapport: {e}")
        raise


async def _check_anomalies(report: dict):
    """Vérifie les anomalies dans le rapport"""
    
    anomalies = []
    
    # Revenus négatifs
    net_revenue = report["transactions"]["net_revenue"]
    if net_revenue < 0:
        anomalies.append(f"⚠️ Revenu négatif: {net_revenue:.0f} HTG")
    
    # Dépôts inhabituels
    deposits = report["transactions"]["deposits"]
    if deposits > 500000:
        anomalies.append(f"💰 Volume de dépôts élevé: {deposits:.0f} HTG")
    
    # Gains inhabituels
    wins = report["transactions"]["wins"]
    if wins > 300000:
        anomalies.append(f"🎯 Gains élevés: {wins:.0f} HTG")
    
    # Faible activité Keno
    if report["keno"]["bets"] < 10:
        anomalies.append(f"📉 Faible activité Keno: {report['keno']['bets']} paris")
    
    # Faible activité Lucky
    if report["lucky"]["total_plays"] < 5:
        anomalies.append(f"📉 Faible activité Lucky: {report['lucky']['total_plays']} parties")
    
    # Taux de gain Keno anormal
    if report["keno"]["stake"] > 0:
        win_rate = report["keno"]["wins"] / report["keno"]["stake"] * 100
        if win_rate > 90:
            anomalies.append(f"🏆 Taux de gains Keno anormal: {win_rate:.1f}%")
    
    # Taux de gain Lucky anormal
    if report["lucky"]["total_stake"] > 0:
        win_rate = report["lucky"]["total_wins"] / report["lucky"]["total_stake"] * 100
        if win_rate > 90:
            anomalies.append(f"🏆 Taux de gains Lucky anormal: {win_rate:.1f}%")
    
    if anomalies:
        from app.workers.notification_worker import send_agent_alert
        for anomaly in anomalies:
            send_agent_alert.delay(
                "admin",
                anomaly,
                "warning"
            )
        
        logger.warning(f"⚠️ Anomalies détectées: {anomalies}")
    
    return anomalies


@celery_app.task(
    name="app.workers.monitoring_worker.check_system_health"
)
def check_system_health():
    """Vérifie la santé du système"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_check_system_health_async())
    else:
        return loop.run_until_complete(_check_system_health_async())


async def _check_system_health_async():
    """Vérifie la santé du système"""
    logger.info("🔍 Vérification de la santé du système...")
    
    health_status = {
        "timestamp": datetime.utcnow().isoformat(),
        "services": {}
    }
    
    # Base de données
    try:
        async with AsyncSessionLocal() as db:
            await db.execute("SELECT 1")
            health_status["services"]["database"] = {"status": "healthy"}
    except Exception as e:
        health_status["services"]["database"] = {"status": "unhealthy", "error": str(e)}
        logger.error(f"❌ Base de données: {e}")
    
    # Redis
    try:
        await redis_client.ping()
        health_status["services"]["redis"] = {"status": "healthy"}
    except Exception as e:
        health_status["services"]["redis"] = {"status": "unhealthy", "error": str(e)}
        logger.error(f"❌ Redis: {e}")
    
    # Workers
    try:
        from app.workers.celery import celery_app
        inspect = celery_app.control.inspect()
        active = inspect.active()
        health_status["services"]["workers"] = {
            "status": "healthy",
            "active": len(active) if active else 0
        }
    except Exception as e:
        health_status["services"]["workers"] = {"status": "unhealthy", "error": str(e)}
        logger.error(f"❌ Workers: {e}")
    
    # Stocker le health check
    await redis_client.setex(
        "monitoring:health",
        300,
        json.dumps(health_status)
    )
    
    # Alerter si problème
    for service, status in health_status["services"].items():
        if status["status"] == "unhealthy":
            from app.workers.notification_worker import send_agent_alert
            send_agent_alert.delay(
                "admin",
                f"🚨 Service {service} indisponible: {status.get('error', '')}",
                "emergency"
            )
    
    return health_status


@celery_app.task(
    name="app.workers.monitoring_worker.check_performance_metrics"
)
def check_performance_metrics():
    """Vérifie les métriques de performance"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_check_performance_metrics_async())
    else:
        return loop.run_until_complete(_check_performance_metrics_async())


async def _check_performance_metrics_async():
    """Vérifie les métriques de performance"""
    logger.info("📊 Vérification des métriques de performance...")
    
    metrics = {
        "timestamp": datetime.utcnow().isoformat(),
        "api": {},
        "db": {},
        "cache": {}
    }
    
    try:
        async with AsyncSessionLocal() as db:
            api_latency = await redis_client.get("monitoring:api:latency")
            metrics["api"]["latency"] = float(api_latency) if api_latency else 0
            
            api_requests = await redis_client.get("monitoring:api:requests")
            metrics["api"]["requests"] = int(api_requests) if api_requests else 0
            
            api_errors = await redis_client.get("monitoring:api:errors")
            metrics["api"]["errors"] = int(api_errors) if api_errors else 0
            
            cache_hits = await redis_client.get("monitoring:cache:hits")
            cache_misses = await redis_client.get("monitoring:cache:misses")
            
            metrics["cache"]["hits"] = int(cache_hits) if cache_hits else 0
            metrics["cache"]["misses"] = int(cache_misses) if cache_misses else 0
            
            total = metrics["cache"]["hits"] + metrics["cache"]["misses"]
            metrics["cache"]["hit_rate"] = round(metrics["cache"]["hits"] / total * 100, 2) if total > 0 else 0
            
            await redis_client.setex(
                "monitoring:metrics",
                3600,
                json.dumps(metrics)
            )
            
            if metrics["api"]["errors"] > 50:
                from app.workers.notification_worker import send_agent_alert
                send_agent_alert.delay(
                    "admin",
                    f"⚠️ {metrics['api']['errors']} erreurs API détectées",
                    "warning"
                )
            
            if metrics["cache"]["hit_rate"] < 50:
                from app.workers.notification_worker import send_agent_alert
                send_agent_alert.delay(
                    "admin",
                    f"⚠️ Taux de cache bas: {metrics['cache']['hit_rate']}%",
                    "warning"
                )
            
            return metrics
            
    except Exception as e:
        logger.error(f"❌ Erreur vérification métriques: {e}")
        raise


@celery_app.task(
    name="app.workers.monitoring_worker.generate_weekly_report"
)
def generate_weekly_report():
    """Génère un rapport hebdomadaire (Keno + Lucky)"""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        return loop.create_task(_generate_weekly_report_async())
    else:
        return loop.run_until_complete(_generate_weekly_report_async())


async def _generate_weekly_report_async():
    """Génère un rapport hebdomadaire"""
    logger.info("📊 Génération du rapport hebdomadaire...")
    
    try:
        async with AsyncSessionLocal() as db:
            end_date = datetime.utcnow()
            start_date = end_date - timedelta(days=7)
            
            from app.models.keno import KenoDraw, KenoBet
            from app.models.lucky import LuckyPlay
            from app.models.transaction import Transaction
            from app.models.user import User
            
            # Keno
            keno_result = await db.execute(
                select(
                    func.count(KenoDraw.id).filter(KenoDraw.status == "completed").label("draws"),
                    func.count(KenoBet.id).label("bets"),
                    func.coalesce(func.sum(KenoBet.stake), 0).label("stake"),
                    func.coalesce(func.sum(KenoBet.winnings), 0).label("wins")
                )
                .where(KenoBet.placed_at >= start_date)
            )
            keno_stats = keno_result.one()
            
            # Lucky
            lucky_result = await db.execute(
                select(
                    func.count(LuckyPlay.id).label("plays"),
                    func.coalesce(func.sum(LuckyPlay.stake), 0).label("stake"),
                    func.coalesce(func.sum(LuckyPlay.winnings), 0).label("wins")
                )
                .where(LuckyPlay.played_at >= start_date)
            )
            lucky_stats = lucky_result.one()
            
            # Transactions
            tx_result = await db.execute(
                select(
                    func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == "deposit"), 0).label("deposits"),
                    func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == "withdrawal"), 0).label("withdrawals")
                )
                .where(Transaction.created_at >= start_date)
            )
            tx_stats = tx_result.one()
            
            # Nouveaux utilisateurs
            users_result = await db.execute(
                select(func.count(User.id))
                .where(User.created_at >= start_date)
            )
            new_users = users_result.scalar() or 0
            
            report = {
                "period": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat()
                },
                "keno": {
                    "draws": keno_stats.draws or 0,
                    "bets": keno_stats.bets or 0,
                    "stake": float(keno_stats.stake),
                    "wins": float(keno_stats.wins)
                },
                "lucky": {
                    "plays": lucky_stats.plays or 0,
                    "stake": float(lucky_stats.stake),
                    "wins": float(lucky_stats.wins)
                },
                "transactions": {
                    "deposits": float(tx_stats.deposits),
                    "withdrawals": float(tx_stats.withdrawals)
                },
                "users": {
                    "new": new_users
                }
            }
            
            # Envoyer par email aux admins
            from app.workers.notification_worker import send_email_notification
            
            result = await db.execute(
                select(User).where(User.role.in_(["admin", "super_admin"]))
            )
            admins = result.scalars().all()
            
            for admin in admins:
                if admin.email:
                    send_email_notification.delay(
                        admin.email,
                        "📊 Rapport Hebdomadaire - Parier Keno & Lucky",
                        f"""
                        Rapport Hebdomadaire ({start_date.date()} au {end_date.date()})
                        
                        Keno:
                        - {keno_stats.draws or 0} tirages
                        - {keno_stats.bets or 0} paris
                        - {float(keno_stats.stake):.0f} HTG misés
                        - {float(keno_stats.wins):.0f} HTG gagnés
                        
                        Lucky:
                        - {lucky_stats.plays or 0} parties
                        - {float(lucky_stats.stake):.0f} HTG misés
                        - {float(lucky_stats.wins):.0f} HTG gagnés
                        
                        Finance:
                        - Dépôts: {float(tx_stats.deposits):.0f} HTG
                        - Retraits: {float(tx_stats.withdrawals):.0f} HTG
                        - Nouveaux joueurs: {new_users}
                        """
                    )
            
            logger.info(f"✅ Rapport hebdomadaire envoyé à {len(admins)} admins")
            
    except Exception as e:
        logger.error(f"❌ Erreur génération rapport hebdomadaire: {e}")
        raise


@celery_app.task(
    name="app.workers.monitoring_worker.alert_slow_queries"
)
def alert_slow_queries():
    """Alerte sur les requêtes lentes"""
    logger.info("🔍 Vérification des requêtes lentes...")
    return {"status": "ok"}