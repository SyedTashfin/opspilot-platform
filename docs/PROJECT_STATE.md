# Project state

Last updated: 2026-09-19 (end of M7).

## Current milestone

**M0–M7 complete**: research and architecture, local skeleton, model gateway, tool registry and audit,
agent runtime and OpsPilot pipeline, observability and platform metrics, the Incident Lab, and the
evaluation suite.

Verification, all run locally on 2026-09-19 against the real stack:

| Check | Command | Result |
| --- | --- | --- |
| Formatting | `uv run ruff format --check src tests migrations` | 106 files already formatted |
| Lint | `uv run ruff check src tests migrations` | all checks passed |
| Types | `uv run mypy src` | no issues in 71 source files |
| Tests | `uv run pytest` | **228 passed, 1 deselected** (live marker), 12 of them against live PostgreSQL, in 16s |
| Evaluation suite | `uv run python -m opspilot.evals run --provider reference` | **structural gate PASS**, 4/4 cases, 0.00 EUR, ~2s; report written to JSON |
| Negative control | `... --provider fake` | the suite **fails**: a meaningless answer is caught by the citation grade |
| Key preflight | `... --provider configured` | exits 3 naming `DEEPSEEK_API_KEY`; no provider key is configured on this machine |
| Retrieval benchmark | part of the suite | hit@1 0.14, hit@3 0.71 over 7 paraphrased queries (see below) |
| Compose | `docker compose config --quiet` | valid, including the lab service |
| Migration (live) | `uv run alembic upgrade head` | `0003_spans` applied to PostgreSQL 16; `spans` table confirmed |
| Trace read-back | `tests/integration/test_spans_and_metrics.py` | spans round-trip through PostgreSQL; the overview aggregates rows the test inserted and can count by hand |
| Demo run | `uv run pytest tests/agents -q` | 11-step investigation on the deterministic provider, reproducible |
| Live model call | `uv run pytest -m live -s` | **not run** — no provider key configured yet |

The `SAWarning` about a connection collected by the garbage collector is gone: it was pointing at a real
bug in a test that read through a session after its context had closed. The integration fixtures now set
`lock_timeout` before dropping the schema, so a test that leaves a transaction open fails in five seconds
instead of blocking the suite.


## Architecture (settled)

Python/FastAPI + Pydantic v2 + SQLAlchemy/Alembic API (`apps/api`), Next.js UI (`apps/web`),
PydanticAI for typed agent/tool abstraction with our own Postgres-backed run state machine,
LiteLLM in-process behind a `gateway` facade, runbook retrieval from a lexical index (ADR-017),
OpenTelemetry GenAI
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


### M4 — agent runtime and the OpsPilot pipeline (complete)

- `opspilot.agent` holds the runtime: `RunLimits` (steps, wall clock, cost — all mandatory), a `RunStore`
  protocol with an in-memory double and a PostgreSQL implementation, and `AgentRuntime`, which records
  every step before and after execution and enforces the ceilings around each one.
- **Resumable by construction**: steps already recorded as succeeded or skipped are not re-executed, so a
  run that suspended for approval continues where it stopped. Verified both in memory and across
  database sessions (`tests/integration/test_run_store.py`).
- **Failure containment**: a step that raises is recorded as failed with the exception type and ends the
  run; the runtime never retries and never hides a failure. Hitting a budget ends the run as
  `budget_exceeded` or `timeout` with the reason recorded.
- `opspilot.telemetry` defines a `TelemetrySource` protocol and one implementation — a deterministic
  synthetic source driven by a scenario. Metrics, logs, deployments and resource state are labelled
  `source: demo` everywhere they are returned, so no artefact can be mistaken for live telemetry.
- `opspilot.retrieval` indexes runbooks into heading-delimited chunks with a stable citation id and a
  content hash, and retrieves with BM25 (ADR-017).
- Five shipped runbooks with real operational content (database connectivity, dependency timeouts,
  deployment rollback, HTTP 5xx, high CPU), each with symptoms, diagnosis steps, remediation,
  verification and escalation.
- `opspilot.tools` gained 4 read-only telemetry tools, 1 retrieval tool and 2 `WRITE_RESTRICTED`
  actions, all built by factories over an injected source, so the tools do not know where data comes
  from and M6 can swap the source without touching a step.
- **The OpsPilot pipeline** is 11 recorded steps: classify → collect metrics → collect logs → collect
  deployments → collect resource state → retrieve runbook → select chunks → assemble evidence →
  diagnose → write report → propose remediation. The investigation report is written *before* any
  action is proposed, because it is what a human reviews at the approval gate (ADR-018).
- **Grounding is checked deterministically**: every evidence id the model cites is verified against the
  evidence it was actually shown. Unverifiable ids are recorded as `unsupported_evidence_ids` and the
  `grounding_ratio` is stored alongside the confidence — nothing is silently dropped and nothing is
  silently rewritten. The pipeline test asserts exactly this, using the fake provider, whose output
  deliberately cites a non-existent id.
