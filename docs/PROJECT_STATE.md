# Project state

Last updated: 2026-09-19 (end of M1).

## Current milestone

**M0 (research and architecture) and M1 (local skeleton) complete.**

M1 verification, all run locally on 2026-09-19:

| Check | Command | Result |
| --- | --- | --- |
| Formatting | `uv run ruff format --check .` | 32 files already formatted |
| Lint | `uv run ruff check .` | all checks passed |
| Types | `uv run mypy src` | no issues in 15 source files |
| Tests | `uv run pytest` | 22 passed, 1 skipped (DB integration: no local PostgreSQL) |
| Migration | `uv run alembic upgrade head --sql` | renders 7 tables + 5 indexes + alembic_version, exit 0 |

One step is deliberately **not** yet proven: applying the migration against a live PostgreSQL. Docker
Desktop's daemon is stopped on the author's machine, so the integration test skips rather than passing
falsely. That verification is the first task of M2.


## Architecture (settled)

Python/FastAPI + Pydantic v2 + SQLAlchemy/Alembic API (`apps/api`), Next.js UI (`apps/web`),
PydanticAI for typed agent/tool abstraction with our own Postgres-backed run state machine,
LiteLLM in-process behind a `gateway` facade, pgvector over in-repo runbooks, OpenTelemetry GenAI
spans persisted in Postgres with Phoenix as the viewer, MCP consumed via the official Python SDK,
Postgres as the only stateful dependency, Terraform + GitHub OIDC to Azure Container Apps.
Full rationale and rejected alternatives: `docs/DECISIONS.md`.

## Completed work

- Existing-work audit over the author's repositories and local projects (`docs/research/existing-work-audit.md`).
- Open-source evaluation with measured repository metrics as of 2026-09-19 (`docs/research/oss-evaluation.md`).
- Architecture, data flow and module boundaries (`docs/ARCHITECTURE.md`).
- Thirteen ADRs including four spec corrections (ADR-001, ADR-005, ADR-006, ADR-013).
- Cost model and guardrails (`docs/COSTS.md`).
- Azure target verified: student subscription `dc0945fe-6315-46cd-a5f3-fd89a39d66dd`, tenant DVHE,
  `spendingLimit: On`, empty; `Microsoft.App`, `Microsoft.ContainerRegistry`, `Microsoft.DBforPostgreSQL`
  all `NotRegistered` (registration is part of M9).

### M1 — local skeleton (complete)

- Python package `src/opspilot` with strict typing end to end (`mypy --strict` clean).
- Configuration in `config.py`: `OPSPILOT_`-prefixed settings, bounded run/budget limits, a validator
  that refuses `run_cost_cap_eur > daily_cost_cap_eur`.
- Core schema in `db/models.py`: `agents`, `runs`, `run_steps`, `model_calls`, `tool_calls`,
  `approvals`, `audit_events` — status columns as `VARCHAR(32)` (no native enums, no CHECK, so adding
  a status never needs a lockstep migration), `JSONB` on PostgreSQL via a portable JSON variant, money
  as `NUMERIC(12,6)`, token counts as integers.
- Alembic wired to application settings (URL never stored in `alembic.ini`), self-contained initial
  migration, plus `alembic upgrade head --sql` for reviewing DDL without a database.
- FastAPI app factory with `/healthz` (liveness, never touches the database), `/readyz` (checks it),
  `/version`; OpenAPI generated, not hand-written.
- Structured JSON logging via structlog; startup/shutdown events logged with environment and version.
- Docker: multi-stage `Dockerfile` running as a non-root user, `docker-compose.yml` with PostgreSQL 16
  + pgvector and a healthcheck.
- CI (`.github/workflows/ci.yml`): formatting, lint, types, tests on Python 3.12 — no live models, no
  live database, so it cannot flake on a provider or cost tokens.
- Tests: configuration guards, API behaviour, schema metadata and PostgreSQL DDL compilation, and an
  opt-in integration test that refuses to run against a database whose name lacks `test`.
- `THIRD_PARTY.md`, `docs/DEVELOPMENT.md`, `docs/COSTS.md`, `apps/web/README.md` (M11 placeholder).


## Active work

M2 — model gateway: provider abstraction, model policy, retries and timeouts, per-call token and cost
accounting into `model_calls`, a deterministic fake provider for CI, and the first live-model smoke test
(opt-in, cost printed before it runs). Pending prerequisite: bring PostgreSQL up (Docker Desktop) and
apply the initial migration.

## Outstanding work (planned milestones)

| # | Milestone | Outcome |
| --- | --- | --- |
| M1 | Local skeleton | FastAPI + Postgres + Alembic + docker compose + CI (lint, types, tests) green |
| M2 | Model gateway | provider abstraction, policy, retries, token/cost rows, fake provider for CI |
| M3 | Tool registry | schemas, permission classes, timeouts, audit events, MCP client |
| M4 | OpsPilot pipeline | deterministic investigation with real tool calls, persisted steps, report |
| M5 | Observability | OTel spans on every step, Postgres span store, Phoenix viewer, platform metrics |
| M6 | Incident Lab | demo service + fault injection + scenarios with withheld ground truth |
| M7 | Evaluations | datasets, graders, regression gates in CI, failed-case retention, UI |
| M8 | Security + approvals | approval workflow, injection defences, permission tests, threat model |
| M9 | Azure infrastructure | Terraform dev/prod-demo, provider registration, budget alerts, deploy |
| M10 | CI/CD | OIDC federated deploy, image scan, smoke test, environment rules |
| M11 | Frontend polish | the four recruiter flows from the spec, honest metric labelling |
| M12 | Documentation | SECURITY, THREAT_MODEL folded in, DEPLOYMENT, DEVELOPMENT, EVALUATIONS, INCIDENTS |
| M13 | Kubernetes extension | **dropped** (ADR-013) — already demonstrated in Outsight-MultiTenant-GitOps-Lab |

## Known issues and open risks

1. **Scope.** The specification describes several engineer-months of work. Mitigation: milestone order
   puts the recruiter-visible spine (M1–M7) ahead of infrastructure polish, and K8s is dropped.
2. **Azure provider registration** for a fresh student subscription is untested (Container Apps in
   France Central, Postgres Flexible capacity). Verify in M9 before building Terraform around it.
3. **Institutional tenant policy** blocks the device-code auth flow; CI must use OIDC federated
   credentials, and no interactive admin login can be scripted.
4. **Reliability of the local Azure CLI** on the author's Mac requires forcing IPv4 (dead IPv6 route to
   `login.microsoftonline.com`); documented in the `azure-cli-multitenant-access` skill.
5. **Evaluation credibility** depends on the incident scenarios being genuinely unknown to the agent;
   ground truth must never leak into prompts, tools or retrieval.

## Important commands

```bash
# local dev (M1)
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn opspilot.api.main:app --reload --port 8000

# evaluation suite, deterministic provider (no tokens spent)
uv run python -m opspilot.evals run --dataset scenarios/ --provider fake

# Azure context (requires the IPv4 wrapper on this machine)
az account show --subscription dc0945fe-6315-46cd-a5f3-fd89a39d66dd
```

## Current deployment state

**Nothing deployed.** No Azure resource exists in either subscription for this project. No secrets are
committed; no live demo URL exists yet and none is claimed.
