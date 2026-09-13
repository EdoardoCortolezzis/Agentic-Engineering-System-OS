# ADR 0015: GitHub Issues are the task control plane

Accepted. Issue labels describe task state, the issue body contains the
execution contract, and an atomic remote Git ref is the ownership lock. A
local marker is audit metadata only; lost claims are abandoned, never
replaced. Product decisions and merge approval remain human actions.