- **The approval gate is exercised end to end**: the remediation step calls a restricted tool without an
  approval, the executor refuses, the refusal is audited, the run suspends as `waiting_approval`, and
  the evidence log shows that nothing ran.
- `model_calls.step` (migration `0002`) attributes each model call to the pipeline step that issued it.
  Before this, a run's cost was a total with no attribution — the recorder carried the step name and
  dropped it on the way to storage.
- Tests: 51 new (runtime budgets and containment, telemetry determinism and ground-truth non-leakage,
  retrieval ranking and citation stability, tool governance, and the pipeline end to end).

### M5 — observability and platform metrics (complete)

- `opspilot.observability.spans` holds span *data* and the in-process buffer, with no OpenTelemetry
  import: the run store can persist a span and a test can build one without a tracing stack.
- `opspilot.observability.tracing` is the OTel adapter: one tracer provider per process, always feeding
  the buffer, plus an OTLP export when `OPSPILOT_OTEL_EXPORTER_OTLP_ENDPOINT` is set. With no provider
  configured the OTel API returns non-recording spans, so instrumented code runs unchanged in CI.
- **Instrumented at three levels, with parent/child structure asserted in tests**: `agent.run` wraps the
  whole run, `agent.step.<name>` wraps each step, and `gateway.complete` and `tool.execute` nest under
  the step that made the call. A run that ends for any reason other than success is an error span; a run
  suspended for approval is not, because waiting for a human is the system working.
- A span carries facts: step, model, provider, tokens, cost *and whether the cost is known*, latency,
  attempts, fallback flag, tool name, permission class, risk level, outcome status and argument hash. It
  never carries prompts or completions — a test passes a sentinel prompt through the gateway and asserts
  it appears in no span, and another asserts every attribute stays a JSON scalar.
- `spans` table plus migration `0003_spans`, indexed on `trace_id` and `run_id`, cascading from `runs`.
  Spans are drained for the run's own trace when the run ends and written in the run's session (ADR-020).
- `opspilot.observability.metrics` is a pure function from database facts to the dashboard payload, and
  **every figure carries `source: measured` and a `basis` string naming the query** (ADR-012). Success
  rate counts only finished runs; percentiles are nearest-rank so a p95 is always a value that was
  actually observed; an empty input is 0, never undefined. `data_sources` states where the telemetry
  behind the runs came from (`demo` today), so no number can pass for live infrastructure data.
- `POST`-free read endpoints: `/api/v1/platform/overview` and `/api/v1/runs/{id}/trace`. Dependencies are
  FastAPI dependencies, so the contract is tested without a database and the SQL is tested against a real
  one — where the aggregate test inserts known rows and counts them by hand.
- Phoenix is available behind `docker compose --profile observability up -d phoenix` and is never a
  dependency: spans are readable from PostgreSQL without it (ADR-006).
- Tests: 27 new (metric aggregation and provenance, span structure and non-leakage, the two endpoints),
  plus 3 integration tests against live PostgreSQL.

### M6 — the Incident Lab (complete)

- `opspilot.lab` is a real service, not a fixture: `/work` really slows down or fails according to the
  injected fault, and it records what it actually served. The telemetry an investigation reads is
  computed from those observations.
- **Two surfaces, deliberately separated.** The read surface (`/metrics`, `/logs`, `/deployments`,
  `/state`) is what the agent's tools consume. The admin surface (`/admin/faults`, `/admin/ground-truth`,
  `/admin/restart`) requires a token, and **no agent tool points at it** — a test walks the tool registry
  and asserts no governance payload mentions `/admin` or the ground truth (ADR-021).
- **Ground truth is withheld at the source.** A scenario carries the fault *and* the answer key: root
  cause, acceptable diagnoses, expected tools, forbidden actions. The scenario's `alert()` exposes only
  the symptom, and an injection is not written to the service log stream — a production service does not
  print "a fault was injected", and an agent reading logs must not be handed the answer.
- Three scenarios shipped: a deployment that causes retry amplification against a slow dependency, a
  dependency outage producing fast 503s, and CPU saturation. Each lists what a correct investigation must
  not do (for example: restart the service without evidence).
- `HttpTelemetrySource` implements the same `TelemetrySource` protocol as the synthetic source, over
  HTTP, reading only the four read paths. **The agent's tools did not change** — the claim M4 made when it
  built them as factories over an injected source.
- **Unavailable telemetry raises** (`TelemetryUnavailableError`) instead of returning an empty result:
  "no data" and "we could not see the service" are different findings, and only one belongs in a
  diagnosis. A response describing a different service is refused too, so two services' telemetry can
  never be mixed silently.
- Two honesty notes in the code: a healthy backlog is seeded at startup so a fresh container has a window
  to investigate, and CPU/memory/dependency series are derived from the active fault because a synthetic
  service has no CPU to measure. Each series carries a `basis` field saying which it is.
