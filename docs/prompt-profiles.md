# Prompt profiles

The harness can use role-specific prompts above the provider/model routing
layer. Profiles are optional and are invoked one at a time by a supervisor.

| Profile | Responsibility |
|---|---|
| Architect | Understand scope, split bounded work, and preserve acceptance criteria. |
| Builder | Implement one bounded change and run its focused checks. |
| Forensic | Review correctness, edge cases, permissions, and data exposure. |
| Author | Update state, decisions, and public documentation after verification. |

Profiles choose a role; routing chooses an available model. Keep those concerns
orthogonal and do not introduce parallel orchestration until a measured use
case justifies it.
