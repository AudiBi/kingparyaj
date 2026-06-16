# app/api/v1/reports.py
"""API de rapports et statistiques"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import Optional
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.database import get_db
from app.core.security import get_current_admin, get_current_agent
from app.models.user import User
from app.models.transaction import Transaction, TransactionType
from app.models.keno import KenoDraw, KenoBet
from app.models.ticket import Ticket
from app.models.bureau import Bureau, CashierSession

router = APIRouter(prefix="/reports", tags=["Reports"])


@router.get("/daily")
async def daily_report(
    date: Optional[str] = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Rapport journalier."""
    
    if date:
        report_date = datetime.strptime(date, "%Y-%m-%d").date()
    else:
        report_date = datetime.utcnow().date()
    
    start_date = datetime.combine(report_date, datetime.min.time())
    end_date = datetime.combine(report_date, datetime.max.time())
    
    # Transactions
    transactions_result = await db.execute(
        select(
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.DEPOSIT), 0).label("deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WITHDRAWAL), 0).label("withdrawals"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.BET), 0).label("bets"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WIN), 0).label("wins")
        ).where(
            and_(
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
                Transaction.status == "completed"
            )
        )
    )
    transactions = transactions_result.one()
    
    # Nouveaux utilisateurs
    users_result = await db.execute(
        select(func.count(User.id)).where(
            and_(
                User.created_at >= start_date,
                User.created_at <= end_date
            )
        )
    )
    new_users = users_result.scalar()
    
    # Tirages Keno
    draws_result = await db.execute(
        select(
            func.count(KenoDraw.id).label("total"),
            func.coalesce(func.sum(KenoDraw.total_amount), 0).label("total_stake"),
            func.coalesce(func.sum(KenoDraw.total_payout), 0).label("total_payout")
        ).where(
            and_(
                KenoDraw.draw_time >= start_date,
                KenoDraw.draw_time <= end_date,
                KenoDraw.status == "completed"
            )
        )
    )
    draws = draws_result.one()
    
    # Tickets
    tickets_result = await db.execute(
        select(
            func.count(Ticket.id).label("created"),
            func.coalesce(func.sum(Ticket.initial_amount), 0).label("total_amount")
        ).where(
            and_(
                Ticket.created_at >= start_date,
                Ticket.created_at <= end_date
            )
        )
    )
    tickets = tickets_result.one()
    
    return {
        "date": report_date.isoformat(),
        "transactions": {
            "deposits": float(transactions.deposits),
            "withdrawals": float(transactions.withdrawals),
            "bets": float(transactions.bets),
            "wins": float(transactions.wins),
            "net": float(transactions.deposits - transactions.withdrawals + transactions.wins - transactions.bets)
        },
        "users": {
            "new": new_users or 0
        },
        "keno": {
            "draws": draws.total or 0,
            "total_stake": float(draws.total_stake),
            "total_payout": float(draws.total_payout),
            "house_edge": round((float(draws.total_stake) - float(draws.total_payout)) / float(draws.total_stake) * 100, 2) if draws.total_stake > 0 else 0
        },
        "tickets": {
            "created": tickets.created or 0,
            "total_amount": float(tickets.total_amount)
        }
    }


