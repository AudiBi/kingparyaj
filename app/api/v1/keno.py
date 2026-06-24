# app/api/v1/keno.py
"""API complète du jeu Keno (80 numéros, 20 tirés)"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from typing import List, Optional
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.database import get_db
from app.core.redis_client import get_redis
from app.core.security import get_current_user, get_current_agent
from app.schemas.keno import (
    KenoBetCreate, KenoBetResponse, KenoDrawResponse,
    KenoResultResponse, KenoHistoryResponse, KenoStatsResponse,
    KenoQuickPickRequest
)
from app.schemas.common import SuccessResponse, PaginatedResponse
from app.services.keno_service import KenoService
from app.services.wallet_service import WalletService
from app.models.user import User
from app.models.keno import KenoDraw, KenoBet, KenoDrawStatus, KenoBetStatus
from app.models.ticket import Ticket
from app.api.websockets.manager import broadcast_draw_result
import redis.asyncio as redis

router = APIRouter(prefix="/keno", tags=["Keno"])


# ==================== FONCTIONS UTILITAIRES ====================

async def notify_bet_placed(user_id: str, bet_id: str, stake: float):
    """Notification en arrière-plan pour un pari placé"""
    # Implémentation simplifiée
    pass


# ==================== TIRAGES ====================

@router.get(
    "/draws/next",
    response_model=Optional[KenoDrawResponse],
    summary="Prochain tirage",
    description="Récupère les informations du prochain tirage Keno"
)
async def get_next_draw(
    db: AsyncSession = Depends(get_db)
):
    """Récupère le prochain tirage à venir."""
    result = await db.execute(
        select(KenoDraw)
        .where(KenoDraw.draw_time > datetime.utcnow())
        .where(KenoDraw.status == KenoDrawStatus.PENDING)
        .order_by(KenoDraw.draw_time)
        .limit(1)
    )
    draw = result.scalar_one_or_none()
    return draw


@router.get(
    "/draws/last",
    response_model=Optional[KenoDrawResponse],
    summary="Dernier tirage",
    description="Récupère les informations du dernier tirage effectué"
)
async def get_last_draw(
    db: AsyncSession = Depends(get_db)
):
    """Récupère le dernier tirage effectué."""
    result = await db.execute(
        select(KenoDraw)
        .where(KenoDraw.status == KenoDrawStatus.COMPLETED)
        .where(KenoDraw.numbers.isnot(None))
        .order_by(KenoDraw.draw_time.desc())
        .limit(1)
    )
    draw = result.scalar_one_or_none()
    return draw


@router.get(
    "/draws/{draw_id}",
    response_model=KenoDrawResponse,
    summary="Détails tirage",
    description="Récupère les détails d'un tirage spécifique"
)
async def get_draw_by_id(
    draw_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Récupère un tirage par son ID."""
    result = await db.execute(
        select(KenoDraw).where(KenoDraw.id == draw_id)
    )
    draw = result.scalar_one_or_none()
    
    if not draw:
        raise HTTPException(status_code=404, detail="Tirage non trouvé")
    
    return draw


@router.get(
    "/draws",
    response_model=List[KenoDrawResponse],
    summary="Liste des tirages",
    description="Récupère la liste des tirages (passés et futurs)"
)
async def get_draws(
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[KenoDrawStatus] = None,
    db: AsyncSession = Depends(get_db)
):
    """Liste les tirages."""
    query = select(KenoDraw)
    
    if status_filter:
        query = query.where(KenoDraw.status == status_filter)
    
    query = query.order_by(KenoDraw.draw_time.desc()).limit(limit)
    
    result = await db.execute(query)
    draws = result.scalars().all()
    
    return draws


# ==================== PARIS ====================

