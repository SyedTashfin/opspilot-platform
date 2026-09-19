# apps/web

Placeholder. The Next.js dashboard lands in M11, after the platform API, the agent pipeline and the
evaluation harness exist — a UI over a system that does not work yet would be a mockup, and this
project does not ship mockups.

Navigation is fixed by the product spec: Dashboard, Agents, Runs, Evaluations, Tools, Infrastructure,
Observability.

Design constraints carried into M11:

- density and credibility over decoration — closer to Datadog, Grafana, Linear and the Azure portal
  than to a consumer chatbot;
- no gradients-as-personality, no glowing blobs, no chatbot-first layout, no fake dashboards;
- every metric rendered carries `source: measured | seeded | simulated`, and seeded demo data is
  visually distinct from data measured in the current session;
- the run view renders structured operational traces (actions, tool calls, observations, evidence,
  decisions) and never a private chain of thought.
