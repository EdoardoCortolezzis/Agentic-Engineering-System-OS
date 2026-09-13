# Observability

The supported baseline is local deterministic verification and an inspectable
local ledger. If an operator enables OpenTelemetry, export only the metadata
needed to diagnose sessions, tools, errors, and budget; keep prompt, response,
raw body, and tool-content variables disabled by default.

Tail-based sampling is deferred until trace volume makes full review
impractical. Budget limits belong at the provider or queue boundary as well as
in the client because a client-only cap cannot protect a compromised
credential. Any new exporter or collector requires a data-flow review and an
eval for failure and retry behavior.
