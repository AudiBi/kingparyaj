# app/core/logger.py
import logging
import sys
from app.config import settings

# Configuration du logger
def setup_logging():
    """Configure le logging pour l'application"""
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    
    # Handler console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format))
    
    # Handler fichier
    file_handler = logging.FileHandler(settings.LOG_FILE)
    file_handler.setFormatter(logging.Formatter(log_format))
    
    # Configuration root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    
    return root_logger


# Logger par défaut
logger = setup_logging()


def get_logger(name: str) -> logging.Logger:
    """Récupère un logger nommé"""
    return logging.getLogger(name)