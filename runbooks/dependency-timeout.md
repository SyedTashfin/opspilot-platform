---
title: Dependency timeouts and retry amplification
service_class: any
symptoms: [timeouts to one dependency, latency spikes, retry counts rising, load on a downstream]
tags: [timeout, retry, amplification, cascade]
---

# Dependency timeouts and retry amplification

## Symptoms

- Timeouts or near-timeouts to one dependency, while the dependency itself reports latency below its
  own timeout threshold for most requests.
- Request latency multiplied by retry count rather than added to it.
- The dependency's own load rising *because of* the retrying caller.

## Diagnosis

1. Read the timeout and retry configuration as deployed, not as documented. Effective values come
   from configuration, code defaults and in-flight overrides.
2. Compute the worst case: `attempts x timeout` plus backoff plus queueing. If that exceeds the
   caller's own timeout, the caller will fail while the dependency is merely slow.
3. Check for retry amplification: multiply the retry rate by the original request rate to see the
   load placed on the dependency. This is the step most investigations skip.
4. Look for a fixed-size resource in the caller: connection pool, worker count, thread pool. Waiting
   for a free slot is not a dependency timeout but presents identically.

## Likely causes

- A deployment that shortened a timeout while lengthening retries (or the reverse).
- Retries without jitter and without a budget, converting slowness into a stampede.
- A synchronous call to a dependency on the critical path that could be cached or made asynchronous.

## Remediation

1. Make retries bounded and budgeted: cap total attempts, add jitter, and prefer fail-fast over a
   long tail of attempts.
2. Set the caller's timeout above the dependency's realistic p99, or reduce attempts until it fits.
3. Protect the dependency with a circuit breaker or bulkhead so degradation stays partial.
4. Only then consider raising capacity.

## Verification

- Retry rate returns to baseline before latency does; latency follows.
- p99 latency for the caller stops being dominated by `attempts x timeout`.
- The dependency's own load returns to its pre-incident level.

## Escalation

If timeouts persist with retries disabled, the dependency is the problem: escalate with its latency
percentiles and the caller's timeout budget.
