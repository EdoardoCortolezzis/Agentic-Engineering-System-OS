# Feature specification template

## Goal

Describe the user-visible behavior and the boundaries that must remain true.

## Design

List the smallest implementation surface, dependencies, failure behavior, and
human approval gates. Keep provider-specific details in configuration.

## BDD scenarios

Scenario: happy path
Given the required preconditions
When the user or worker performs the action
Then the expected observable result occurs.

Scenario: invalid or unsafe input
Given a missing, malformed, or unauthorized precondition
When the action is attempted
Then it fails closed with an actionable reason.

## Evaluation

Name the deterministic or trajectory eval that proves each scenario. Include a
negative case and a mutation that would fail if the guard were removed.
