# OpsPilot Platform

**Status: Phase 1 (research and architecture). Nothing is deployed yet; no metric in this repository is
invented. Every number that appears in the product will be measured, seeded-and-labelled, or absent.**

An AI agent platform: deploy, run, evaluate, observe, secure and integrate AI agents. It ships with one
flagship agent — **OpsPilot**, an infrastructure incident investigation agent that works over real
telemetry, deployment history and runbooks, and asks a human before touching anything.

The point of the project is not "an agent". It is the platform engineering required to run agents
safely and credibly: typed model gateway with cost accounting, a tool registry with permission classes
and human approval, an evaluation harness with regression gates, OpenTelemetry traces on every run, and
reproducible Azure infrastructure under Terraform.

## 1. What it does

- **Agent platform** — agents are first-class records (model policy, allowed tools, evaluation suite),
  runnable from the UI, with every run persisted and traceable.
- **OpsPilot** — receives an alert, inspects metrics, logs, recent deployments and configuration,
  retrieves the relevant runbook, assembles evidence, produces a diagnosis with a calibrated confidence
  and a proposed remediation, requests approval for anything restricted, executes it, verifies recovery,
  and writes an incident report.
- **Incident Lab** — controlled faults injected into a demo service (latency, 5xx, DB connectivity,
  bad config, bad deployment, CPU/memory pressure, dependency failure). The evaluator knows the ground
  truth; the agent does not.
- **Evaluations** — repeatable benchmark over incident scenarios: root-cause accuracy, tool-selection
  accuracy, grounding, unsafe-action prevention, remediation correctness, latency, tokens, cost.
  Failed cases are kept as evidence.

## 2. Why it exists

Existing portfolio work proves pieces of this: `SyedTashfin/OpsPilot` (v1) proves deterministic
orchestration, RAG over runbooks and local-first infrastructure; `Outsight-MultiTenant-GitOps-Lab`
proves Kubernetes and GitOps; `Local-Multi-LLM-Orchestrator` proves provider abstraction. None of them
proves the operational layer an agent platform needs — permissions, approvals, evaluation, cost control,
observability and cloud deployment. That gap is this project.

## 3. Live demo

Not yet deployed. Deployment target is the Azure for Students subscription (France Central) via
Terraform + GitHub OIDC. A link goes here when it exists — not before.

## 4. Architecture

See `docs/ARCHITECTURE.md`. Decisions and rejected alternatives: `docs/DECISIONS.md`.

## 5. Flagship incident example

Planned end-to-end story: an API latency incident caused by a bad deployment, investigated through
read-only tools, diagnosed with cited evidence, remediated only after human approval, verified, and
written up. No example numbers are published until they come from a real run.

## 6. Evaluation results

Published from stored evaluation runs only (`docs/EVALUATIONS.md`). Empty until the harness exists.

## 7. Technology

```
uv run python -m opspilot.evals run --provider reference   # the evaluation suite, structural gate
```

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 + Alembic · PostgreSQL + pgvector · PydanticAI ·
LiteLLM · OpenTelemetry · Phoenix · MCP (Python SDK) · Next.js + TypeScript · Docker · Terraform ·
Azure Container Apps · GitHub Actions (OIDC). Rationale for each in `docs/research/oss-evaluation.md`.

## 8. Security

Read-only tools by default; four permission classes; single-use, expiring approvals for restricted
actions; append-only audit events; managed identity instead of cloud credentials; agent identity scoped
to one resource group. Details: `docs/SECURITY.md`.

## 9. Local development

See `docs/DEVELOPMENT.md` (created in M1).

## 10. Deployment

See `docs/DEPLOYMENT.md` (created in M9). Costs and limits: `docs/COSTS.md`.
