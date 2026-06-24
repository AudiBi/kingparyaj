# app/routes/agent.py
"""Routes pour les agents de bureau - Ajouts Lucky Live Results"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_
from datetime import datetime, timedelta
from typing import List, Optional
import json

from app.core.database import get_db
from app.core.security import get_current_agent
from app.models.user import User
from app.models.lucky import LuckyPlay, LuckyWheelConfig
from app.models.bureau import CashierSession
from app.models.ticket import Ticket
from app.api.websockets.manager import manager, broadcast_lucky_result

router = APIRouter(prefix="/agent", tags=["Agent"])


# ==================== LUCKY LIVE RESULTS ====================

@router.get("/api/lucky/latest")
async def agent_lucky_latest(
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Récupère le dernier résultat Lucky"""
    
    result = await db.execute(
        select(LuckyPlay)
        .order_by(desc(LuckyPlay.played_at))
        .limit(1)
    )
    play = result.scalar_one_or_none()
    
    if not play:
        return {"has_result": False, "message": "Aucun résultat disponible"}
    
    # Récupérer la configuration de la roue
    config_result = await db.execute(
        select(LuckyWheelConfig).where(LuckyWheelConfig.id == play.wheel_config_id)
    )
    config = config_result.scalar_one_or_none()
    
    # Trouver le segment
    segment = None
    if config and config.segments:
        for seg in config.segments:
            if seg["label"] == play.result_segment.get("label"):
                segment = seg
                break
    
    return {
        "has_result": True,
        "play_id": play.id,
        "segment": play.result_segment.get("label"),
        "multiplier": float(play.multiplier),
        "winnings": float(play.winnings),
        "stake": float(play.stake),
        "color": segment.get("color") if segment else "#94a3b8",
        "player": play.user.full_name if play.user else "Ticket",
        "played_at": play.played_at.isoformat()
    }


@router.get("/api/lucky/history")
async def agent_lucky_history(
    limit: int = 20,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Récupère l'historique des résultats Lucky"""
    
    result = await db.execute(
        select(LuckyPlay)
        .order_by(desc(LuckyPlay.played_at))
        .limit(limit)
    )
    plays = result.scalars().all()
    
    history = []
    for play in plays:
        # Récupérer la config
        config_result = await db.execute(
            select(LuckyWheelConfig).where(LuckyWheelConfig.id == play.wheel_config_id)
        )
        config = config_result.scalar_one_or_none()
        
        segment = None
        if config and config.segments:
            for seg in config.segments:
                if seg["label"] == play.result_segment.get("label"):
                    segment = seg
                    break
        
        history.append({
            "play_id": play.id,
            "segment": play.result_segment.get("label"),
            "multiplier": float(play.multiplier),
            "winnings": float(play.winnings),
            "stake": float(play.stake),
            "color": segment.get("color") if segment else "#94a3b8",
            "player": play.user.full_name if play.user else "Ticket",
            "played_at": play.played_at.isoformat()
        })
    
    return history


@router.post("/api/lucky/spin")
async def agent_lucky_spin(
    request: Request,
    data: dict,
    current_user: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db)
):
    """Effectue un tour de Lucky Wheel pour un joueur"""
    
    player_type = data.get("player_type")
    identifier = data.get("identifier")
    stake = data.get("stake")
    
    if not stake or stake < 10:
        raise HTTPException(400, "Mise minimum: 10 HTG")
    
    # Vérifier la session de caisse ouverte
    session_result = await db.execute(
        select(CashierSession).where(
            and_(
                CashierSession.agent_id == current_user.id,
                CashierSession.status == "OPEN"
            )
        )
    )
    session = session_result.scalar_one_or_none()
    
    if not session:
        raise HTTPException(400, "Aucune session de caisse ouverte")
    
    # Récupérer la configuration de la roue
    config_result = await db.execute(
        select(LuckyWheelConfig)
        .where(LuckyWheelConfig.is_active == True)
        .order_by(LuckyWheelConfig.is_default.desc())
        .limit(1)
    )
    config = config_result.scalar_one_or_none()
    
    if not config:
        raise HTTPException(500, "Configuration de la roue non trouvée")
    
    # Générer le résultat
    import secrets
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
    
    multiplier = winning_segment["multiplier"]
    winnings = stake * multiplier
    
    # Gérer le joueur
    user_id = None
    ticket_id = None
    player_name = "Anonyme"
    
    if player_type == "account":
        # Rechercher l'utilisateur par téléphone
        user_result = await db.execute(
            select(User).where(User.phone == identifier)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(404, "Joueur non trouvé")
        user_id = user.id
        player_name = user.full_name or user.phone
        
        # Vérifier le solde
        from app.models.wallet import Wallet
        wallet_result = await db.execute(
            select(Wallet).where(Wallet.user_id == user_id)
        )
        wallet = wallet_result.scalar_one_or_none()
        if not wallet or wallet.balance < stake:
            raise HTTPException(400, "Solde insuffisant")
        
        # Débiter le wallet
        wallet.balance -= stake
        
    elif player_type == "ticket":
        # Rechercher le ticket
        ticket_result = await db.execute(
            select(Ticket).where(Ticket.ticket_number == identifier)
        )
        ticket = ticket_result.scalar_one_or_none()
        if not ticket:
            raise HTTPException(404, "Ticket non trouvé")
        if ticket.balance < stake:
            raise HTTPException(400, "Solde ticket insuffisant")
        ticket_id = ticket.id
        player_name = ticket.player_name or "Ticket"
        
        # Débiter le ticket
        ticket.balance -= stake
    
    else:
        raise HTTPException(400, "Type de joueur invalide")
    
    # Créer la trace du jeu
    import hashlib
    random_seed = secrets.token_hex(32)
    verification_hash = hashlib.sha256(
        f"{random_seed}{stake}{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()
    
    lucky_play = LuckyPlay(
        user_id=user_id,
        ticket_id=ticket_id,
        agent_id=current_user.id,
        wheel_config_id=config.id,
        stake=stake,
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
    
    # Créditer les gains
    if winnings > 0:
        if user_id:
            # Créditer le wallet
            wallet_result = await db.execute(
                select(Wallet).where(Wallet.user_id == user_id)
            )
            wallet = wallet_result.scalar_one()
            wallet.balance += winnings
            wallet.total_won += winnings
        elif ticket_id:
            # Créditer le ticket
            ticket_result = await db.execute(
                select(Ticket).where(Ticket.id == ticket_id)
            )
            ticket = ticket_result.scalar_one()
            ticket.balance += winnings
    
    # Mettre à jour la session de caisse (si paiement en cash)
    # À implémenter selon votre logique
    
    await db.commit()
    
    # Diffuser le résultat via WebSocket
    await broadcast_lucky_result({
        "type": "lucky_result",
        "data": {
            "segment": winning_segment["label"],
            "multiplier": multiplier,
            "winnings": winnings,
            "player": player_name,
            "played_at": datetime.utcnow().isoformat(),
            "stake": stake
        }
    })
    
    return {
        "success": True,
        "segment": winning_segment["label"],
        "multiplier": multiplier,
        "winnings": winnings,
        "color": winning_segment["color"],
        "play_id": lucky_play.id,
        "player": player_name,
        "message": f"Tour terminé ! {('Gain: ' + str(winnings) + ' HTG') if winnings > 0 else 'Perdu'}"
    }