# Existing work audit

Scope: what the author already owns that this project should reuse, and what must be built new.
Method: GitHub API metadata + local tree/README inspection. Measured 2026-09-19.

## Direct predecessor

`SyedTashfin/OpsPilot` (MIT, TypeScript, last push 2026-09-10, local `~/Projects/opspilot-work`)

pnpm/Turbo monorepo: `apps/`, `packages/{domain,contracts,database,llm,rag,telemetry}`,
`infra/{docker,compose}`, `docs/{adr,architecture,release}`, `tests/{integration,fixtures}`,
`.github/workflows`.

v1 README states its own scope and explicitly puts **out of V1 scope**: evaluation, prompt
management, Kubernetes, Terraform, cloud deployment, multi-tenancy, RBAC, automatic
remediation, multi-agent planning, enterprise integrations.

That list is almost exactly what `OpsPilot Platform` must add. The platform is therefore a
**v2 in a new stack**, not a greenfield rewrite, and v1 remains valuable as:

| Reuse | Asset | How |
| --- | --- | --- |
| Reuse as-is | `runbooks/` content (database-connectivity, high-cpu, http-5xx, deployment-rollback, dependency-timeout class) | copy content into `runbooks/`, keep front-matter schema |
| Reuse as-is | incident scenario definitions + seeded demo telemetry generator concepts | port into `scenarios/` as declarative YAML |
| Reuse with edits | evidence / root-cause report contract (typed, versioned) | port to Pydantic models; add `confidence`, `citations`, `unsupported_claims` |
| Reuse as pattern | deterministic orchestration *before* the model call; one structured generation at the end | becomes the spine of the OpsPilot pipeline |
| Reuse as pattern | typed provider abstraction + fake LLM for tests | becomes `model_gateway` + deterministic test provider |
| Reuse as pattern | dark, dense dashboard UX (incidents, evidence, tool timeline, trace links) | informs the Next.js dashboard |
| Reuse as pattern | ADR discipline, release notes, CI workflow shape | carried forward |
| Do **not** reuse | the TypeScript/Fastify codebase itself | v2 backend is Python/FastAPI per spec; mixing runtimes in one repo is friction for no signal |
| Do **not** reuse | single-call root-cause generation | v2 needs multi-step tool-using investigation with approval gates |

## Other owned repositories worth reusing

| Repo | Lang | Last push | Reuse verdict |
| --- | --- | --- | --- |
| `Local-Multi-LLM-Orchestrator` (MIT) | TS | 2026-09-10 | Provider health / fallback / ranking patterns for the model gateway. Patterns only, reimplemented in Python. |
| `Outsight-MultiTenant-GitOps-Lab` | Shell | 2026-09-10 | The Kubernetes/K8s signal already exists here (Argo Rollouts, Prometheus gates, Helm). Do **not** duplicate it inside OpsPilot Platform; link it instead. Its Terraform/pytest/Makefile shapes are reusable for IaC conventions. |
| `mcp-grafana` | Go | 2026-09-14 | A fork of the upstream Grafana MCP server. Evidence of real MCP exposure; the sensible MCP integration here is *consuming* an MCP server (Grafana or Azure), not writing one. |
| `Thales-optronic-video-indexing` | Python | 2026-09-02 | FastAPI service structure, Celery/Redis worker conventions — reused only as style reference, since v2 deliberately avoids Celery/Redis. |
| `ISO-27001-Web-App` | TS | 2026-07-27 | Control-evidence vocabulary for `SECURITY.md` / threat model. |
| `Portfolio` | TS | 2026-09-18 | Where the live demo link belongs; existing design tokens and typography. |
| `~/Projects/lc-observability`, `wazuh-lab`, `lc-next-security` | mixed | local | Observability + incident-response vocabulary and dashboard patterns. |
| `opentelemetry-browser`, `opentelemetry-js-contrib` | JS | 2026-09-17 | Author already contributes to OTel; the platform should emit **standard OTel GenAI semantic conventions** rather than a bespoke trace format. |

## Gaps that must be built (no owned equivalent)

1. Tool registry with schemas, permission levels, timeouts, risk classes, approval requirement.
2. Human-approval workflow with persisted, expiring approval records and an append-only audit trail.
3. Evaluation harness: datasets, graders, regression gates, failed-case retention.
4. Incident Lab with application-level fault injection and withheld ground truth.
5. Model gateway with token/cost accounting persisted per call.
6. Azure deployment: Terraform, OIDC CI/CD, managed identity, Key Vault.
7. Platform API + dashboard for agents, runs, evals, tools, infrastructure, observability.

## Environment facts that shape the build

- Azure for Students subscription (`dc0945fe-…`, tenant DVHE) is the deployment target: `spendingLimit: On`, credit visible in the Education blade, currently **empty** (no resource groups) and **no providers registered yet** (`Microsoft.App`, `Microsoft.ContainerRegistry`, `Microsoft.DBforPostgreSQL` all `NotRegistered`).
- The institutional tenant blocks the **device-code** authentication flow for the CLI (Conditional Access) while allowing interactive browser login — relevant to CI/CD design, which must use OIDC federated credentials rather than user login.
- Author's personal free-trial subscription must **not** be used for this build.
