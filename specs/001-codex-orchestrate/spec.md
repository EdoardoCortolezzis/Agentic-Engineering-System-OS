# Specification: bounded CLI orchestration

## Goal

Provide an opt-in wrapper around complete CLI invocations that preserves
explicit human handling of ambiguity. The wrapper is a protocol, not a model
runtime or sandbox.

## BDD scenarios

Scenario: a task completes in one round
Given a bounded task with no ambiguity
When the output contains `PHASE:` and `DONE`
Then the session status is `done`.

Scenario: an implementation question pauses work
Given a running session
When the output contains `QUESTION:`
Then the status is `awaiting_answer` and no automatic retry occurs.

Scenario: an answer resumes work
Given a session awaiting an answer
When the supervisor supplies a non-empty answer
Then the next prompt includes the prior question and answer.

Scenario: the iteration cap is reached
Given a session at `MAX_ITER`
When another round is requested
Then the next action is `cap_reached` and the session stops.

Scenario: unmarked output is not treated as success
Given a running session
When output contains no recognized marker
Then the status remains `in_progress`.

## Contract

State is JSON and includes a task, status, iteration, history, and last
question. Session IDs accept only safe identifier characters. The wrapper uses
argument arrays, not shell interpolation, for user input. It stores state and
logs under a gitignored directory and never merges or pushes protected refs.