@router.get("/weekly")
async def weekly_report(
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Rapport hebdomadaire."""
    
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=7)
    
    # Transactions par jour
    transactions_result = await db.execute(
        select(
            func.date_trunc('day', Transaction.created_at).label("day"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.DEPOSIT), 0).label("deposits"),
            func.coalesce(func.sum(Transaction.amount).filter(Transaction.transaction_type == TransactionType.WITHDRAWAL), 0).label("withdrawals")
        ).where(
            and_(
                Transaction.created_at >= start_date,
                Transaction.created_at <= end_date,
                Transaction.status == "completed"
            )
        ).group_by(func.date_trunc('day', Transaction.created_at))
        .order_by(func.date_trunc('day', Transaction.created_at))
    )
    daily = transactions_result.all()
    
    # Nouveaux utilisateurs par jour
    users_result = await db.execute(
        select(
            func.date_trunc('day', User.created_at).label("day"),
            func.count(User.id).label("new_users")
        ).where(
            and_(
                User.created_at >= start_date,
                User.created_at <= end_date
            )
        ).group_by(func.date_trunc('day', User.created_at))
    )
    daily_users = {row.day.date().isoformat(): row.new_users for row in users_result}
    
    # Formater la réponse
    days = []
    for i in range(7):
        day_date = (start_date + timedelta(days=i)).date()
        day_str = day_date.isoformat()
        
        day_data = next((d for d in daily if d.day.date() == day_date), None)
        
        days.append({
            "date": day_str,
            "deposits": float(day_data.deposits) if day_data else 0,
            "withdrawals": float(day_data.withdrawals) if day_data else 0,
            "new_users": daily_users.get(day_str, 0)
        })
    
    # Totaux
    total_deposits = sum(d["deposits"] for d in days)
    total_withdrawals = sum(d["withdrawals"] for d in days)
    
    return {
        "start_date": start_date.date().isoformat(),
        "end_date": end_date.date().isoformat(),
        "daily": days,
        "totals": {
            "deposits": total_deposits,
            "withdrawals": total_withdrawals,
            "net": total_deposits - total_withdrawals
        }
    }


@router.get("/financial")
async def financial_report(
    start_date: str,
    end_date: str,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Rapport financier détaillé."""
    
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
    
    # Transactions par type
    transactions_result = await db.execute(
        select(
            Transaction.transaction_type,
            func.count(Transaction.id).label("count"),
            func.coalesce(func.sum(Transaction.amount), 0).label("total")
        ).where(
            and_(
                Transaction.created_at >= start,
                Transaction.created_at <= end,
                Transaction.status == "completed"
            )
        ).group_by(Transaction.transaction_type)
    )
    transactions = {row.transaction_type.value: {"count": row.count, "total": float(row.total)} for row in transactions_result}
    
    # Méthodes de paiement
    methods_result = await db.execute(
        select(
            Transaction.payment_method,
            func.count(Transaction.id).label("count"),
            func.coalesce(func.sum(Transaction.amount), 0).label("total")
        ).where(
            and_(
                Transaction.created_at >= start,
                Transaction.created_at <= end,
                Transaction.status == "completed",
                Transaction.payment_method.isnot(None)
            )
        ).group_by(Transaction.payment_method)
    )
    methods = {row.payment_method.value: {"count": row.count, "total": float(row.total)} for row in methods_result}
    
    return {
        "period": {
            "start_date": start_date,
            "end_date": end_date
        },
        "transactions": {
            "deposits": transactions.get("deposit", {"count": 0, "total": 0}),
            "withdrawals": transactions.get("withdrawal", {"count": 0, "total": 0}),
            "bets": transactions.get("bet", {"count": 0, "total": 0}),
            "wins": transactions.get("win", {"count": 0, "total": 0})
        },
        "payment_methods": methods,
        "summary": {
            "total_volume": sum(t["total"] for t in transactions.values()),
            "net_revenue": (transactions.get("deposit", {"total": 0})["total"] - 
                          transactions.get("withdrawal", {"total": 0})["total"] +
                          transactions.get("win", {"total": 0})["total"] - 
                          transactions.get("bet", {"total": 0})["total"])
        }
    }


@router.get("/agents")
async def agent_report(
    agent_id: Optional[str] = None,
    bureau_id: Optional[str] = None,
    date: Optional[str] = None,
    current_user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Rapport des agents."""
    
    if date:
        report_date = datetime.strptime(date, "%Y-%m-%d").date()
    else:
        report_date = datetime.utcnow().date()
    
    start_date = datetime.combine(report_date, datetime.min.time())
    end_date = datetime.combine(report_date, datetime.max.time())
    
    # Requête de base
    query = select(CashierSession)
    
    if agent_id:
        query = query.where(CashierSession.agent_id == agent_id)
    if bureau_id:
        query = query.where(CashierSession.bureau_id == bureau_id)
    
    query = query.where(
        and_(
            CashierSession.opened_at >= start_date,
            CashierSession.opened_at <= end_date
        )
    )
    
    result = await db.execute(query)
    sessions = result.scalars().all()
    
    return {
        "date": report_date.isoformat(),
        "sessions": [
            {
                "agent_id": s.agent_id,
                "bureau_id": s.bureau_id,
                "starting_balance": float(s.starting_balance),
                "cash_in": float(s.cash_in_amount),
                "cash_out": float(s.cash_out_amount),
                "expected_balance": float(s.expected_balance),
                "actual_balance": float(s.current_balance),
                "difference": float(s.difference),
                "opened_at": s.opened_at,
                "closed_at": s.closed_at
            }
            for s in sessions
        ],
        "totals": {
            "total_cash_in": sum(float(s.cash_in_amount) for s in sessions),
            "total_cash_out": sum(float(s.cash_out_amount) for s in sessions),
            "total_difference": sum(float(s.difference) for s in sessions)
        }
    }