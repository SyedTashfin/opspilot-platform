---
title: Database connectivity failures
service_class: stateful
symptoms: [connection refused, pool exhausted, timeouts on queries, 5xx after a deploy]
tags: [database, connection-pool, timeouts]
---

# Database connectivity failures

## Symptoms

- Elevated 5xx on every endpoint that touches the database, uniform across routes.
- `connection refused`, `too many clients`, `remaining connection slots are reserved`, or pool
  acquisition timeouts in application logs.
- Database CPU is *not* necessarily high: a pool exhaustion looks like latency, not load.

## Diagnosis

1. Confirm scope: if one route fails and others are healthy, suspect that route's query, not
   connectivity. If all routes fail together, suspect the pool, the network path, or the database
   itself.
2. Compare connection metrics against the pool ceiling: `pool_size + max_overflow` versus observed
   concurrent borrowers. A pool that is continuously saturated turns ordinary latency into timeouts.
3. Check whether the failure began with a deployment. Connection behaviour changes (pool sizes,
   statement timeouts, retry counts, TLS settings) are a common cause and have a precise timestamp.
4. Look for leaks: connections held across an `await` that can block indefinitely, or sessions not
   released on error paths.
5. Check the database side directly: active connections, longest running query, lock waits.

## Likely causes

- Pool exhaustion from a leak or from a shared client being created per request.
- A migration that added a long lock.
- Network policy, security group or certificate change between application and database.
- Dependency latency pushing every query past a newly shortened statement timeout.

## Remediation

- Fix the leak or raise the pool ceiling *after* understanding the leak; raising it first hides the
  problem and moves the failure to the database.
- Roll back the deployment that changed connection behaviour.
- Shed load (rate limit, queue) rather than retrying into a saturated pool.

## Verification

- Pool saturation returns to baseline; 5xx return to the pre-incident rate; longest query time drops.
- Confirm with a tail of application logs, not with the absence of alerts alone.

## Escalation

If saturation persists after the pool is restored, escalate to the database owner with active
connection counts, longest query, and lock waits attached.
