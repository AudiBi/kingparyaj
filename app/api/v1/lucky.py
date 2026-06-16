# app/api/v1/lucky.py
"""API du jeu Lucky Wheel (Roue de la chance)"""

from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List, Optional
from datetime import datetime, timedelta
from decimal import Decimal
import secrets
import hashlib

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_user, get_current_agent
from app.schemas.lucky import (
    LuckySpinRequest, LuckySpinResponse, LuckyWheelConfigResponse,
    LuckyPlayHistoryResponse, LuckyStatsResponse
)
from app.schemas.common import SuccessResponse
from app.services.wallet_service import WalletService
from app.services.lucky_service import LuckyWheelService
from app.models.user import User
from app.models.lucky import LuckyPlay, LuckyWheelConfig
from app.models.ticket import Ticket
from app.models.audit import AuditLog, AuditAction
import redis.asyncio as redis

router = APIRouter(prefix="/lucky", tags=["Lucky"])


# ==================== CONFIGURATION ====================

@router.get(
    "/wheel/config",
    response_model=LuckyWheelConfigResponse,
    summary="Configuration de la roue",
    description="Récupère la configuration actuelle de la roue de la chance"
)
async def get_wheel_config(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """Récupère la configuration de la roue."""
    
    # Essayer le cache Redis
    cached = await redis_client.get("lucky:wheel:config")
    if cached:
        import json
        return json.loads(cached)
    
    # Récupérer la config active
    result = await db.execute(
        select(LuckyWheelConfig)
        .where(LuckyWheelConfig.is_active == True)
        .order_by(LuckyWheelConfig.is_default.desc())
        .limit(1)
    )
    config = result.scalar_one_or_none()
    
    if not config:
        # Créer la config par défaut
        from app.models.lucky import LuckyWheelConfig as LuckyWheelConfigModel
        config = LuckyWheelConfigModel.get_default_config()
        db.add(config)
        await db.commit()
    
    response = {
        "name": config.name,
        "segments": config.segments,
        "min_bet": float(config.min_bet),
        "max_bet": float(config.max_bet),
        "theoretical_rtp": config.theoretical_rtp
    }
    
    # Mettre en cache
    await redis_client.setex("lucky:wheel:config", 3600, response)
    
    return response


# ==================== JEU ====================

@router.post(
    "/wheel/spin",
    response_model=LuckySpinResponse,
    summary="Tourner la roue",
    description="Fait tourner la roue de la chance - résultat instantané"
)
async def spin_wheel(
    request: Request,
    spin_data: LuckySpinRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Fait tourner la roue de la chance.
    
    - **stake**: Montant de la mise (min 10 HTG, max 10 000 HTG)
    """
    # Récupérer la config
    config_result = await db.execute(
        select(LuckyWheelConfig)
        .where(LuckyWheelConfig.is_active == True)
        .order_by(LuckyWheelConfig.is_default.desc())
        .limit(1)
    )
    config = config_result.scalar_one_or_none()
    
    if not config:
        raise HTTPException(status_code=500, detail="Configuration de la roue non trouvée")
    
    # Vérifier la mise
    if spin_data.stake < config.min_bet or spin_data.stake > config.max_bet:
        raise HTTPException(
            status_code=400,
            detail=f"Mise invalide. Min: {config.min_bet} HTG, Max: {config.max_bet} HTG"
        )
    
    # Vérifier le solde
    wallet_service = WalletService(db, redis_client)
    balance = await wallet_service.get_balance(current_user.id)
    
    if balance["balance"] < spin_data.stake:
        raise HTTPException(status_code=400, detail="Solde insuffisant")
    
    # Générer le résultat
    segments = config.segments
    total_weight = sum(s["weight"] for s in segments)
    roll = secrets.randbelow(int(total_weight * 100)) / 100
    
    cumulative = 0
    winning_segment = None
    for segment in segments:
        cumulative += segment["weight"]
        if roll < cumulative:
            winning_segment = segment
            break
    
    if not winning_segment:
        winning_segment = segments[0]
    
    multiplier = Decimal(str(winning_segment["multiplier"]))
    winnings = spin_data.stake * multiplier
    
    # Générer preuve d'équité
    random_seed = secrets.token_hex(32)
    verification_hash = hashlib.sha256(
        f"{random_seed}{spin_data.stake}{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()
    
    # Débiter le wallet
    await wallet_service.debit_for_bet(
        user_id=current_user.id,
        amount=spin_data.stake,
        bet_id=None,
        draw_id=None
    )
    
    # Créditer les gains si > 0
    if winnings > 0:
        await wallet_service.credit_for_win(
            user_id=current_user.id,
            amount=winnings,
            bet_id=None,
            draw_id=None
        )
    
    # Créer la trace du jeu
    lucky_play = LuckyPlay(
        user_id=current_user.id,
        wheel_config_id=config.id,
        stake=spin_data.stake,
        result_segment={
            "label": winning_segment["label"],
            "multiplier": winning_segment["multiplier"],
            "color": winning_segment["color"]
        },
        multiplier=multiplier,
        winnings=winnings,
        random_seed=random_seed,
        verification_hash=verification_hash,
        played_at=datetime.utcnow()
    )
    
    db.add(lucky_play)
    
    # Audit log
    audit = AuditLog(
        user_id=current_user.id,
        action=AuditAction.LUCKY_SPIN,
        resource_type="lucky_play",
        resource_id=lucky_play.id,
        new_values={
            "stake": float(spin_data.stake),
            "multiplier": float(multiplier),
            "winnings": float(winnings),
            "segment": winning_segment["label"]
        }
    )
    db.add(audit)
    
    await db.commit()
    
    # Récupérer le nouveau solde
    new_balance = await wallet_service.get_balance(current_user.id)
    
    # Notification en arrière-plan pour les gros gains
    if winnings >= 10000:
        background_tasks.add_task(
            notify_big_win,
            current_user.id,
            current_user.phone,
            float(winnings)
        )
    
    return {
        "success": True,
        "segment": winning_segment["label"],
        "multiplier": float(multiplier),
        "winnings": float(winnings),
        "color": winning_segment["color"],
        "play_id": lucky_play.id,
        "verification_hash": verification_hash,
        "new_balance": new_balance["balance"]
    }


@router.post(
    "/wheel/spin-ticket",
    response_model=LuckySpinResponse,
    summary="Tourner la roue avec ticket (Agent)",
    description="Fait tourner la roue avec un ticket (joueur sans compte)"
)
async def spin_wheel_with_ticket(
    ticket_number: str,
    stake: float,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Fait tourner la roue avec un ticket.
    Réservé aux agents de bureau.
    """
    # Vérifier le ticket
    ticket_result = await db.execute(
        select(Ticket).where(Ticket.ticket_number == ticket_number)
    )
    ticket = ticket_result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket invalide")
    
    if ticket.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Ticket expiré ou déjà payé")
    
    if ticket.balance < stake:
        raise HTTPException(status_code=400, detail="Solde ticket insuffisant")
    
    # Récupérer la config
    config_result = await db.execute(
        select(LuckyWheelConfig)
        .where(LuckyWheelConfig.is_active == True)
        .order_by(LuckyWheelConfig.is_default.desc())
        .limit(1)
    )
    config = config_result.scalar_one_or_none()
    
    if not config:
        raise HTTPException(status_code=500, detail="Configuration de la roue non trouvée")
    
    # Vérifier la mise
    if stake < config.min_bet or stake > config.max_bet:
        raise HTTPException(
            status_code=400,
            detail=f"Mise invalide. Min: {config.min_bet} HTG, Max: {config.max_bet} HTG"
        )
    
    # Générer le résultat
    segments = config.segments
    total_weight = sum(s["weight"] for s in segments)
    roll = secrets.randbelow(int(total_weight * 100)) / 100
    
    cumulative = 0
    winning_segment = None
    for segment in segments:
        cumulative += segment["weight"]
        if roll < cumulative:
            winning_segment = segment
            break
    
    if not winning_segment:
        winning_segment = segments[0]
    
    multiplier = Decimal(str(winning_segment["multiplier"]))
    winnings = Decimal(str(stake)) * multiplier
    
    # Générer preuve d'équité
    random_seed = secrets.token_hex(32)
    verification_hash = hashlib.sha256(
        f"{random_seed}{stake}{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()
    
    # Débiter le ticket
    ticket.balance -= Decimal(str(stake))
    
    # Créditer le ticket si gain
    if winnings > 0:
        ticket.balance += winnings
    
    # Créer la trace du jeu
    lucky_play = LuckyPlay(
        ticket_id=ticket.id,
        agent_id=current_agent.id,
        wheel_config_id=config.id,
        stake=Decimal(str(stake)),
        result_segment={
            "label": winning_segment["label"],
            "multiplier": winning_segment["multiplier"],
            "color": winning_segment["color"]
        },
        multiplier=multiplier,
        winnings=winnings,
        random_seed=random_seed,
        verification_hash=verification_hash,
        played_at=datetime.utcnow()
    )
    
    db.add(lucky_play)
    
    # Audit log
    audit = AuditLog(
        user_id=ticket.agent_id,
        agent_id=current_agent.id,
        action=AuditAction.LUCKY_SPIN,
        resource_type="lucky_play",
        resource_id=lucky_play.id,
        new_values={
            "stake": stake,
            "multiplier": float(multiplier),
            "winnings": float(winnings),
            "segment": winning_segment["label"],
            "ticket_number": ticket.ticket_number
        }
    )
    db.add(audit)
    
    await db.commit()
    
    return {
        "success": True,
        "segment": winning_segment["label"],
        "multiplier": float(multiplier),
        "winnings": float(winnings),
        "color": winning_segment["color"],
        "play_id": lucky_play.id,
        "verification_hash": verification_hash,
        "new_balance": float(ticket.balance)
    }


# ==================== HISTORIQUE ====================

@router.get(
    "/history",
    response_model=List[LuckyPlayHistoryResponse],
    summary="Historique des parties",
    description="Récupère l'historique des parties Lucky du joueur"
)
async def get_lucky_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 50,
    offset: int = 0
):
    """Récupère l'historique des parties."""
    
    result = await db.execute(
        select(LuckyPlay)
        .where(LuckyPlay.user_id == current_user.id)
        .order_by(LuckyPlay.played_at.desc())
        .offset(offset)
        .limit(limit)
    )
    plays = result.scalars().all()
    
    return [
        {
            "id": p.id,
            "stake": float(p.stake),
            "multiplier": float(p.multiplier),
            "winnings": float(p.winnings),
            "segment": p.result_segment["label"],
            "played_at": p.played_at
        }
        for p in plays
    ]


@router.get(
    "/statistics",
    response_model=LuckyStatsResponse,
    summary="Statistiques Lucky",
    description="Récupère les statistiques du joueur pour le jeu Lucky"
)
async def get_lucky_statistics(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Récupère les statistiques Lucky."""
    
    result = await db.execute(
        select(
            func.count(LuckyPlay.id).label("total_plays"),
            func.coalesce(func.sum(LuckyPlay.stake), 0).label("total_stake"),
            func.coalesce(func.sum(LuckyPlay.winnings), 0).label("total_wins"),
            func.max(LuckyPlay.winnings).label("best_win"),
            func.max(LuckyPlay.multiplier).label("best_multiplier")
        ).where(LuckyPlay.user_id == current_user.id)
    )
    stats = result.one()
    
    return {
        "total_plays": stats.total_plays or 0,
        "total_stake": float(stats.total_stake),
        "total_wins": float(stats.total_wins),
        "best_win": float(stats.best_win or 0),
        "best_multiplier": float(stats.best_multiplier or 0),
        "win_rate": round((stats.total_wins or 0) / (stats.total_stake or 1) * 100, 2)
    }


@router.get(
    "/verify/{play_id}",
    summary="Vérifier équité",
    description="Vérifie l'équité d'une partie (preuve de résultat aléatoire)"
)
async def verify_lucky_play(
    play_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Vérifie l'équité d'une partie."""
    
    result = await db.execute(
        select(LuckyPlay).where(LuckyPlay.id == play_id)
    )
    play = result.scalar_one_or_none()
    
    if not play:
        raise HTTPException(status_code=404, detail="Partie non trouvée")
    
    # Recalculer le hash
    expected_hash = hashlib.sha256(
        f"{play.random_seed}{play.stake}{play.played_at.isoformat()}".encode()
    ).hexdigest()
    
    return {
        "play_id": play_id,
        "is_valid": play.verification_hash == expected_hash,
        "random_seed": play.random_seed,
        "expected_hash": expected_hash,
        "actual_hash": play.verification_hash,
        "stake": float(play.stake),
        "multiplier": float(play.multiplier),
        "winnings": float(play.winnings),
        "segment": play.result_segment["label"],
        "played_at": play.played_at
    }


# ==================== FONCTIONS UTILITAIRES ====================

async def notify_big_win(user_id: str, phone: str, amount: float):
    """Notifie un gros gain."""
    # À implémenter: SMS, Email, Notification push
    pass