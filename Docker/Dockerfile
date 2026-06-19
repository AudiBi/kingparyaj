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

# Installer Poetry
RUN pip install "poetry==${POETRY_VERSION}"

# Copier les fichiers de dépendances
WORKDIR /app
COPY pyproject.toml poetry.lock* ./

# Installer les dépendances
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root --only main

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
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

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
    CMD curl -f http://localhost:8000/health || exit 1

# Commande de démarrage
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]