# ADR 0017: Review uses managed subscriptions

Accepted. The default review path uses the operator's managed CLI subscription
rather than provider API-key credentials. Missing quota, credentials, or
sandbox attestations stop the operation and request human repair.
