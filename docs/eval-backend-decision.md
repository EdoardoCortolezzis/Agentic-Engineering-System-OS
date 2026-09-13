# Evaluation methodology

The deterministic suites remain the default because they are fast, reviewable,
and run without provider credentials. Use `pass@1` for a single attempt and
`pass^k` when reliability under repetition matters: `pass^k` requires all `k`
attempts to pass and is stricter than `pass@k`.

An external evaluation backend is deliberately not a core dependency. When a
real trajectory workload justifies one, add a thin pytest-native adapter,
record the decision in an ADR, and keep provider credentials out of the eval
fixtures. Until then, the repository's ordinary test runner is the supported
backend and no remote evaluation service is assumed.