- The end-to-end test injects a fault, drives real traffic, runs the full pipeline against the lab over
  HTTP, and asserts that the evidence contains the deployment the lab recorded and the timeout the
  service logged, that the remediation stopped at the approval gate with nothing executed, and that the
  withheld answer key was never needed to get there.
- Tests: 16 new in `tests/lab`, plus the adapter suite in `tests/telemetry/test_http_source.py`.

### M7 — the evaluation suite (complete)

- `opspilot.evals` runs four cases through the real pipeline against the real lab service over HTTP:
  latency after a deployment changed dependency timeouts, an error spike from a dependency outage, CPU
  saturation, and an adversarial case. Nothing is mocked except the model.
- **Two families of grade, only one of them gated** (ADR-022). Structural: run completed, budgets
  respected, citations grounded, no unapproved action, forbidden actions avoided, no injection compliance.
  Semantic: expected tools used, root-cause match. Structural grades gate CI; semantic grades are reported
  with their method, because gating on a placeholder model's opinion would be theatre.
- **The root-cause grader is keyword-based and says so in its own output.** It is not an LLM judge and its
  known weakness (a correct diagnosis phrased unexpectedly scores low) is documented rather than
  discovered later.
- **A negative control exists and is documented**: with the gateway's `fake` provider — which cites an id
  that does not exist — the suite *fails* the citation and completion grades. A suite in which a
  meaningless answer passes is not measuring anything.
- `reference` answers from the case's own answer key, so its semantic scores are the harness's **ceiling,
  not a model's ability**, and the CLI prints that sentence after every reference run.
- **Injection defence is now measured, not asserted.** `guardrails.py` scans retrieved evidence for
  instruction-shaped content (override, spoofed role, concealment, auto-approval, destructive command,
  urgent restricted action), records matches in the report as `injection_flags`, and the adversarial case
  asserts the flag is raised *and* that nothing executed. The detector is deliberately narrow, and a test
  asserts it produces zero flags on the five shipped runbooks — a detector that fires on normal content
  would make the gate meaningless.
- **The retrieval question from ADR-017 is now answered with numbers**: hit@1 0.14, hit@3 0.71 over seven
  paraphrased queries. Recall@3 is usable, precision@1 is poor, and two queries with no vocabulary overlap
  retrieve nothing useful. That is the evidence ADR-017 asked for, and it points at ranking (heading
  weighting, separate symptom sections, or a reranker) rather than at embeddings being universally better.
- CI gained an `evaluation` job running the structural gate on every push, uploading the JSON report as a
  build artefact. The lab runs in process, so the job needs no container and no network.
- `docs/EVALUATIONS.md` records the methods, the current numbers, and — explicitly — what is *not*
  measured yet: reasoning quality with a real model, cost per investigation, and remediation verification.
- Tests: 36 new across `tests/evals` (grader arithmetic, including four negative graders, the suite end to
  end, the negative control, the dataset's coherence, and the retrieval benchmark).

## Active work

M8 — security and approvals: the approval workflow end to end (request, single-use token bound to the
arguments, resume the suspended run, and verify the action actually restored the service), the threat
model, permission tests, and the failed-case retention M7 left to it.

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
   ground truth must never leak into prompts, tools or retrieval. M4 keeps the fault and root cause in
   the scenario harness (`for_agent()` exposes the alert only) and a test asserts that no step detail,
   summary or tool output contains either.
6. **Retrieval precision is poor and now measured**: hit@1 0.14, hit@3 0.71 (ADR-017's revisit condition
   is met). The fix is a ranking problem, not proof that embeddings are better; the next change to
   retrieval must move this table (see `docs/EVALUATIONS.md`).
7. **The first live call has now been made, and it immediately found a bug**: the provider served
   `deepseek-flash` while the price table listed the retired `deepseek-chat`, so the call came back
   `cost_known=False`. The table now keys on the served model name and encodes DeepSeek's peak/off-peak
   windows rather than averaging them (ADR-023); the default model is the current documented identifier.
   Cost remains conservative: all input is billed at the cache-miss rate because cache hits are not
   visible to the platform.
8. **The service under investigation is a lab service.** Telemetry now comes from a real process that
   really was slowed down, but it is still our own demo service, and the overview says so
   (`data_sources: ["demo", "postgres"]`). It stops being `demo` when a live source produces it (M9).
9. **Cost attribution is per step but not per tool call** — tool latency is recorded, tool spend is not
   a concept (no tool in M4 costs money).

## Important commands

```bash
# local dev (M1)
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn opspilot.api.main:app --reload --port 8000

# evaluation suite, deterministic provider (no tokens spent)
uv run python -m opspilot.evals run --dataset scenarios/ --provider fake

# trace viewer (optional, never a dependency)
docker compose --profile observability up -d phoenix   # http://localhost:6006

# Azure context (requires the IPv4 wrapper on this machine)
az account show --subscription dc0945fe-6315-46cd-a5f3-fd89a39d66dd
```

## Current deployment state

**Nothing deployed.** No Azure resource exists in either subscription for this project. No secrets are
committed; no live demo URL exists yet and none is claimed.
