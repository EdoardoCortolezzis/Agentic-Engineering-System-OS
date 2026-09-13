# ADR 0024: Queue state survives checkout replacement

Accepted. Durable queue state and quota observations live outside the
ephemeral runner checkout. If the external state root is missing or unsafe,
the worker stops and requests human repair instead of pretending a resume is
safe.
