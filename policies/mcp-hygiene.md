# MCP hygiene policy

Use at most one MCP server per project, expose real data read-only by default,
and supply credentials only through environment or CI secret storage. Run
`uvx mcp-scan@latest scan` after every MCP configuration change, not only once
when a project is created.

Before enabling a server:

1. Confirm that it does not expose an unreviewed destructive action.
2. Keep credentials out of configuration files and tracked logs.
3. Keep project boundaries separate; do not share a privileged server across
   unrelated repositories.
4. Run `uvx mcp-scan@latest scan --dangerously-run-mcp-servers` only when the
   operator has reviewed the risk of starting the server subprocesses.

An empty MCP configuration is a valid and preferred baseline. A scan result is
evidence for review, not a guarantee that a third-party server is safe.
