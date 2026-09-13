# Specification: CI harness propagation

## Goal

Allow a consumer to request an explicit harness update through a reviewed pull
request without hiding drift, overwriting human work, or merging automatically.

## BDD scenarios

Scenario: a clean source produces a pull request
Given a consumer has a clean stable branch and a newer trusted source
When the propagation workflow runs
Then it creates a feature branch and pull request with the changed assets.

Scenario: local drift stops propagation
Given a managed asset differs from its recorded digest
When propagation starts
Then it stops without rewriting the consumer.

Scenario: uncommitted work stops propagation
Given the stable branch has uncommitted changes
When propagation starts
Then it stops and asks for human cleanup.

Scenario: merge remains human
Given a propagation pull request is green
When the workflow completes
Then it reports the pull request and does not merge it.

## Contract

The workflow uses least-privilege credentials, pins its source revision, and
invokes `aes-sync.sh` through the manifest. Divergence must be declared in
`.harness-overrides`; no workflow rewrites local changes silently.
