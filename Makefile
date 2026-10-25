# Convenience targets for operators with GNU make. Docker Compose is the
# supported deployment path; the PowerShell scripts are equivalent on Windows.
#
# On Windows, pass the venv interpreter explicitly:
#   make test-api PYTHON=.venv/Scripts/python.exe

COMPOSE ?= docker compose
PYTHON ?= python

.PHONY: help up down restart ps logs migrate psql backup test test-api test-web typecheck lint build

help:
	@echo "up        Build and start db, migrations, api, worker, and web"
	@echo "down      Stop all services (volumes are preserved)"
	@echo "migrate   Run alembic upgrade head in a one-off container"
	@echo "psql      Open psql against the database"
	@echo "backup    Create an encrypted backup (prompts for a passphrase)"
	@echo "test      Run backend unit tests and the web typecheck"
	@echo "build     Build the images without starting them"

up:
	$(COMPOSE) up -d --build
	@$(COMPOSE) ps

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart api worker web

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=200 api worker

migrate:
	$(COMPOSE) run --rm migrate

psql:
	$(COMPOSE) exec db psql -U deliverability -d deliverability

backup:
	python scripts/backup.py

test: test-api test-web

test-api:
	cd api && $(PYTHON) -m pytest -q

test-web typecheck:
	cd web && npm run typecheck

lint:
	cd api && $(PYTHON) -m compileall -q app

build:
	$(COMPOSE) build
