# Dockerfile
# Image de production pour FastAPI - Parier Keno & Lucky Haïti

# ==================== ÉTAPE 1 : Build ====================
FROM python:3.12.3-slim AS builder

# Variables d'environnement
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VERSION=1.7.1

# Installer les dépendances système
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Dépendances : requirements.txt (versions figées). Le bloc [tool.poetry.dependencies] de
# pyproject.toml ne liste aucune bibliothèque : `poetry install` produirait une image vide.
WORKDIR /app
COPY requirements.txt ./
RUN pip install --prefix=/install -r requirements.txt

# ==================== ÉTAPE 2 : Production ====================
FROM python:3.12.3-slim AS production

# Variables d'environnement
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Port-au-Prince

# Installer les dépendances runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

# Créer un utilisateur non-root
RUN addgroup --system --gid 1000 appuser \
    && adduser --system --uid 1000 --gid 1000 appuser

# Copier les dépendances depuis le builder
COPY --from=builder /install /usr/local

# Définir le dossier de travail
WORKDIR /app

# Copier le code source
COPY --chown=appuser:appuser . .

# Créer les dossiers nécessaires
RUN mkdir -p logs uploads static/uploads \
    && chown -R appuser:appuser /app

# Passer à l'utilisateur non-root
USER appuser

# Port exposé
EXPOSE 8000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health/live || exit 1

# Commande de démarrage
# PORT est fourni par l'hébergeur (Render) ; WEB_CONCURRENCY règle le nombre de processus
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-4} --proxy-headers --forwarded-allow-ips='*'"]