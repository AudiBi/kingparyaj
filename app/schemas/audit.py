# app/schemas/audit.py
"""Schémas pour le journal d'audit"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class AuditFilter(BaseModel):
    """Filtres pour l'audit"""
    user_id: Optional[str] = None
    action: Optional[str] = None
    resource_type: Optional[str] = None
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    ip_address: Optional[str] = None


class AuditLogResponse(BaseModel):
    """Réponse audit log"""
    id: str
    user_id: Optional[str]
    agent_id: Optional[str]
    user_phone: Optional[str]
    action: str
    resource_type: Optional[str]
    resource_id: Optional[str]
    old_values: Optional[Dict[str, Any]]
    new_values: Optional[Dict[str, Any]]
    reason: Optional[str]
    extra_data: Optional[Dict[str, Any]]
    ip_address: str
    user_agent: Optional[str]
    session_id: Optional[str]
    created_at: datetime
    leh_exported: bool
    leh_exported_at: Optional[datetime]
    
    class Config:
        from_attributes = True


class AuditLogListResponse(BaseModel):
    """Liste des logs d'audit"""
    items: List[AuditLogResponse]
    total: int
    page: int
    page_size: int