@router.post(
    "/bets",
    response_model=KenoBetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Placer un pari Keno",
    description="Place un pari Keno avec les numéros choisis"
)
async def place_keno_bet(
    bet_data: KenoBetCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Place un pari Keno.
    
    - **draw_id**: ID du tirage
    - **picks**: Liste des numéros choisis (1-10 numéros entre 1 et 80)
    - **stake**: Montant de la mise (min 10 HTG)
    """
    # Vérifier le tirage
    draw_result = await db.execute(
        select(KenoDraw).where(KenoDraw.id == bet_data.draw_id)
    )
    draw = draw_result.scalar_one_or_none()
    
    if not draw:
        raise HTTPException(status_code=404, detail="Tirage non trouvé")
    
    if draw.status != KenoDrawStatus.PENDING:
        raise HTTPException(status_code=400, detail="Ce tirage n'est plus disponible")
    
    if draw.draw_time < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Ce tirage est déjà passé")
    
    # Vérifier le solde
    wallet_service = WalletService(db, redis_client)
    balance = await wallet_service.get_balance(current_user.id)
    
    if balance["balance"] < bet_data.stake:
        raise HTTPException(status_code=400, detail="Solde insuffisant")
    
    # Débiter le wallet
    transaction = await wallet_service.debit_for_bet(
        user_id=current_user.id,
        amount=bet_data.stake,
        bet_id=None,  # Sera mis à jour après création
        draw_id=bet_data.draw_id
    )
    
    # Créer le pari
    bet = KenoBet(
        user_id=current_user.id,
        draw_id=bet_data.draw_id,
        picks=bet_data.picks,
        stake=bet_data.stake,
        status=KenoBetStatus.PENDING,
        placed_at=datetime.utcnow()
    )
    
    db.add(bet)
    await db.flush()
    
    # Mettre à jour la transaction avec le bet_id
    transaction.bet_id = bet.id
    await db.flush()
    
    # Mettre à jour les stats utilisateur
    current_user.total_bets_count += 1
    current_user.total_bets_amount += bet_data.stake
    
    # Mettre à jour les stats du tirage
    draw.total_bets += 1
    draw.total_amount += bet_data.stake
    
    await db.commit()
    
    # Notification en arrière-plan
    background_tasks.add_task(
        notify_bet_placed,
        current_user.id,
        bet.id,
        bet_data.stake
    )
    
    return bet


@router.post(
    "/quick-pick",
    response_model=KenoBetResponse,
    summary="Quick Pick",
    description="Génère des numéros aléatoires et place un pari"
)
async def quick_pick(
    request: KenoQuickPickRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Génère des numéros aléatoires et place un pari.
    
    - **numbers_count**: Nombre de numéros à générer (1-10)
    - **stake**: Montant de la mise
    """
    import secrets
    
    # Générer des numéros aléatoires uniques
    numbers = list(range(1, 81))
    for i in range(len(numbers) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        numbers[i], numbers[j] = numbers[j], numbers[i]
    
    picks = sorted(numbers[:request.numbers_count])
    
    # Créer le pari
    bet_data = KenoBetCreate(
        draw_id=request.draw_id,
        picks=picks,
        stake=request.stake
    )
    
    return await place_keno_bet(bet_data, BackgroundTasks(), current_user, db, redis_client)


@router.get(
    "/bets/history",
    response_model=KenoHistoryResponse,
    summary="Historique des paris",
    description="Récupère l'historique des paris du joueur"
)
async def get_bet_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0)
):
    """Récupère l'historique des paris."""
    
    # Récupérer les paris
    bets_result = await db.execute(
        select(KenoBet)
        .where(KenoBet.user_id == current_user.id)
        .order_by(KenoBet.placed_at.desc())
        .offset(offset)
        .limit(limit)
    )
    bets = bets_result.scalars().all()
    
    # Récupérer les stats globales
    stats_result = await db.execute(
        select(
            func.count(KenoBet.id).label("total_bets"),
            func.coalesce(func.sum(KenoBet.stake), 0).label("total_stake"),
            func.coalesce(func.sum(KenoBet.winnings), 0).label("total_wins"),
            func.max(KenoBet.winnings).label("best_win"),
            func.max(KenoBet.multiplier).label("best_multiplier")
        ).where(KenoBet.user_id == current_user.id)
    )
    stats = stats_result.one()
    
    total_bets = stats.total_bets or 0
    total_stake = float(stats.total_stake or 0)
    total_wins = float(stats.total_wins or 0)
    win_rate = round((stats.total_wins or 0) / (stats.total_stake or 1) * 100, 2) if stats.total_stake > 0 else 0
    
    return {
        "total_bets": total_bets,
        "total_stake": total_stake,
        "total_wins": total_wins,
        "best_win": float(stats.best_win or 0),
        "best_multiplier": float(stats.best_multiplier or 0),
        "recent_bets": bets,
        "win_rate": win_rate
    }


