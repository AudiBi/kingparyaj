# app/api/websockets/draws.py
"""WebSockets pour les résultats en direct"""

from fastapi import WebSocket, WebSocketDisconnect, APIRouter, Depends
from typing import Dict, Set, List
import json
import asyncio
from datetime import datetime

from app.core.redis_client import get_redis
from app.core.database import get_db
from app.models.keno import KenoDraw
from app.models.lucky import LuckyPlay

router = APIRouter()


# ==================== GESTIONNAIRE DE CONNEXIONS ====================

class ConnectionManager:
    """Gère toutes les connexions WebSocket"""
    
    def __init__(self):
        self.active_connections: Dict[str, Dict[WebSocket, str]] = {}
        self.all_connections: Set[WebSocket] = set()
    
    async def connect(self, websocket: WebSocket, draw_id: str = "all", user_id: str = None):
        """Accepte une connexion WebSocket"""
        await websocket.accept()
        
        if draw_id not in self.active_connections:
            self.active_connections[draw_id] = {}
        self.active_connections[draw_id][websocket] = user_id or "anonymous"
        self.all_connections.add(websocket)
        
        # Envoyer les derniers résultats immédiatement
        await self.send_latest_results(websocket)
    
    def disconnect(self, websocket: WebSocket, draw_id: str = "all"):
        """Déconnecte un client"""
        if draw_id in self.active_connections:
            self.active_connections[draw_id].pop(websocket, None)
            if not self.active_connections[draw_id]:
                del self.active_connections[draw_id]
        self.all_connections.discard(websocket)
    
    async def broadcast(self, message: dict, draw_id: str = "all"):
        """Diffuse un message à tous les clients d'un tirage"""
        connections = []
        
        if draw_id == "all":
            connections = list(self.all_connections)
        elif draw_id in self.active_connections:
            connections = list(self.active_connections[draw_id].keys())
        
        disconnected = []
        for connection in connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        
        for connection in disconnected:
            self.disconnect(connection, draw_id)
    
    async def send_to_user(self, user_id: str, message: dict):
        """Envoie un message à un utilisateur spécifique"""
        for draw_id, connections in self.active_connections.items():
            for websocket, uid in connections.items():
                if uid == user_id:
                    try:
                        await websocket.send_json(message)
                    except Exception:
                        pass
    
    async def send_latest_results(self, websocket: WebSocket):
        """Envoie les derniers résultats à un nouveau client"""
        try:
            # Envoyer le dernier tirage Keno
            await self._send_latest_keno(websocket)
            
            # Envoyer le prochain tirage
            await self._send_next_draw(websocket)
            
            # Envoyer l'historique
            await self._send_history(websocket)
            
        except Exception as e:
            print(f"Error sending latest results: {e}")
    
    async def _send_latest_keno(self, websocket: WebSocket):
        """Envoie le dernier tirage Keno"""
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import select, desc
        
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(KenoDraw)
                .where(KenoDraw.numbers.isnot(None))
                .order_by(desc(KenoDraw.draw_time))
                .limit(1)
            )
            draw = result.scalar_one_or_none()
            
            if draw:
                await websocket.send_json({
                    "type": "latest_keno",
                    "data": {
                        "draw_id": draw.id,
                        "draw_number": draw.draw_number,
                        "numbers": draw.numbers,
                        "draw_time": draw.draw_time.isoformat(),
                        "total_bets": draw.total_bets,
                        "total_payout": float(draw.total_payout)
                    }
                })
    
    async def _send_next_draw(self, websocket: WebSocket):
        """Envoie le prochain tirage"""
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import select
        
        async with AsyncSessionLocal() as db:
            from datetime import datetime
            result = await db.execute(
                select(KenoDraw)
                .where(KenoDraw.draw_time > datetime.utcnow())
                .where(KenoDraw.status == "PENDING")
                .order_by(KenoDraw.draw_time)
                .limit(1)
            )
            draw = result.scalar_one_or_none()
            
            if draw:
                await websocket.send_json({
                    "type": "next_draw",
                    "data": {
                        "draw_id": draw.id,
                        "draw_number": draw.draw_number,
                        "draw_time": draw.draw_time.isoformat()
                    }
                })
    
    async def _send_history(self, websocket: WebSocket):
        """Envoie l'historique des tirages"""
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import select, desc
        
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(KenoDraw)
                .where(KenoDraw.numbers.isnot(None))
                .order_by(desc(KenoDraw.draw_time))
                .limit(20)
            )
            draws = result.scalars().all()
            
            if draws:
                await websocket.send_json({
                    "type": "history",
                    "data": [
                        {
                            "draw_number": d.draw_number,
                            "numbers": d.numbers,
                            "draw_time": d.draw_time.isoformat(),
                            "total_bets": d.total_bets
                        }
                        for d in draws
                    ]
                })


# Instance globale du gestionnaire
manager = ConnectionManager()


# ==================== WEBSOCKET ENDPOINT ====================

@router.websocket("/ws/draws/{draw_id}")
async def websocket_draws(
    websocket: WebSocket,
    draw_id: str = "all"
):
    """
    WebSocket pour suivre les tirages en direct.
    
    - draw_id: ID du tirage spécifique ou "all" pour tous
    """
    await manager.connect(websocket, draw_id)
    
    try:
        while True:
            # Recevoir les messages du client (ping/pong)
            data = await websocket.receive_text()
            
            if data == "ping":
                await websocket.send_text("pong")
            elif data.startswith("subscribe:"):
                # Changer de tirage
                new_draw_id = data.split(":")[1]
                manager.disconnect(websocket, draw_id)
                await manager.connect(websocket, new_draw_id)
            elif data == "history":
                await manager._send_history(websocket)
            elif data == "latest":
                await manager._send_latest_keno(websocket)
                
    except WebSocketDisconnect:
        manager.disconnect(websocket, draw_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket, draw_id)


# ==================== FONCTIONS DE DIFFUSION ====================

async def broadcast_draw_result(draw_result: dict):
    """
    Diffuse les résultats d'un tirage à tous les clients connectés.
    Appelé après chaque tirage.
    """
    await manager.broadcast({
        "type": "draw_completed",
        "data": draw_result,
        "timestamp": datetime.utcnow().isoformat()
    }, draw_id=draw_result.get("draw_id", "all"))


async def broadcast_lucky_result(lucky_result: dict):
    """
    Diffuse le résultat d'un tour de Lucky Wheel.
    """
    await manager.broadcast({
        "type": "lucky_result",
        "data": lucky_result,
        "timestamp": datetime.utcnow().isoformat()
    }, draw_id="all")


async def broadcast_jackpot_alert(jackpot_data: dict):
    """
    Diffuse une alerte jackpot.
    """
    await manager.broadcast({
        "type": "jackpot_alert",
        "data": jackpot_data,
        "timestamp": datetime.utcnow().isoformat()
    }, draw_id="all")