# Experimental runner examples

These files are generic, reviewable examples for a Linux self-hosted runner.
They are not automatically deployed and are not propagated to consumers. An
operator must adapt account names, service paths, ownership, capacity, and
security controls to the target host.

| File | Example destination |
|---|---|
| `aes.slice` | systemd slice configuration |
| `aes-limits.conf` | per-runner systemd drop-in |
| `aes.conf` | systemd-tmpfiles configuration |
| `job-started.sh` | root-owned runner hook |
| `job-completed.sh` | root-owned runner hook |
| `aes-keyring-unlock` | operator-only keyring helper |
| `limits.conf` | root-owned hook configuration |

Keep hooks root-owned and unwritable by the runner. Register them with
`ACTIONS_RUNNER_HOOK_JOB_STARTED` and `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` only
after reviewing the data and resource boundaries. See
[`docs/runner-self-hosted.md`](../../docs/runner-self-hosted.md).
