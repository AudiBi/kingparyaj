# app/api/websockets/manager.py
import datetime

from fastapi import WebSocket
from typing import Dict, List
import json


class ConnectionManager:
    """Gestionnaire de connexions WebSocket"""
    
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket, draw_id: str = "all"):
        await websocket.accept()
        
        if draw_id not in self.active_connections:
            self.active_connections[draw_id] = []
        self.active_connections[draw_id].append(websocket)
    
    def disconnect(self, websocket: WebSocket, draw_id: str = "all"):
        if draw_id in self.active_connections:
            self.active_connections[draw_id].remove(websocket)
    
    async def broadcast(self, message: dict, draw_id: str = "all"):
        """Diffuse un message à tous les clients connectés"""
        if draw_id not in self.active_connections:
            return
        
        disconnected = []
        for connection in self.active_connections[draw_id]:
            try:
                await connection.send_json(message)
            except:
                disconnected.append(connection)
        
        for connection in disconnected:
            self.disconnect(connection, draw_id)


manager = ConnectionManager()


async def broadcast_draw_result(draw_result: dict):
    """Diffuse les résultats d'un tirage"""
    await manager.broadcast({
        "type": "draw_completed",
        "data": draw_result
    }, draw_id=draw_result.get("draw_id", "all"))


async def broadcast_lucky_result(result_data: dict):
    """Diffuse un résultat Lucky à tous les clients"""
    await manager.broadcast({
        "type": "lucky_result",
        "data": result_data,
        "timestamp": datetime.utcnow().isoformat()
    }, draw_id="all")


async def broadcast_lucky_history(history_data: list):
    """Diffuse l'historique Lucky"""
    await manager.broadcast({
        "type": "lucky_history",
        "data": history_data,
        "timestamp": datetime.utcnow().isoformat()
    }, draw_id="all")