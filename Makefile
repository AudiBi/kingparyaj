# Makefile

.PHONY: help install migrate test run docker-up docker-down clean

help:
	@echo "Commandes disponibles:"
	@echo "  make install     - Installer les dépendances"
	@echo "  make migrate     - Appliquer les migrations"
	@echo "  make test        - Lancer les tests"
	@echo "  make run         - Lancer le serveur"
	@echo "  make docker-up   - Démarrer Docker"
	@echo "  make docker-down - Arrêter Docker"
	@echo "  make seed        - Peupler la base"
	@echo "  make clean       - Nettoyer"

install:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

migrate:
	alembic upgrade head

migration:
	alembic revision --autogenerate -m "$(msg)"

test:
	pytest tests/ -v --cov=app --cov-report=html

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

docker-up:
	docker-compose up -d

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

seed:
	python scripts/seed_database.py

create-agent:
	python scripts/create_agent.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov .coverage .mypy_cache

lint:
	black app/
	isort app/
	flake8 app/
	mypy app/