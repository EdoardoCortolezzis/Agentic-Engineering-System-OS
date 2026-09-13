# ADR 0010: Checks fail closed

Accepted. Required CI, guardrail, and parsing checks report an explicit failure
when input, dependencies, or invocation are unavailable. Operational errors
must never become green results; ambiguous outcomes require human inspection.
