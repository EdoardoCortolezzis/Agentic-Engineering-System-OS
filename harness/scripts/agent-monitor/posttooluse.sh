#!/usr/bin/env bash
# PostToolUse hook for the local monitor. This asset is propagated by aes-sync.
#
# Derive the root from the project (`$CLAUDE_PROJECT_DIR`, falling back to Git),
# never from a machine-specific path. A project hook already scopes execution.
#
# Any error exits 0: the monitor warns but never stops work (ADR 0011).

[ -n "${AES_MONITOR_ACTIVE:-}" ] && exit 0

ROOT="${CLAUDE_PROJECT_DIR:-}"
[ -n "$ROOT" ] || ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "$ROOT" ] || exit 0

MONITOR="$ROOT/harness/scripts/agent-monitor/hook.py"
[ -f "$MONITOR" ] || exit 0

python3 "$MONITOR" 2>/dev/null || true
exit 0
