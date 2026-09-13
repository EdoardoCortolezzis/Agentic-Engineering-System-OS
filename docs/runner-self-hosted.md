# Experimental self-hosted runner

This document is an operator example for the optional remote queue. It is not
the supported local baseline and does not promise a hosted service. GitHub
Actions remains the scheduler; the runner makes outbound requests and no
inbound queue endpoint is required.

## Boundaries

Use a dedicated operating-system account and keep provider binaries, hooks,
credential directories, quota cache, and archived ledgers outside checkouts.
Multiple runners may share a host, but this does not isolate one checkout from
another. A model with access to one runner account may be able to inspect other
workspaces; use separate hosts or a stronger sandbox when that matters.

An example layout is:

```text
/opt/aes/hooks/                 root-owned job hooks
/var/lib/aes/runners/<repo>/    one runner installation per repository
/var/lib/aes/quota/             shared quota cache, mode 0700
/var/lib/aes/codex-home/        managed credential directory, mode 0700
/var/lib/aes/claude-config/     worker configuration, mode 0700
/var/lib/aes/state/archive/     archived ledgers
/run/aes/jobs/                  active-job locks
```

The operator owns the actual account home and must keep it out of tracked
documentation. The hooks must be root-owned and not writable by the runner.

## Resource limits

Apply a shared systemd slice and per-runner limits. The values below are
examples, not a universal capacity recommendation:

```ini
[Slice]
MemoryHigh=6G
MemoryMax=6800M
CPUQuota=350%
```

The start hook rejects a job when free disk space is below its configured
threshold and waits for a concurrency slot. It fails closed when it cannot
measure disk space, acquire its gate, or validate its numeric and absolute-path
configuration. The completion hook never fails the job; it releases only its
own lock, archives the ledger when possible, and leaves dirty or uninspectable
worktrees for a human.

Hooks read root-owned configuration rather than job-controlled environment
variables. A `--config` argument is an explicit operator action. These limits
defend against mistakes and overload, not a malicious process running as the
same account.

## Credentials and restart behavior

Use an OS keyring or an equivalent managed credential store. Keep credential
directories outside checkouts, reject plaintext auth files, and install the
CLI's code-mode host alongside the CLI binary. After a host restart, an
operator must unlock the keyring and verify the provider before the queue can
resume. A missing or stale provider state becomes `aes:waiting-provider` or
`aes:needs-human`; it must not trigger an implicit fallback.

## Recovery

Inspect runner labels, keyring state, `AES_QUEUE_ENABLED`, identity checks, and
the quota cache when a queue is idle. A worker hook rejection can leave an
issue claimed; a human must reconcile the issue label and remote claim ref.
Do not remove a lock or ledger merely to make the queue appear healthy.

The example configuration in `ops/hetzner/` is experimental and must be adapted
to the operator's host, filesystem, account, and threat model before use.
