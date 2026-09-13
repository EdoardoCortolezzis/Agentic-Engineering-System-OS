# Specification: harness synchronization

## Goal

Propagate a manifest of harness assets to a consumer with explicit provenance,
local drift detection, and declared overrides.

## BDD scenarios

Scenario: a copy asset is synchronized
Given a manifest row with `copy` mode
When sync runs
Then the destination matches the source and its digest is recorded.

Scenario: a seed asset is preserved
Given a destination already exists for a `seed` row
When sync runs
Then the destination is not overwritten.

Scenario: managed JSON keys are merged
Given a consumer JSON file has unmanaged keys
When `merge-json` runs
Then managed keys match the source and all other keys are preserved.

Scenario: local drift is reported
Given a managed destination was changed after synchronization
When `--check` runs
Then it fails and identifies the changed asset.

Scenario: upstream age is checked separately
Given local assets are intact but the source revision is newer
When `--check-upstream` runs
Then it reports staleness without rewriting the consumer.

## Contract

Sources are repository-relative and each asset has one source of truth. Sync
writes through a temporary file and atomic rename, so an executing script is
not truncated while it propagates itself. `.harness-overrides` records
intentional divergence. The check validates integrity; it is not an update
operation.
