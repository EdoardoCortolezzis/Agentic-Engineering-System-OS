# ADR 0011: Bounded review work

Accepted. Automated review is bounded by configured turn and retry budgets.
Repeated attempts are an escalation and must not silently consume unbounded
provider quota. An incomplete review never authorizes a merge.
