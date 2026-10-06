# app/models/setting.py
"""Réglages durables de la plateforme (clé / valeur).

Pourquoi : les réglages « généraux » vivent dans Redis avec une expiration
de 24 h ; un réglage qui a un effet sur l'argent (ex. taux de commission par
défaut des agents) ne doit pas disparaître si Redis est vidé ou redémarre.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from app.core.database import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    updated_by = Column(String(36), nullable=True)
