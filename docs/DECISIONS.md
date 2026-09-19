# Decisions

Short ADRs. One decision per entry, with the alternatives that were rejected and why.
Do not reopen a settled decision without new evidence.

## ADR-001 — Repository layout: one Python package, one web app

Status: accepted 2026-09-19.

`apps/api` is a single Python package (`src/opspilot/…`) with internal module boundaries
(`agent`, `gateway`, `tools`, `evals`, `observability`, `api`, `db`), plus `apps/web` (Next.js).
The spec's pnpm-style `packages/*` split is TypeScript-idiomatic; in Python it becomes packaging
friction with no enforcement benefit. Alternatives rejected: polyglot monorepo with TS packages
(split runtime for no signal), separate repositories (loses atomic changes across API and web).

## ADR-002 — Agent runtime: PydanticAI, plus our own persisted step machine

Status: accepted 2026-09-19.

The OpsPilot pipeline is *deterministic*: classify → metrics → logs → deployments → runbook →
evidence → diagnosis → approval → action → verify. A graph framework is not needed to express a
fixed pipeline with typed steps; a framework's value appears when the control flow itself is
model-chosen. We use PydanticAI for typed agent/tool abstraction and structured outputs, and keep
run state, step transitions, budgets, retries and resumption in our own Postgres-backed state
machine — which is also the thing the audit trail and the evaluation harness need.

Alternatives rejected: **LangGraph** (41,952 stars, MIT) — strong checkpointing/interrupt story, but
for a fixed pipeline it adds a heavyweight dependency and hides the control flow that this project
exists to demonstrate; revisit if durable human-in-the-loop interrupts across long-running
asynchronous runs become the primary requirement. **OpenAI Agents SDK** — narrower provider story,
weaker typing. **Hand-rolled tool loop only** — loses structured outputs and retry semantics that
are otherwise free.

## ADR-003 — Model gateway: LiteLLM as an in-process library behind our own facade

Status: accepted 2026-09-19.

All model traffic goes through `opspilot.gateway`. Inside, LiteLLM (59,159 stars) provides provider
abstraction, retries, timeouts and cost tables; our facade owns model policy, per-step model
selection, fallback order, budget caps, request ids, latency and the `model_calls` accounting rows.
Alternatives rejected: running LiteLLM as a standalone proxy service (one more deployable and a network
hop for a single-tenant demo); hand-written provider clients (commodity work, and per-vendor cost
tables are a tar pit). Note: LiteLLM core is MIT but the repository carries separately licensed
enterprise files — stay in the open-source surface, record it in `THIRD_PARTY.md`.

## ADR-004 — Backend stack: FastAPI + Pydantic v2 + SQLAlchemy 2 + Alembic

Status: accepted 2026-09-19. Strict typing end to end; OpenAPI generated from the app (no hand-written
`API.md`). Migrations mandatory; no ad-hoc DDL. Rejected: Django (admin/ORM weight irrelevant here),
Node/Fastify (v2 is the Python AI-engineering signal; v1 already proves the TS stack).

## ADR-005 — Postgres is the only stateful dependency

Status: accepted 2026-09-19.

Postgres holds domain data, run state, audit events, spans, evaluations and the job queue
(`SELECT … FOR UPDATE SKIP LOCKED` worker loop, or a Container Apps Job). Rejected: Redis/Celery
(a broker to operate for a job volume of a few per minute), Temporal (excellent, but another cluster),
Service Bus (same reasoning, cloud-side).

## ADR-006 — Observability: OpenTelemetry as the wire format, our own span persistence, Phoenix as viewer

Status: accepted 2026-09-19.

Every run emits OTel spans using the GenAI semantic conventions (agent invocation, model call,
retrieval, tool call, approval, error, retry). Spans are persisted in Postgres so the platform's own
metrics (p95 latency, cost, failure rate) never depend on a third-party service; the OTLP endpoint is
configuration, so Langfuse Cloud or any other collector can be attached without code changes. A
self-hosted Phoenix container provides the trace UI.

Alternatives rejected: **self-hosted Langfuse** — needs ClickHouse + Redis + web, i.e. real money and
real operational surface on a student budget for a viewer; **building a trace UI** — negative signal;
**Grafana as the embedded UI** — AGPL and unnecessary for the product's own dashboards.

## ADR-007 — Retrieval: pgvector over in-repo runbooks

Status: accepted 2026-09-19. The corpus is a few dozen runbooks. A dedicated vector database adds a
service, a backup story and a failure mode for no measurable gain at this size. Rejected: Qdrant,
Chroma, Azure AI Search (the last also costs money and requires an index lifecycle for ~10 documents).
Every retrieved chunk is returned with its source path and content hash so grounding is checkable.

## ADR-008 — Incident Lab: application-level fault injection, ground truth withheld

Status: accepted 2026-09-19.

A `demo-service` container exposes a control API that injects faults in-process (latency, 5xx,
DB connection failure, bad env var, failed deployment marker, CPU burn, memory pressure, auth failure,
dependency-unavailable) and emits Prometheus metrics, structured logs and deployment records. The agent
reads that telemetry through normal read-only tools; the scenario's ground truth lives in the evaluation
dataset and is never passed to the agent.

Rejected: cluster-level chaos (Litmus/Chaos Mesh/netem) — expensive, needs AKS, and would be labelled
"simulated incident" either way; canned log fixtures — the whole point is that the agent reasons over
telemetry it did not author.

## ADR-009 — Azure target: Container Apps, ACR, Postgres Flexible, Key Vault, Managed Identity

Status: accepted 2026-09-19. Scale-to-zero compute; no AKS (cost, and the K8s signal already exists in
`Outsight-MultiTenant-GitOps-Lab`); no Service Bus; no Azure AI Search. Postgres Flexible Server is the
main fixed cost and is stopped between demo sessions. Managed identity for service-to-service auth, Key
Vault for secrets, GitHub OIDC for deploys. Region: France Central (author's residence, lowest latency).

## ADR-010 — Tool permissions: default deny, four classes, expiring approvals, append-only audit

Status: accepted 2026-09-19. `READ_ONLY` executes automatically; `WRITE_SAFE` executes with an audit
record inside an allowlist; `WRITE_RESTRICTED` requires a persisted human approval with a TTL and a
single-use token bound to (run, tool, argument hash); `ADMIN` is not reachable by agents at all. Agents
never receive shell access. The deployed agent's identity is a custom role on one resource group, never
Owner — the strongest claim we can make is that the blast radius is one resource group and one action.

## ADR-011 — MCP: consume, do not implement

Status: accepted 2026-09-19. The tool registry speaks to MCP servers through the official Python SDK.
One useful integration ships (Azure MCP via `microsoft/mcp`, and/or the Grafana MCP server the author
already has a fork of). Writing a protocol implementation to claim protocol support is rejected
explicitly.

## ADR-012 — Metric honesty is a data-model property

Status: accepted 2026-09-19. Every metric surfaced in the UI carries `source: measured | seeded |
simulated`, and evaluation numbers are only rendered from a stored evaluation run. Seeded demo data is
allowed but always labelled, and the dashboard separates "demo data" from "runtime measured this
session". Rejected: silent seeding, and rounding up empty states into plausible-looking numbers.

## ADR-013 — Scope: drop the Kubernetes extension from this project

Status: accepted 2026-09-19. The spec marks AKS/Helm/Argo as optional and secondary; the author already
demonstrates that stack elsewhere. Time saved goes to evaluations, approvals and cost accounting, which
are the differentiators. Revisit only after the deployed product satisfies the definition of done.
