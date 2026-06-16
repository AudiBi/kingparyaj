# app/workers/celery.py
"""Configuration Celery pour les workers - VERSION COMPLÈTE (Keno + Lucky)"""

import datetime

from celery import Celery
from celery.schedules import crontab
from app.config import settings
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

celery_app = Celery(
    "parier_keno",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.workers.draw_worker",
        "app.workers.notification_worker",
        "app.workers.cleanup_worker",
        "app.workers.monitoring_worker"
    ]
)

celery_app.conf.update(
    timezone="America/Port-au-Prince",
    enable_utc=True,
    
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    
    task_track_started=True,
    task_time_limit=30 * 60,
    task_soft_time_limit=25 * 60,
    
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    worker_concurrency=4,
    
    result_expires=3600,
    result_backend_transport_options={
        "visibility_timeout": 3600,
    },
    
    task_default_rate_limit="100/s",
    
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    
    worker_redirect_stdouts=False,
    worker_hijack_root_logger=False,
    
    beat_schedule={
        # ==================== KENO ====================
        'process-keno-draws': {
            'task': 'app.workers.draw_worker.process_draw',
            'schedule': crontab(minute='*/5'),
            'args': (),
        },
        'schedule-keno-draws': {
            'task': 'app.workers.draw_worker.schedule_draws',
            'schedule': crontab(hour=0, minute=5),
            'args': (),
        },
        'cancel-stale-keno-draws': {
            'task': 'app.workers.draw_worker.cancel_stale_draws',
            'schedule': crontab(minute='*/15'),
            'args': (),
        },
        'export-keno-to-leh': {
            'task': 'app.workers.draw_worker.export_draw_results_to_leh',
            'schedule': crontab(hour=1, minute=0),
            'args': (datetime.utcnow().strftime("%Y-%m-%d"),),
        },
        
        # ==================== LUCKY ====================
        'export-lucky-to-leh': {
            'task': 'app.workers.draw_worker.export_lucky_daily_to_leh',
            'schedule': crontab(hour=1, minute=30),
            'args': (),
        },
        'cleanup-old-lucky-plays': {
            'task': 'app.workers.cleanup_worker.cleanup_old_lucky_plays',
            'schedule': crontab(hour=2, minute=30),
            'args': (),
        },
        'cleanup-inactive-wheel-configs': {
            'task': 'app.workers.cleanup_worker.cleanup_inactive_wheel_configs',
            'schedule': crontab(hour=3, minute=30),
            'args': (),
        },
        'send-lucky-daily-reminder': {
            'task': 'app.workers.notification_worker.send_lucky_daily_reminder',
            'schedule': crontab(hour=10, minute=0),
            'args': (),
        },
        
        # ==================== CLEANUP ====================
        'daily-cleanup': {
            'task': 'app.workers.cleanup_worker.run_daily_cleanup',
            'schedule': crontab(hour=2, minute=0),
            'args': (),
        },
        'cleanup-expired-tickets': {
            'task': 'app.workers.cleanup_worker.cleanup_expired_tickets',
            'schedule': crontab(minute=0),
            'args': (),
        },
        'cleanup-expired-sessions': {
            'task': 'app.workers.cleanup_worker.cleanup_expired_sessions',
            'schedule': crontab(minute='*/30'),
            'args': (),
        },
        'reset-daily-counts': {
            'task': 'app.workers.cleanup_worker.reset_daily_counts',
            'schedule': crontab(hour=0, minute=0),
            'args': (),
        },
        
        # ==================== NOTIFICATIONS ====================
        'notify-expiring-tickets': {
            'task': 'app.workers.notification_worker.notify_expiring_tickets',
            'schedule': crontab(hour=9, minute=0),
            'args': (),
        },
        'send-daily-summary': {
            'task': 'app.workers.notification_worker.send_daily_summary',
            'schedule': crontab(hour=8, minute=0),
            'args': (),
        },
        'send-kyc-reminder': {
            'task': 'app.workers.notification_worker.send_kyc_reminder',
            'schedule': crontab(hour=11, minute=0),
            'args': (),
        },
        
        # ==================== MONITORING ====================
        'monitoring-report': {
            'task': 'app.workers.monitoring_worker.generate_performance_report',
            'schedule': crontab(hour=23, minute=59),
            'args': (),
        },
        'check-system-health': {
            'task': 'app.workers.monitoring_worker.check_system_health',
            'schedule': crontab(minute='*/15'),
            'args': (),
        },
        'check-performance-metrics': {
            'task': 'app.workers.monitoring_worker.check_performance_metrics',
            'schedule': crontab(minute='*/5'),
            'args': (),
        },
        'generate-weekly-report': {
            'task': 'app.workers.monitoring_worker.generate_weekly_report',
            'schedule': crontab(day_of_week='sunday', hour=23, minute=59),
            'args': (),
        },
    }
)

celery_app.conf.update(
    worker_redirect_stdouts_level="INFO",
    worker_redirect_stdouts=True,
)

if __name__ == "__main__":
    celery_app.start()