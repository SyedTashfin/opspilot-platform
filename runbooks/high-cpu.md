---
title: High CPU saturation
service_class: compute
symptoms: [sustained CPU above 90%, latency rising with load, queue growth, timeouts]
tags: [cpu, saturation, throttling]
---

# High CPU saturation

## Symptoms

- CPU utilisation pinned near the limit for more than a few minutes.
- Latency rising roughly linearly with request rate while throughput stalls.
- Autoscaling either cannot keep up or is not permitted.

## Diagnosis

1. Distinguish *demand* CPU from *pathological* CPU: compare request rate with before the incident.
   Same traffic and higher CPU means a code or configuration change, not load.
2. Check whether throughput fell but CPU stayed high. That pattern indicates wasted work: retry
   storms, unbounded loops, or lock contention spinning.
3. Attribute the CPU: thread-level profile or a flame graph if available. Unattributed "high CPU"
   is not a diagnosis.
4. Check for a companion signal. High CPU with high memory suggests garbage collection pressure;
   high CPU with high latency to one dependency suggests retry amplification.

## Likely causes

- Retry amplification: a slow dependency plus aggressive retries multiplies CPU per request.
- A deployment introducing an expensive path (serialisation, regular expressions, N+1 queries).
- Traffic that grew beyond the current capacity envelope.

## Remediation

- Stop the amplifier first (reduce retries, add timeouts) before adding capacity.
- Roll back the offending deployment if the change correlates with the onset.
- Scale out only when the work itself is legitimate and necessary.

## Verification

- CPU returns to the level that matches the current request rate.
- Latency follows CPU down; queue depth drains.
- Confirm the retry rate metric dropped, not just CPU.

## Escalation

If a single dependency drives the retries, escalate to its owner with the retry rate and the
deployment that introduced the amplification.