@router.get(
    "/bets/{bet_id}",
    response_model=KenoBetResponse,
    summary="Détails d'un pari",
    description="Récupère les détails d'un pari spécifique"
)
async def get_bet_by_id(
    bet_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Récupère un pari par son ID."""
    result = await db.execute(
        select(KenoBet).where(KenoBet.id == bet_id)
    )
    bet = result.scalar_one_or_none()
    
    if not bet:
        raise HTTPException(status_code=404, detail="Pari non trouvé")
    
    if bet.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Accès non autorisé")
    
    return bet


# ==================== RÉSULTATS ====================

@router.get(
    "/results/latest",
    response_model=KenoResultResponse,
    summary="Derniers résultats",
    description="Récupère les résultats du dernier tirage"
)
async def get_latest_results(
    db: AsyncSession = Depends(get_db)
):
    """Récupère les résultats du dernier tirage."""
    
    # Dernier tirage complété
    draw_result = await db.execute(
        select(KenoDraw)
        .where(KenoDraw.status == KenoDrawStatus.COMPLETED)
        .where(KenoDraw.numbers.isnot(None))
        .order_by(KenoDraw.draw_time.desc())
        .limit(1)
    )
    draw = draw_result.scalar_one_or_none()
    
    if not draw:
        raise HTTPException(status_code=404, detail="Aucun tirage trouvé")
    
    # Statistiques du tirage
    stats_result = await db.execute(
        select(
            func.count(KenoBet.id).label("total_bets"),
            func.coalesce(func.sum(KenoBet.stake), 0).label("total_stake"),
            func.coalesce(func.sum(KenoBet.winnings), 0).label("total_payout"),
            func.count().filter(KenoBet.winnings > 0).label("winners")
        ).where(KenoBet.draw_id == draw.id)
    )
    stats = stats_result.one()
    
    return {
        "draw_id": draw.id,
        "draw_number": draw.draw_number,
        "draw_time": draw.draw_time,
        "numbers": draw.numbers,
        "total_bets": stats.total_bets or 0,
        "total_stake": float(stats.total_stake),
        "total_payout": float(stats.total_payout),
        "winners": stats.winners or 0
    }


@router.get(
    "/results/{draw_id}",
    response_model=KenoResultResponse,
    summary="Résultats d'un tirage",
    description="Récupère les résultats d'un tirage spécifique"
)
async def get_draw_results(
    draw_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Récupère les résultats d'un tirage."""
    
    draw_result = await db.execute(
        select(KenoDraw).where(KenoDraw.id == draw_id)
    )
    draw = draw_result.scalar_one_or_none()
    
    if not draw:
        raise HTTPException(status_code=404, detail="Tirage non trouvé")
    
    if draw.status != KenoDrawStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="Ce tirage n'est pas encore terminé")
    
    # Statistiques du tirage
    stats_result = await db.execute(
        select(
            func.count(KenoBet.id).label("total_bets"),
            func.coalesce(func.sum(KenoBet.stake), 0).label("total_stake"),
            func.coalesce(func.sum(KenoBet.winnings), 0).label("total_payout"),
            func.count().filter(KenoBet.winnings > 0).label("winners")
        ).where(KenoBet.draw_id == draw.id)
    )
    stats = stats_result.one()
    
    return {
        "draw_id": draw.id,
        "draw_number": draw.draw_number,
        "draw_time": draw.draw_time,
        "numbers": draw.numbers,
        "total_bets": stats.total_bets or 0,
        "total_stake": float(stats.total_stake),
        "total_payout": float(stats.total_payout),
        "winners": stats.winners or 0
    }


# ==================== STATISTIQUES ====================

@router.get(
    "/statistics",
    response_model=KenoStatsResponse,
    summary="Statistiques globales",
    description="Récupère les statistiques globales du jeu Keno"
)
async def get_global_statistics(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db)
):
    """Récupère les statistiques globales."""
    
    since_date = datetime.utcnow() - timedelta(days=days)
    
    # Statistiques globales
    global_result = await db.execute(
        select(
            func.count(KenoDraw.id).filter(KenoDraw.status == KenoDrawStatus.COMPLETED).label("total_draws"),
            func.sum(KenoBet.stake).label("total_volume"),
            func.sum(KenoBet.winnings).label("total_payout"),
            func.count(KenoBet.id).label("total_bets")
        ).where(KenoBet.placed_at >= since_date)
    )
    global_stats = global_result.one()
    
    # Numéros les plus joués (exemple simplifié)
    popular_numbers = []
    least_popular_numbers = []
    
    total_volume = float(global_stats.total_volume or 0)
    total_payout = float(global_stats.total_payout or 0)
    house_edge = ((total_volume - total_payout) / total_volume * 100) if total_volume > 0 else 0
    rtp = 100 - house_edge
    
    return {
        "total_draws": global_stats.total_draws or 0,
        "total_bets": global_stats.total_bets or 0,
        "total_volume": total_volume,
        "total_payout": total_payout,
        "house_edge": round(house_edge, 2),
        "popular_numbers": popular_numbers,
        "least_popular_numbers": least_popular_numbers,
        "rtp": round(rtp, 2)
    }


# ==================== AGENT ENDPOINTS (joueurs sans compte) ====================

@router.post(
    "/ticket-bets",
    response_model=KenoBetResponse,
    summary="Pari avec ticket (Agent)",
    description="Place un pari Keno avec un ticket (joueur sans compte)"
)
async def place_ticket_keno_bet(
    ticket_number: str,
    bet_data: KenoBetCreate,
    current_agent: User = Depends(get_current_agent),
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis)
):
    """
    Place un pari avec un ticket.
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
    
    if ticket.balance < bet_data.stake:
        raise HTTPException(status_code=400, detail="Solde ticket insuffisant")
    
    # Vérifier le tirage
    draw_result = await db.execute(
        select(KenoDraw).where(KenoDraw.id == bet_data.draw_id)
    )
    draw = draw_result.scalar_one_or_none()
    
    if not draw or draw.status != KenoDrawStatus.PENDING:
        raise HTTPException(status_code=400, detail="Tirage non disponible")
    
    # Débiter le ticket
    ticket.balance -= bet_data.stake
    
    # Créer le pari
    bet = KenoBet(
        ticket_id=ticket.id,
        draw_id=bet_data.draw_id,
        picks=bet_data.picks,
        stake=bet_data.stake,
        agent_id=current_agent.id,
        status=KenoBetStatus.PENDING,
        placed_at=datetime.utcnow()
    )
    
    db.add(bet)
    
    # Mettre à jour les stats du tirage
    draw.total_bets += 1
    draw.total_amount += bet_data.stake
    
    await db.commit()
    
    return bet