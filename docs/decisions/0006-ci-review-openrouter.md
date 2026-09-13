# ADR 0006: Configurable CI review backend

Accepted. CI review uses the configured Anthropic-compatible endpoint and
model. Provider selection belongs in workflow configuration, not application
code, so a consumer can choose a compatible backend without changing the
harness. Third-party actions remain pinned and missing credentials fail
visibly.
