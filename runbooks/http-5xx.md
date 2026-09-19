---
title: Elevated HTTP 5xx responses
service_class: stateless
symptoms: [error rate above baseline, single route or service wide, downstream failures]
tags: [5xx, availability, errors]
---

# Elevated HTTP 5xx responses

## Symptoms

- Error rate above baseline, with a clear onset.
- Errors concentrated on one route, one status code, or spread across all routes.

## Diagnosis

1. Split by status code. 500 means the application raised; 502/503/504 mean something in front of the
   application (proxy, gateway, upstream) failed or timed out.
2. Split by route. One route failing points at code or one dependency; all routes failing points at
   shared infrastructure, credentials or a global configuration change.
3. Read the errors, do not aggregate them. The first distinct error message usually names the cause;
   the thousands that follow are consequences.
4. Check the deployment and configuration history for the incident window.
5. Check dependency health: an unavailable dependency often surfaces as 5xx locally.

## Likely causes

- A dependency returning errors or timing out, with insufficient timeout and retry discipline.
- Invalid or expired credentials after a rotation.
- A code path that raises on unexpected input.
- Infrastructure: capacity, certificate expiry, DNS.

## Remediation

- Fix the dependency or shed the traffic it cannot serve.
- Where a dependency is degraded, return a truthful degraded response rather than a 5xx where the
  product can tolerate it.
- Roll back a change that correlates with onset.

## Verification

- Error rate returns to baseline and stays there for at least one full traffic cycle.
- Confirm the specific error message disappeared from logs, not merely that the aggregate dropped.

## Escalation

Attach the first distinct error, the affected routes, and the change history in the incident window.
