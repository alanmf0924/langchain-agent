# AutoPE Prompt Rollback Runbook

## Trigger

Roll back the canary immediately when any configured release-policy threshold is met, a
Promptfoo hard gate fails, or an operator identifies a safety, evidence, price, stock, or
transaction-boundary violation.

## Procedure

1. Stop canary expansion and mark the release candidate as frozen.
2. Switch the application configuration to the last registered `approved` Prompt version.
3. Restart or reload the application configuration using the standard deployment procedure.
4. Verify the active Prompt version hash matches the prior approved version.
5. Run the deterministic backend suite and the Promptfoo golden gate before ending the incident.
6. Preserve the promotion report, canary metrics, trigger, actions, timestamps, and owner in
   the incident record. A new candidate requires a new evaluation and release approval.

## Boundaries

Do not weaken golden assertions, modify the release threshold, or delete telemetry to clear an
alert. Do not use rollback to alter prices, stock, orders, customer identity, or permissions.
