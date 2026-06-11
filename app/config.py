# app/config.py
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import List, Optional


class Settings(BaseSettings):
    """Configuration de l'application"""
    
    # Application
    APP_NAME: str = "Parier Keno & Lucky"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = Field(default=False)
    ENVIRONMENT: str = Field(default="development")
    
    # URLs
    BASE_URL: str = Field(default="http://localhost:8000")
    FRONTEND_URL: str = Field(default="http://localhost:3000")
    
    # Base de données
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://parier_user:password@localhost:5432/parier_keno"
    )
    DATABASE_POOL_SIZE: int = Field(default=20)
    DATABASE_MAX_OVERFLOW: int = Field(default=10)
    DATABASE_POOL_TIMEOUT: int = Field(default=30)
    
    # Redis
    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    REDIS_PASSWORD: Optional[str] = Field(default=None)
    
    # Sécurité
    SECRET_KEY: str = Field(default="change-me-in-production")
    ALGORITHM: str = Field(default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7)
    
    # Bcrypt
    BCRYPT_ROUNDS: int = Field(default=12)
    
    # CORS
    BACKEND_CORS_ORIGINS: List[str] = Field(default=[
        "http://localhost:3000",
        "http://localhost:8000",
        "http://localhost:8501",
    ])
    
    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v
    
    # MonCash (Digicel Haïti)
    MONCASH_ENABLED: bool = Field(default=False)
    MONCASH_API_URL: str = Field(default="https://api.moncash.digicelhaiti.com/v1")
    MONCASH_MERCHANT_ID: str = Field(default="")
    MONCASH_API_KEY: str = Field(default="")
    MONCASH_API_SECRET: str = Field(default="")
    MONCASH_WEBHOOK_SECRET: str = Field(default="")
    
    # NatCash (NatCom Haïti)
    NATCASH_ENABLED: bool = Field(default=False)
    NATCASH_API_URL: str = Field(default="https://api.natcash.natcom.ht/v1")
    NATCASH_MERCHANT_ID: str = Field(default="")
    NATCASH_API_KEY: str = Field(default="")
    NATCASH_WEBHOOK_SECRET: str = Field(default="")
    
    # SMS (Twilio ou autre)
    SMS_ENABLED: bool = Field(default=False)
    SMS_PROVIDER: str = Field(default="twilio")
    TWILIO_ACCOUNT_SID: str = Field(default="")
    TWILIO_AUTH_TOKEN: str = Field(default="")
    TWILIO_PHONE_NUMBER: str = Field(default="")
    
    # Email
    EMAIL_ENABLED: bool = Field(default=False)
    SMTP_HOST: str = Field(default="smtp.gmail.com")
    SMTP_PORT: int = Field(default=587)
    SMTP_USER: str = Field(default="")
    SMTP_PASSWORD: str = Field(default="")
    SMTP_FROM: str = Field(default="noreply@parierkeno.ht")
    
    # Limites jeu
    KENO_MIN_BET: int = Field(default=10)
    KENO_MAX_BET: int = Field(default=100000)
    KENO_MIN_PICKS: int = Field(default=1)
    KENO_MAX_PICKS: int = Field(default=10)
    KENO_DRAW_INTERVAL_MINUTES: int = Field(default=5)
    
    LUCKY_WHEEL_MIN_BET: int = Field(default=10)
    LUCKY_WHEEL_MAX_BET: int = Field(default=10000)
    
    # Limites joueur
    MAX_DAILY_DEPOSIT: int = Field(default=500000)
    MAX_DAILY_LOSS: int = Field(default=100000)
    MAX_SINGLE_BET: int = Field(default=100000)
    
    # Rate limiting
    RATE_LIMIT_REQUESTS: int = Field(default=100)
    RATE_LIMIT_PERIOD_SECONDS: int = Field(default=60)
    
    # Logging
    LOG_LEVEL: str = Field(default="INFO")
    LOG_FILE: str = Field(default="logs/app.log")
    
    # LEH (Loterie de l'État Haïtien)
    LEH_API_URL: Optional[str] = Field(default=None)
    LEH_API_KEY: Optional[str] = Field(default=None)
    LEH_ENABLED: bool = Field(default=False)
    
    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()