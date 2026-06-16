# app/workers/__init__.py
"""Workers Celery pour tâches asynchrones - Keno + Lucky"""

from app.workers.celery import celery_app

# ==================== DRAW WORKER (Keno + Lucky) ====================
from app.workers.draw_worker import (
    # Keno
    process_draw,
    schedule_draws,
    cancel_stale_draws,
    export_draw_results_to_leh,
    settle_bets_for_draw,
    generate_draw_results,
    # Lucky
    export_lucky_results_to_leh,
    export_lucky_daily_to_leh,
)

# ==================== NOTIFICATION WORKER (Keno + Lucky) ====================
from app.workers.notification_worker import (
    # Base
    send_sms_notification,
    send_email_notification,
    send_push_notification,
    send_agent_alert,
    # Keno
    send_bet_confirmation,
    send_win_notification,
    # Lucky
    send_lucky_win_notification,
    send_lucky_daily_reminder,
    # Commun
    send_deposit_confirmation,
    send_withdrawal_confirmation,
    send_self_exclusion_confirmation,
    send_promotion_notification,
    send_kyc_reminder,
    send_daily_summary,
    notify_expiring_tickets,
)

# ==================== CLEANUP WORKER (Keno + Lucky) ====================
from app.workers.cleanup_worker import (
    # Keno
    cleanup_old_keno_draws,
    # Lucky
    cleanup_old_lucky_plays,
    cleanup_inactive_wheel_configs,
    # Commun
    cleanup_expired_tickets,
    cleanup_expired_sessions,
    cleanup_old_audit_logs,
    cleanup_orphaned_data,
    cleanup_old_sessions,
    archive_old_transactions,
    cleanup_duplicate_notifications,
    run_daily_cleanup,
    reset_daily_counts,
)

# ==================== MONITORING WORKER (Keno + Lucky) ====================
from app.workers.monitoring_worker import (
    generate_performance_report,
    generate_weekly_report,
    check_system_health,
    check_performance_metrics,
    alert_slow_queries,
)

# ==================== TÂCHES PARTAGÉES ====================
from app.workers.tasks import (
    health_check,
    test_task,
)

__all__ = [
    # Celery
    "celery_app",
    
    # Draw Worker - Keno
    "process_draw",
    "schedule_draws",
    "cancel_stale_draws",
    "export_draw_results_to_leh",
    "settle_bets_for_draw",
    "generate_draw_results",
    
    # Draw Worker - Lucky
    "export_lucky_results_to_leh",
    "export_lucky_daily_to_leh",
    
    # Notification Worker - Base
    "send_sms_notification",
    "send_email_notification",
    "send_push_notification",
    "send_agent_alert",
    
    # Notification Worker - Keno
    "send_bet_confirmation",
    "send_win_notification",
    
    # Notification Worker - Lucky
    "send_lucky_win_notification",
    "send_lucky_daily_reminder",
    
    # Notification Worker - Commun
    "send_deposit_confirmation",
    "send_withdrawal_confirmation",
    "send_self_exclusion_confirmation",
    "send_promotion_notification",
    "send_kyc_reminder",
    "send_daily_summary",
    "notify_expiring_tickets",
    
    # Cleanup Worker - Keno
    "cleanup_old_keno_draws",
    
    # Cleanup Worker - Lucky
    "cleanup_old_lucky_plays",
    "cleanup_inactive_wheel_configs",
    
    # Cleanup Worker - Commun
    "cleanup_expired_tickets",
    "cleanup_expired_sessions",
    "cleanup_old_audit_logs",
    "cleanup_orphaned_data",
    "cleanup_old_sessions",
    "archive_old_transactions",
    "cleanup_duplicate_notifications",
    "run_daily_cleanup",
    "reset_daily_counts",
    
    # Monitoring Worker
    "generate_performance_report",
    "generate_weekly_report",
    "check_system_health",
    "check_performance_metrics",
    "alert_slow_queries",
    
    # Tâches partagées
    "health_check",
    "test_task",
]