# Review criteria

These criteria make the repository's own rules visible to automated and human
review. They supplement `AGENTS.md`.

## Blocking

1. Provider-specific behavior must live in configuration, not application
   logic.
2. A deviation from `ROADMAP.md` or an existing ADR needs a new ADR in the
   same change.
3. Secrets, tokens, and credentials must never appear in source, fixtures,
   logs, or generated artifacts.
4. Account creation, public publication, real spending, and irreversible
   destructive actions require an explicit human gate.
5. Documentation that becomes false must be updated with the behavior change.
6. CI checks must not become green by skipping a missing secret or ignoring a
   failed check.

## Non-blocking findings

Report oversized modules, one-use abstractions, missing tests where an
established pattern exists, and unrelated formatting or refactoring. Do not
spend review time on subjective naming or formatting already covered by an
automated formatter.
