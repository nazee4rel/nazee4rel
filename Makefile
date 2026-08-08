.PHONY: help setup up down logs migrate revision test lint fmt seed reset

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

test:  ## Run the backend test suite
	docker compose exec backend pytest -v

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
