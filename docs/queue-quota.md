# Queue quota configuration

The optional queue uses managed CLI subscriptions only. AES does not accept,
store, or forward provider API keys; `GH_TOKEN` is solely a GitHub credential
for issue and pull-request operations. The controller route is `gpt-5.6-sol`
with `claude-opus-5` fallback; workers use `gpt-5.6-luna` with
`claude-sonnet-5` fallback. Claude is attempted only after Codex quota
exhaustion is confirmed.

The queue is disabled unless `AES_QUEUE_ENABLED=true`. Supported capacity and
execution settings are `AES_QUEUE_MAX_CONCURRENCY`,
`AES_QUEUE_MAX_CONCURRENCY_PER_REPO`, `AES_QUEUE_BUDGET`,
`AES_MODEL_TRANSIENT_RETRIES`, and `AES_MODEL_TIMEOUT_SECONDS`.

## Shared cache

Workers share one atomic JSON cache outside the checkout:

```text
AES_QUOTA_CACHE_ROOT=/var/lib/aes/quota
AES_QUOTA_CACHE_PATH=quota.json
AES_QUOTA_CACHE_TTL_SECONDS=300
AES_TRUSTED_ACTOR=queue-service-account
```

`AES_QUOTA_CACHE_ROOT` must be an absolute operator-managed directory;
`AES_QUOTA_CACHE_PATH` must resolve inside it. Missing values, traversal,
unauthorized symlinks, and paths inside the checkout are rejected. The cache
does not contain credentials or raw model payloads.

## Provider behavior

The worker refreshes Codex quota at startup. If refresh fails, it does not use a
stale value or silently switch provider: it records `aes:waiting-provider` and
leaves the execution for a later retry. A Claude status-line collector writes
the same cache. On weekdays from 08:00 to 17:00 Europe/Berlin, a missing or
stale observation, or usage at least 70%, blocks Claude. Outside that window,
unknown status remains unknown and does not authorize an implicit fallback.

If a provider cannot be configured safely, the worker records
`aes:needs-human`; it does not request credentials through a prompt or commit
them to the repository. A product manager receives the event through the
configured GitHub notification path; email depends on the account's
notifications/watch settings and is not an SMTP service managed by AES.

## Managed CLI requirements

Self-hosted execution must use `AES_CODEX_MANAGED_CREDENTIALS=1` and
`CODEX_HOME` pointing to a dedicated credential directory outside the worktree;
that directory must use an OS keyring. The operator must also install the
CLI's code-mode host alongside the CLI binary. The optional Claude fallback
requires `AES_CLAUDE_MANAGED_SANDBOX=1` and its own managed CLI configuration.
These are deployment prerequisites, not secrets that AES can infer or create.
A missing prerequisite stops the worker and requires human repair.

For local execution, configure `CODEX_HOME` directly. `AES_CODEX_HOME` is only
an optional workflow mapping (for example, a CI variable mapped to
`CODEX_HOME`); it is not a replacement runtime variable and contains no
credential value in this template.
