# Architecture

Phase 1 design. Components marked **(M#)** are built in that milestone; everything else is selected
open source.

```mermaid
flowchart TB
  subgraph UI["apps/web — Next.js (M11)"]
    DASH[Dashboard]
    AG[Agents]
    RUN[Runs]
    EVAL[Evaluations]
    TOOLS[Tools]
    INFRA[Infrastructure]
    OBS[Observability]
  end

  subgraph API["apps/api — FastAPI (M1)"]
    REST[REST + OpenAPI]
    APPROVE[Approval service]
    AUDIT[Audit log]
  end

  subgraph RUNTIME["packages: agent runtime (M2–M4)"]
    SM[Run state machine\nbudgets · retries · timeouts · resume]
    OPS[OpsPilot pipeline]
    REG[Tool registry\nschemas · permissions · risk]
  end

  subgraph GW["model gateway (M2)"]
    FACADE[policy · fallback · accounting]
    LL[LiteLLM]
  end

  subgraph TOOLS["tools (M3)"]
    AZ[azure.* read-only]
    GH[github.*]
    SQL[postgres.read_query]
    RAG[docs.search_runbook]
    MCPC[MCP client — Azure / Grafana]
    ACT[restricted actions\napproval required]
  end

  subgraph LAB["Incident Lab (M6)"]
    DEMO[demo-service\nfault injection control API]
  end

  subgraph DATA["PostgreSQL (M1)"]
    PG[(domain · runs · spans · evals\naudit · cost)]
    IDX[(runbook index:\nlexical, cited chunks)]
  end

  subgraph OBSL["Observability (M5)"]
    OTEL[OTel GenAI spans]
    PHX[Phoenix — trace viewer]
  end

  DASH & AG & RUN & EVAL & TOOLS & INFRA & OBS --> REST
  REST --> SM
  SM --> OPS
  OPS --> REG
  OPS --> FACADE
  FACADE --> LL
  REG --> AZ & GH & SQL & RAG & MCPC & ACT
  OPS -.11 recorded steps.-> PIPE[classify → metrics → logs → deployments → resource state →
    runbook → evidence → diagnose → report → remediation]
  AZ -.read-only.-> AZURE[(Azure Monitor / resource state)]
  GH -.read-only.-> GITHUB[(GitHub API)]
  SQL --> PG
  RAG --> VEC
  MCPC --> MCPSRV[(MCP servers)]
  ACT --> DEMO
  DEMO --> OTEL
  SM --> OTEL
  OTEL --> PG
  OTEL -.OTLP endpoint is config.-> PHX
  APPROVE --> PG
  REST --> APPROVE
  REST --> AUDIT
  AUDIT --> PG
  EVAL --> SM
```

## Data flow for the flagship run

1. Incident Lab injects a fault in `demo-service`; the service emits Prometheus metrics, structured
   logs and a deployment record.
2. A run is created for the OpsPilot agent with an explicit budget (max steps, max wall time, max cost).
3. The runtime executes a fixed pipeline: classify → metrics → logs → deployments → config → runbook
   retrieval → evidence assembly → diagnosis + confidence → remediation proposal.
4. Tools execute through the registry, which enforces permission class, timeout and audit. Read-only
   tools run unattended; a restricted action halts the run and creates an approval record.
5. Every step emits OTel spans, persisted to Postgres and viewable in Phoenix.
6. The run ends with a report: evidence, citations to runbook chunks, diagnosis, confidence, proposed
   remediation, cost, latency, and the audit trail.

## Boundaries that must not blur

- **Transport** (FastAPI routers) contains no agent or domain logic.
- **Agent logic** never talks to a cloud SDK directly; it goes through the tool registry.
- **Tools** are the only place infrastructure integrations live, and each declares its schema,
  permission class and timeout.
- **Retrieved content is untrusted data** — runbook text and log lines can influence a diagnosis but are
  never interpreted as instructions and never become tool arguments without schema validation.
- **Observability is not business logic**: if the trace exporter is down, runs still complete.
