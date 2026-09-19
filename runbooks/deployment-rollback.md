---
title: Rolling back a deployment
service_class: any
symptoms: [incident began at a deployment, error or latency change with a deploy timestamp]
tags: [deployment, rollback, change-correlation]
---

# Rolling back a deployment

## Symptoms

- The incident start time coincides with a deployment (within a few minutes).
- Behaviour changed without traffic changing.
- A configuration or dependency change shipped with the release.

## Diagnosis

1. Establish the correlation precisely: deployment timestamp, first observed anomaly, and whether
   any other change landed in the same window.
2. Read the diff, not the release notes. Release notes describe intent; the diff describes effect.
3. Identify the blast radius: which services, routes or tenants consume the changed behaviour.
4. Decide rollback versus roll-forward: roll back when the change is not needed to mitigate the
   incident, roll forward when the old version is worse or the migration is irreversible.

## Likely causes

- Timeout, retry or pool settings changed with the release.
- A schema or contract change consumed by an older client.
- Feature flag enabled with the deploy rather than before it.

## Remediation

1. Prefer forward-fix for additive changes with an obvious one-line correction.
2. Otherwise roll back to the last known good version, and record the version that was running.
3. Roll back configuration in the same step as code; a version mismatch is its own incident.

## Verification

- The metric that defined onset returns to its pre-deploy baseline.
- Confirm the rollback actually took effect: check the running version, not the pipeline status.

## Escalation

If rollback does not restore behaviour within one deployment interval, the deployment is not the
whole cause. Re-open the investigation from the remaining signals.
