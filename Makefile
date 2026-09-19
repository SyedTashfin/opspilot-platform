.PHONY: install lint format typecheck test check up down migrate run

install:
	uv sync

lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy src

test:
	uv run pytest

check: lint typecheck test

up:
	docker compose up -d postgres

down:
	docker compose down

migrate:
	uv run alembic upgrade head

run:
	uv run uvicorn opspilot.api.main:app --reload --port 8000
