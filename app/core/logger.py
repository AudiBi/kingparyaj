# app/core/logger.py
import logging
import sys
import os
from pathlib import Path
from typing import Optional


class SafeFileHandler(logging.Handler):
    """Handler de fichier sécurisé qui crée le dossier si nécessaire"""
    
    def __init__(self, filename: str, mode: str = 'a', encoding: str = None):
        self.filename = filename
        self.mode = mode
        self.encoding = encoding
        self._create_log_dir()
        super().__init__()
        self._init_handler()
    
    def _create_log_dir(self):
        """Crée le dossier logs si nécessaire"""
        log_dir = Path(self.filename).parent
        if not log_dir.exists():
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                print(f"Dossier logs créé : {log_dir}")
            except Exception as e:
                print(f"Impossible de créer le dossier logs: {e}")
    
    def _init_handler(self):
        """Initialise le handler de fichier"""
        try:
            self.file_handler = logging.FileHandler(
                self.filename, 
                mode=self.mode, 
                encoding=self.encoding or 'utf-8'
            )
        except Exception as e:
            print(f"Impossible de créer le fichier de log: {e}")
            self.file_handler = None
    
    def emit(self, record):
        if self.file_handler:
            self.file_handler.emit(record)


def setup_logging():
    """Configure le logging pour l'application"""
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    
    # Forcer un niveau de log minimal pour Alembic
    if 'alembic' in sys.argv[0] or 'migrations' in sys.argv[0]:
        log_level = logging.WARNING
    else:
        from app.config import settings
        log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    
    # Handler console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    
    # Configuration root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(console_handler)
    
    # Ajouter le handler fichier (seulement si pas Alembic)
    if 'alembic' not in sys.argv[0] and 'migrations' not in sys.argv[0]:
        try:
            from app.config import settings
            file_handler = SafeFileHandler(settings.LOG_FILE)
            file_handler.setFormatter(logging.Formatter(log_format, date_format))
            root_logger.addHandler(file_handler)
        except Exception as e:
            print(f"Warning: Impossible d'initialiser le file logging: {e}")
    
    # Désactiver les logs trop verbeux
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    return root_logger


# Logger par défaut
logger = setup_logging()


def get_logger(name: str) -> logging.Logger:
    """Récupère un logger nommé"""
    return logging.getLogger(name)