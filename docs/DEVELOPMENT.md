# Development

## Prerequisites

- Python 3.12 (`uv python install 3.12`)
- [uv](https://docs.astral.sh/uv/) for dependencies and virtualenvs
- Docker (for PostgreSQL + pgvector). Docker Desktop must be running for the database services.
- Node 20+ and pnpm for `apps/web` (from M11).

## First run

```bash
uv sync                       # creates .venv, installs runtime + dev dependencies
cp .env.example .env          # fill in provider keys when you reach M2
docker compose up -d postgres # PostgreSQL 16 + pgvector
uv run alembic upgrade head   # apply migrations
uv run uvicorn opspilot.api.main:app --reload --port 8000
```

`GET /healthz` is a liveness probe and never touches the database; `GET /readyz` checks it.
OpenAPI is served at `/docs` and `/openapi.json`.

## Everyday commands

```bash
make check     # ruff format --check, ruff check, mypy src, pytest  (what CI runs)
make format    # rewrite formatting and fix safe lint findings
make test      # tests only
make up / down # postgres only
make migrate   # alembic upgrade head
```

## Test strategy

| Layer | What it covers | Requires |
| --- | --- | --- |
| Unit | configuration guards, schema metadata and DDL compilation, domain rules | nothing |
| API | routes through `httpx.ASGITransport`, no live server | nothing |
| Integration (`-m integration`) | real PostgreSQL: schema create/drop, run lifecycle | `OPSPILOT_TEST_DATABASE_URL` pointing at a database whose name contains `test` |

Two rules that keep the suite honest and cheap:

1. **Unit and API tests never call a model and never open a socket.** Deterministic fakes only, so CI
   spends no tokens and cannot flake on a provider.
2. **Destructive integration tests refuse to run against a database whose name does not contain
   `test`.** The guard exists because the suite drops the schema.

Live-model tests (M2 onward) are a separate, opt-in marker and print their estimated cost before
running.

```bash
# integration, once Docker is up
docker compose exec postgres createdb -U opspilot opspilot_test
OPSPILOT_TEST_DATABASE_URL=postgresql+asyncpg://opspilot:opspilot@localhost:5432/opspilot_test \
  uv run pytest -m integration
```

## Migrations

Schema changes go through Alembic, never ad-hoc DDL:

```bash
uv run alembic revision --autogenerate -m "add evaluations"   # with a live database
uv run alembic upgrade head --sql > /tmp/migration.sql        # review the SQL without applying
```

Migrations are self-contained: they do not import application enums, so a later refactor cannot
rewrite history.

## Configuration

All settings use the `OPSPILOT_` prefix and are validated at startup (`src/opspilot/config.py`).
Budget and limit fields are bounded — `run_cost_cap_eur` may not exceed `daily_cost_cap_eur`, and run
limits cannot be set to values that would allow an unbounded loop. Invalid configuration fails fast
rather than at the first expensive call.

## Environment notes

- On the author's Mac the IPv6 route to `login.microsoftonline.com` is dead, which makes the Azure CLI
  hang silently; it is run with an IPv4-forcing wrapper (see the `azure-cli-multitenant-access` skill).
  Nothing in this repository depends on that workaround.
