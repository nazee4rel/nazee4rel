.PHONY: help setup up down logs migrate revision test test-pg lint fmt seed reset prod-up prod-down backup restore audit

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:  ## Create .env from the template and generate real secrets
	@test -f .env && echo ".env already exists — not overwriting." && exit 0 || true
	@cp .env.example .env
	@python3 -c "import secrets,base64,os,pathlib; p=pathlib.Path('.env'); s=p.read_text(); \
s=s.replace('CHANGE_ME_generate_with_token_urlsafe_64', secrets.token_urlsafe(64)); \
s=s.replace('CHANGE_ME_generate_with_32_random_bytes_b64url', base64.urlsafe_b64encode(os.urandom(32)).decode()); \
s=s.replace('CHANGE_ME_local_dev_password', secrets.token_urlsafe(24)); \
p.write_text(s)"
	@echo ".env created with generated secrets."

up:  ## Start the whole stack (runs migrations automatically)
	docker compose up --build -d
	@echo "Backend  http://localhost:8000/docs"
	@echo "Frontend http://localhost:3000"

down:  ## Stop the stack
	docker compose down

logs:  ## Tail backend logs
	docker compose logs -f backend

worker-logs:  ## Tail collection worker and scheduler logs
	docker compose logs -f worker beat

collect:  ## Run one collection cycle immediately (does not wait for beat)
	docker compose exec worker celery -A app.worker.celery_app call collect.post_metrics

migrate:  ## Apply migrations
	docker compose exec backend alembic upgrade head

revision:  ## Autogenerate a migration:  make revision m="add posts"
	docker compose exec backend alembic revision --autogenerate -m "$(m)"

test:  ## Run the backend test suite (SQLite; no setup required)
	docker compose exec backend pytest -q

test-pg:  ## Also run the PostgreSQL-only tests against the dev database
	docker compose exec -e TEST_POSTGRES_URL=postgresql+asyncpg://$${POSTGRES_USER}:$${POSTGRES_PASSWORD}@postgres:5432/$${POSTGRES_DB}_test backend sh -c "\
		psql -h postgres -U $${POSTGRES_USER} -c 'CREATE DATABASE $${POSTGRES_DB}_test' 2>/dev/null; pytest -q"

audit:  ## Check dependencies for known vulnerabilities
	docker compose exec backend sh -c "pip install -q pip-audit && pip-audit"
	cd frontend && npm audit --audit-level=moderate

lint:  ## Lint and type-check
	docker compose exec backend ruff check .
	docker compose exec backend mypy app

fmt:  ## Format
	docker compose exec backend ruff format .

seed:  ## Create the owner account interactively
	docker compose exec backend python -m app.cli create-owner

reset:  ## Destroy all data and rebuild from scratch
	docker compose down -v
	docker compose up --build -d

# --- production ---------------------------------------------------------------

PROD := docker compose -f docker-compose.prod.yml

prod-up:  ## Build and start the production stack (migrations run first)
	$(PROD) up -d --build
	@echo "Frontend on 127.0.0.1:3000 — put a TLS-terminating proxy in front of it."

prod-down:  ## Stop the production stack, keeping volumes
	$(PROD) down

prod-logs:  ## Tail production logs
	$(PROD) logs -f backend worker beat

backup:  ## Dump the database to ./backups (the only copy of unrecoverable data)
	@mkdir -p backups
	@set -a; . ./.env; set +a; \
	 $(PROD) exec -T postgres pg_dump -U $$POSTGRES_USER -d $$POSTGRES_DB \
	   | gzip > backups/xagent-$$(date +%Y%m%d-%H%M%S).sql.gz
	@ls -lh backups | tail -1
	@echo "Impressions past 30 days and the follower series exist here and nowhere else."

restore:  ## Restore a dump:  make restore FILE=backups/xagent-....sql.gz
	@test -n "$(FILE)" || (echo "usage: make restore FILE=backups/xagent-....sql.gz" && exit 1)
	@set -a; . ./.env; set +a; \
	 gunzip -c $(FILE) | $(PROD) exec -T postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB
	@echo "Restored $(FILE)."
