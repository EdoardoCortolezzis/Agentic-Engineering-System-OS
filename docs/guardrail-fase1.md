# Guardrails

AES uses layered, fail-closed checks around Git, hooks, task claims, and
provider execution. Each guard should have a deterministic eval for its
allowed and blocked paths and should report an actionable reason.

The native sandbox or an external process sandbox may be used by an operator,
but AES does not assume that a sandbox covers every hook, MCP server, or
filesystem path. Review the complete process boundary before treating it as a
security control. A checkpoint or recovery mechanism is useful only when its
failure behavior is tested as well.

Guardrails reduce accidental harm; they do not replace least-privilege
credentials, host hardening, provider policy, or human review.
