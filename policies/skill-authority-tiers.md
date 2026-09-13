# Skill authority tiers

Assign every skill in `.agent/skills/` an authority tier before treating it as
ready. The tier determines the required eval bar.

| Tier | Definition | Minimum verification |
|---|---|---|
| `read-only` | Inspects or reports without side effects. | Functional test on known input. |
| `draft-only` | Produces work that a human or later step must approve. | Three positive and three negative trigger cases. |
| `action-allowed` | Performs an externally visible or irreversible action. | The draft bar plus a test that missing human authorization stops safely. |

Use `action-allowed` sparingly. A skill must state its scope, required
preconditions, failure behavior, and the data it may access. A skill cannot
turn an account setup, credential grant, or product decision into an automatic
step.

Third-party frameworks are optional dependencies, not part of the core
harness. Adopt one only after a bounded trial demonstrates a concrete benefit
and an ADR records the trade-off.
