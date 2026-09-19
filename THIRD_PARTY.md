# Third-party components

Runtime dependencies only. Licence values are recorded from the package metadata / repository at
the time of adoption; the authoritative text is in each project's repository.

| Component | Version | Licence | Usage |
| --- | --- | --- | --- |
| FastAPI | >=0.115 | MIT | HTTP API framework |
| Uvicorn | >=0.32 | BSD-3-Clause | ASGI server |
| Pydantic / pydantic-settings | >=2.9 | MIT | schemas, configuration |
| SQLAlchemy | >=2.0.36 | MIT | ORM, async engine |
| asyncpg | >=0.30 | Apache-2.0 | PostgreSQL driver |
| Alembic | >=1.14 | MIT | migrations |
| structlog | >=24.4 | MIT / Apache-2.0 | structured logging |
| pgvector (image, M3) | pg16 | PostgreSQL licence | vector extension |
| PydanticAI (M2) | >=0.0.x | MIT | typed agent/tool abstraction |
| LiteLLM (M2) | >=1.x | MIT (core; enterprise files separately licensed) | provider abstraction, cost tables |
| OpenTelemetry SDK (M5) | >=1.2x | Apache-2.0 | tracing/metrics |
| Arize Phoenix (M5, separate container) | latest | Elastic License 2.0 (use as service, never vendored) | trace viewer |
| MCP Python SDK (M3) | >=1.x | MIT | MCP client |
| Next.js (M11) | >=15 | MIT | frontend |
| Terraform (M9) | >=1.9 | BUSL-1.1 (own use permitted) | infrastructure as code |

No source code has been copied from any of these projects. Where a pattern was borrowed, it was
reimplemented. AGPL and ELv2 components are used only as separate services, never linked or vendored.
