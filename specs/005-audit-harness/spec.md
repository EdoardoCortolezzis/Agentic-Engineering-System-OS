# Specification: harness audit

## Goal

Prevent false-green diagnostics in the harness by executing each guard and
checking both success and failure paths.

## BDD scenarios

Scenario: a missing monitor entry point is detected
Given the monitor manifest lists modules but its hook is absent
When the doctor runs
Then it reports the monitor as unhealthy.

Scenario: a dependency cycle is visible
Given two tasks depend on each other
When the task graph is inspected
Then `find_cycle` reports the cycle instead of returning a healthy flow.

Scenario: a protected push is blocked
Given a push command targets a protected branch through an option or refspec
When the guard evaluates it
Then the command is blocked.

Scenario: a self-propagating script remains executable
Given synchronization includes the script that is currently running
When the asset is replaced
Then atomic rename preserves the executing inode and the run completes.

Scenario: stale state is not called current
Given a claim or provider observation is stale
When diagnostics run
Then the state is reported as stale or unavailable and no unsafe action occurs.

## Audit method

Every new harness guard needs a deterministic eval with a mutation that would
fail if the guard were removed. A successful process is not evidence of a
healthy system unless its output and side effects satisfy the contract.
