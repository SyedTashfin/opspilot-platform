# Project state

Last updated: 2026-09-19 (end of M3).

## Current milestone

**M0 (research and architecture), M1 (local skeleton), M2 (model gateway) and M3 (tool registry,
permissions, audit) complete.**

Verification, all run locally on 2026-09-19 against the real stack:

| Check | Command | Result |
| --- | --- | --- |
| Formatting | `uv run ruff format --check .` | 58 files already formatted |
| Lint | `uv run ruff check .` | all checks passed |
| Types | `uv run mypy src` | no issues in 32 source files |
| Tests | `uv run pytest` | **88 passed, 1 deselected** (live marker), integration test included |
| Migration (live) | `uv run alembic upgrade head` | applied to PostgreSQL 16; tables confirmed with `\dt` |
| Live model call | `uv run pytest -m live -s` | **not run** — no provider key configured yet |

The M1 gap is closed: the schema is applied to a real database and the integration test drives a run
lifecycle (agent → run → model call → step) through it.


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

### M2 — model gateway (complete)

- `opspilot.gateway` is the only path from the platform to a language model; no agent imports a vendor
  SDK. Types, policy, pricing, accounting and the gateway itself are separate modules (ADR-014).
- **Model policy**: steps resolve to an ordered chain (primary + fallbacks) from configuration, so
  moving a step to a cheaper or stronger model is a config change with a measurable effect.
- **Providers**: a `ModelProvider` protocol, a deterministic `FakeProvider` (scriptable failures,
  timeouts, valid structured objects, no network), and a `LiteLLMProvider` importing litellm lazily.
- **Retries and fallback**: bounded exponential backoff with a cap, retries only for retryable failure
  classes, then the next model in the chain; every failure reason is carried into the raised error.
- **Budgets**: run and daily ceilings checked before the call against recorded spend; exceeding either
  raises `BudgetExceeded` without attempting the call.
- **Accounting**: every successful call records provider, model, request id, tokens, latency, attempts,
  fallback flag and computed cost. Unknown pricing yields `cost_known=False` — never a plausible zero.
  A fully failed chain records exactly one `status=error` row (ADR-015).
- **Price table** (`pricing.py`) is dated, in one place, and supports version-suffixed model names.
- Tests: 12 gateway behaviour tests (retry, backoff cap, fallback, non-retryable short-circuit, budget
  refusal, unknown cost, no-provider, structured-output invariant) plus policy, pricing, accounting and
  fake-provider suites — all without a network, a clock dependency or a token spent.
- Opt-in `live` marker: one real call that prints model, tokens, measured cost and latency, skipped
  unless a provider key is present, and excluded from the default run and from CI.

### M3 — tool registry, permission gate and audit trail (complete)

- `opspilot.tools`: a `ToolDefinition` declares name (namespaced), description, input and output
  schemas, permission class, risk level and its own timeout. Authorisation is derived from that
  declaration, never from a convention in the handler.
- `ToolRegistry` refuses duplicate names, refuses agents requesting unregistered tools (no silently
  ignored entries), and exports both LLM-facing tool schemas and a governance snapshot for the Tools
  page — including `requires_approval` and `agent_callable`.
- `ToolExecutor` is the only place a tool runs, in a fixed order: resolve → validate arguments →
  permission gate → run under timeout → validate output → audit. Invalid arguments never reach a
  handler.
- **Refusals are outcomes, not exceptions**: a `ToolOutcome` carrying `WAITING_APPROVAL`, `REJECTED`,
  `TIMEOUT` or `ERROR` is returned and audited, so the runtime can escalate for approval or abandon a
  step deliberately. Only an unregistered tool raises (a wiring bug).
- **Approval binding**: restricted tools need a single-use approval bound to the tool *and* the
  argument hash, so approving a restart of `api` cannot be replayed to restart `database`. `ADMIN`
  tools are refused outright, with or without a token.
- **Audit trail**: append-only, with a SHA-256 hash chain over event content plus the previous hash,
  so an edited or reordered event is detectable (`InMemoryAuditRecorder.verify_chain`). Every
  execution *and* every refusal is recorded with actor, action, subject, run id and argument hash.
- One new status (`waiting_approval`) required **no migration** — the M1 decision to store statuses as
  plain `VARCHAR(32)` paid off immediately.
- Tests: 29 new (registry, executor, audit), covering the whole refusal taxonomy, single-use approval,
  argument-bound approval, admin refusal, timeout containment, output-schema enforcement, chain
  integrity and tamper detection — all without a network or a database.


## Active work

M4 — agent runtime and the OpsPilot pipeline: a persisted run state machine with deterministic safety
limits (max steps, wall clock, cost), step records written as the run progresses so it is resumable and
auditable, the fixed investigation pipeline (classify → metrics → logs → deployments → runbook →
evidence → diagnosis with citations and confidence), and the first end-to-end run against the
deterministic fake provider, with a real provider behind the same interface.

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
