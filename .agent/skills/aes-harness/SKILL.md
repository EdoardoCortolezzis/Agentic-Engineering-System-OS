---
name: aes-harness
description: >
  Verify that AES is installed and current in a consumer repository, and
  install it when absent. Use this skill before implementing work in a
  repository that relies on AES. It distinguishes local drift from source
  staleness and covers clean installation, sync, and human approval gates.
---

# AES harness synchronization

The harness is vendored as versioned files, not imported as a runtime package.
Always establish which of these states applies before changing consumer code:

| State | Check | Action |
|---|---|---|
| Absent | `harness/` is missing | Install from a trusted AES checkout. |
| Locally changed | `aes-sync.sh --check` fails | Review and sync, or declare an override. |
| Source is newer | `aes-sync.sh --check-upstream` fails | Update source and review the diff. |
| Healthy | Both checks pass | Continue with the requested work. |

`--check` validates the consumer's managed assets against `.harness-version`;
it does not prove that the source revision is current. Use `--check-upstream`
for that separate question.

## Install

From the consumer root, invoke the copy that lives in AES:

```sh
cd /path/to/consumer
/path/to/agentic-engineering-system/harness/scripts/aes-sync.sh \
  --source /path/to/agentic-engineering-system
```

The sync creates missing directories, copies managed scripts and policies,
seeds local configuration, merges only managed hook keys, and writes
`.harness-version`. In a checkout whose AES source is not in the conventional
parent directory, repeat the source argument for the integrity check:

```sh
harness/scripts/aes-sync.sh --check --source /path/to/agentic-engineering-system
```

Run this check immediately after installation.

## Preconditions and human gates

Stop for human direction when the consumer lacks the branches required by its
configured Git policy, has uncommitted work in files the sync will touch, or
needs an account, credential, permission, or product decision. The initial
installation and every update are ordinary feature changes: use a branch and a
pull request, and leave merge approval to a human.

## Update and overrides

```sh
cd /path/to/consumer
harness/scripts/aes-sync.sh
git diff
```

If a managed file intentionally differs, list its path and rationale in
`.harness-overrides`. Do not edit a propagated asset only in the consumer: make
the source change in AES, then sync and review it. Do not infer freshness from a
green `--check`; it checks integrity, not upstream age.
