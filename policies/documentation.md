# Documentation policy

Documentation is updated in the same pull request as the behavior it
describes. A later cleanup is not a substitute for keeping the public contract
true at merge time.

Update the following in the same change when applicable:

| Change | Required update |
|---|---|
| Observable behavior | `STATE.md` |
| Usage or execution | `README.md` |
| Agent rule | `AGENTS.md` or the referenced policy |
| Roadmap or architecture decision | `ROADMAP.md` or a new ADR |
| Specified behavior | The relevant file under `specs/` and its eval |
| Resolved roadmap item | `ROADMAP.md` |

Do not add a duplicate changelog, copy the same fact into several documents,
or document an unimplemented promise. `STATE.md` records failed checks,
measured defects, and deferred work as honestly as successful work. Do not
rewrite unrelated documentation merely because it is nearby.

Review verifies that documentation remains accurate. The synchronization eval
also verifies that this policy is propagated to consumers. Keep public
documentation in English and keep credentials, machine paths, and private
history out of tracked files.
