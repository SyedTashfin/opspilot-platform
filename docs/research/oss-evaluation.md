# Open-source evaluation

Measured via GitHub API on 2026-09-19. Stars/last-push/licenses are the raw values returned by
the API at that time; `NOASSERTION` means GitHub could not map the licence file to an SPDX id and
the licence text must be read before redistribution.

## Measured candidates

| Project | Stars | Last push | Licence | Verdict |
| --- | --- | --- | --- | --- |
| langchain-ai/langgraph | 41,952 | 2026-09-18 | MIT | evaluate — see ADR-002 |
| pydantic/pydantic-ai | 20,052 | 2026-09-19 | MIT | **select** (ADR-002) |
| BerriAI/litellm | 59,159 | 2026-09-19 | NOASSERTION (MIT core + licensed EE files) | **select as library** (ADR-003) |
| langfuse/langfuse | 34,815 | 2026-09-19 | NOASSERTION (MIT core + EE dir) | defer — self-hosting needs ClickHouse+Redis (ADR-006) |
| Arize-ai/phoenix | 11,541 | 2026-09-19 | NOASSERTION (ELv2 components) | **select** as self-hosted trace viewer (ADR-006) |
| open-telemetry/opentelemetry-python | 2,637 | 2026-09-18 | Apache-2.0 | **select** (wire format) |
| traceloop/openllmetry | 7,443 | 2026-09-17 | Apache-2.0 | evaluate — useful GenAI span conventions, heavier than hand-instrumenting |
| openai/openai-agents-python | 29,563 | 2026-09-18 | MIT | reject — framework overlap with PydanticAI, weaker typing story |
| modelcontextprotocol/python-sdk | 24,344 | 2026-09-19 | MIT | **select** (MCP client) |
| modelcontextprotocol/servers | 90,472 | 2026-09-03 | NOASSERTION (per-server licences) | select one server only |
| microsoft/mcp (Azure MCP) | 3,688 | 2026-09-18 | MIT | **select** — the "one useful MCP integration" (ADR-011) |
| temporalio/temporal | 23,177 | 2026-09-19 | MIT | reject — a second stateful service to operate for a workflow we can persist in Postgres |
| celery/celery | 28,902 | 2026-09-19 | NOASSERTION | reject — needs a broker; Postgres job table is sufficient |
| pgvector/pgvector | 23,087 | 2026-09-10 | PostgreSQL licence | **select** (ADR-007) |
| qdrant/qdrant | 34,688 | 2026-09-19 | Apache-2.0 | reject for now — a second datastore for a corpus of ~10 runbooks |
| chroma-core/chroma | 29,332 | 2026-09-18 | Apache-2.0 | reject — same reason |
| vercel/next.js | 142,366 | 2026-09-19 | MIT | **select** (frontend) |
| grafana/grafana | 76,809 | 2026-09-19 | AGPL-3.0 | reject as embedded component — AGPL; keep dashboards to standard metrics |
| prometheus/prometheus | 66,130 | 2026-09-19 | Apache-2.0 | defer to the optional infra milestone |
| hashicorp/terraform | 49,690 | 2026-09-18 | NOASSERTION (BUSL-1.1) | **select** (IaC) — BUSL is fine for our own use, note it in THIRD_PARTY.md |

Two API lookups in the original probe returned 404 (`clickhouse/clickhouse-server`, `AzAppService/api`)
because the org/repo paths were wrong; ClickHouse is `ClickHouse/ClickHouse`, and it is only relevant
if Langfuse is self-hosted, which ADR-006 rejects.

## Why not build it ourselves

**Provider clients, retries, fallbacks, token/cost tables** — LiteLLM. Hand-rolling per-vendor clients
is exactly the commodity work a portfolio should not spend its budget on. We keep the *policy* layer
ours: which model for which step, budget caps, what we record.

**Tracing storage and UI** — Phoenix (or Langfuse Cloud). Writing a trace backend demonstrates nothing
and consumes the credit. We keep the *instrumentation* and the *span persistence* ours so the product's
own metrics never depend on a vendor being up.

**MCP protocol** — `modelcontextprotocol/python-sdk`. Implementing a protocol to claim protocol support
is the anti-pattern the spec calls out.

**OAuth/OIDC, Postgres, container runtime, OpenTelemetry protocol** — not ours to build. CI/CD uses
GitHub OIDC federated credentials against Azure; no long-lived cloud secrets.

## What we still build ourselves (the visible engineering)

1. Agent domain model, run lifecycle, persisted step state machine with safety limits.
2. Tool registry + authorisation model + approval workflow + append-only audit semantics.
3. OpsPilot investigation pipeline over real telemetry, with evidence and confidence.
4. Incident Lab scenarios and fault injection, with withheld ground truth.
5. Evaluation datasets, graders, regression gates, failed-case retention.
6. Cost accounting per model call, aggregated per run, surfaced honestly in the UI.
7. Platform API, dashboard, and Azure operational tool integrations.

## Licence hygiene

Every third-party dependency and any consulted implementation goes in `THIRD_PARTY.md` with licence and
usage mode (dependency vs. pattern). No source copying from incompatible licences. AGPL (Grafana) and
ELv2 (Phoenix components) are used as separate services or not at all — never vendored into our code.
