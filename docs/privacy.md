# Privacy

AES is designed for local, inspectable operation. By default it enables no
telemetry and exports no prompt, response, or tool content. The local monitor
ledger and queue cache may contain operational metadata and remain on the
configured machine unless an operator copies or synchronizes them.

When an operator enables a provider, GitHub workflow, remote worker, or OTLP
collector, data is processed by that service under its account, terms, and
retention settings. AES does not make those services private or determine what
they retain. Review provider settings and permissions before enabling them.

Credentials belong in the local environment or a CI secret store, never in
tracked files, issue bodies, logs, or generated artifacts. Queue operators are
responsible for the host, filesystem, network boundary, runner account,
resource limits, and any data reachable by a model or tool. A self-hosted queue
is experimental and must be reviewed as a separate data-processing system.

The default configuration intentionally leaves content logging variables unset.
If telemetry is enabled, configure only the metadata needed for diagnostics and
verify the collector's policy independently. This document describes AES's
defaults; it is not legal advice or a substitute for the operator's privacy
notice and threat model.
