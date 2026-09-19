# Costs

Target: Azure for Students subscription, France Central, `spendingLimit: On` (hard cap — the
subscription cannot be billed past the credit). Credit is visible in the Azure Education blade;
the figure there is authoritative and is not exposed by the Consumption API for this offer type.

## Estimated monthly idle cost (nothing running)

| Resource | Idle cost | Notes |
| --- | --- | --- |
| Container Apps (API, web, demo-service) | ~€0 | consumption plan, scale-to-zero (`minReplicas: 0`) |
| Container Apps environment | €0 base | Log Analytics ingestion is the only meter attached |
| Azure Container Registry (Basic) | ~€5/mo | fixed; the alternative is GHCR (free) if we accept a GitHub-side dependency |
| PostgreSQL Flexible B1ms | ~€4/mo | compute stopped, storage still billed |
| Key Vault | <€0.10 | secret operations are metered per 10k |
| Log Analytics | €0 | free ingestion allowance covers demo volume |

**Idle: roughly €9–10/month.**

## Estimated demo-day cost

| Item | Cost | Notes |
| --- | --- | --- |
| Postgres compute while started | ~€0.018/hour | stop it again after the session |
| Container Apps compute during a demo | <€0.10 for a few hours of intermittent traffic | only billed while replicas are active |
| Log ingestion | ~€0 | free allowance |
| Model calls | **not** Azure credit | billed by the provider (Mistral / DeepSeek / OpenRouter); tracked per call in the UI |

## Credit maths

€86 (portal figure) at €9–10/month idle with weekly demo sessions lands around **7–8 months**, and the
credit window runs to 2027-09-19. Two levers if it tightens: drop ACR for GHCR (saves ~€5/mo), and stop
Postgres between sessions (saves ~€0.50/day of compute).

## Scale-to-zero truth table

| Service | Scales to zero? |
| --- | --- |
| Azure Container Apps (HTTP) | yes, with `minReplicas: 0` |
| Container Apps Jobs | yes, they only run when triggered |
| ACR | no — always-on registry, billed daily |
| PostgreSQL Flexible Server | no — can be *stopped* (compute), storage always billed |
| Key Vault | effectively no cost at rest |
| Log Analytics | no idle cost, pay per ingested GB |

## Guardrails

1. Terraform defaults to the cheapest tier of each service and refuses `prod-demo` sizing without
   explicit variables.
2. Budget alerts on the subscription (Microsoft.Consumption budgets) at 25/50/75/90 % of a monthly
   ceiling.
3. Model budget per run and per day enforced in the gateway, not by convention: a run refuses to start
   if the daily cap is already spent.
4. Development and CI use the deterministic fake provider and cheap models; a live-model evaluation run
   is opt-in and prints its estimated cost before it starts.
5. No Azure OpenAI dependency: availability on student subscriptions is not guaranteed, so the gateway
   treats it as one optional provider among several.
