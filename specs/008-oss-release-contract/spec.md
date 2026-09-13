# Specification: OSS release contract v0.1.0

## Goal

Define the public and reproducible contract for the OSS v0.1.0 release in the
real repository. The distributed repository must be clean, installable through
the supported harness path, and verifiable without exposing personal data,
credentials, or artifacts that are not intended for users.

## Non-goals

- Implement runtime features or change script behavior.
- Publish a release, create tags, open pull requests, or change documentation
  outside this contract and its evaluation.
- Create a duplicate `release/` subtree as a substitute for the public
  repository.
- Include pilot plans, macOS-specific artifacts, or remote-queue components as
  stable functionality.

## BDD scenarios

Scenario: the public README identifies the v0.1.0 release in English
  Given the repository is ready for release audit
  When a user opens the README at the repository root
  Then they find an English headline, essential sections, and the v0.1.0
  reference without consulting internal files

Scenario: installation documents the supported aes-sync path
  Given a user starts from a compatible empty consumer
  When they follow the multiline installation command in the README
  Then the Installation section contains a code fence with the same aes-sync
  sequence using `--source` and documenting the real `--check` verification,
  including the source argument on both invocations

Scenario: the README distinguishes core and optional components
  Given the repository contains multiple asset categories
  When a user reads the Components section
  Then they can explicitly distinguish core assets from optional assets

Scenario: privacy is secure by default
  Given a new installation has no additional configuration
  When a user reads the Privacy section
  Then it explicitly states that content and telemetry are not sent by default

Scenario: the root contains the complete MIT license for the declared owner
  Given the repository is distributed to third parties
  When a user checks `LICENSE` at the root
  Then it contains the complete MIT text byte for byte, with the declared
  copyright line and exactly one final newline

Scenario: the distribution excludes pilot plans and macOS artifacts
  Given tracked content is the candidate distribution
  When the path audit runs
  Then it contains no pilot plans in paths or text, `.DS_Store`, AppleDouble
  files, `__MACOSX`, or equivalent macOS metadata

Scenario: every remote capability is marked experimental
  Given the README mentions a queue or remote functionality
  When a user evaluates whether to use it in production
  Then the README contains an `## Experimental` section with a line or
  sentence mentioning remote and experimental together

Scenario: the audit rejects concrete personal paths, tokens, and private keys
  Given all tracked text files intended for distribution
  When the audit searches for local personal paths, tokens, and private keys
  Then it finds no sensitive values or author-machine references, while a
  documented generic home-relative configuration such as `~/.codex` is not
  classified as a personal path

Scenario: distributed files do not name consumers or named plans
  Given all tracked distribution files
  When the audit searches for consumer names and named project plans
  Then it finds no private consumer names or equivalent references

Scenario: public artifacts are regular files and not symlinks
  Given README and LICENSE are the verified public artifacts
  When the audit checks their root inodes
  Then both exist as regular files and are not symlinks

## Design

The release candidate is the real repository, not a copy under `release/`. The
evaluation reads tracked paths from `git ls-files` at the repository root and
applies deterministic content and path checks. To run the evaluation against
an isolated fixture, set `AES_REPO_ROOT`; this test-only variable is not part
of the public contract. Text checks exclude only this specification and its
evaluation, which contain deliberately prohibited patterns; documentation and
the actual product remain in scope.

The root contains at least `README.md` and `LICENSE`. The README is the public
contract for version, installation, components, privacy, and experimental
status of remote capabilities. In the `Installation` section, one code fence
must contain the command sequence invoking `aes-sync.sh --source` and
`aes-sync.sh --check --source`; line continuations are normalized before the
check, while text outside the fence does not count. `LICENSE` must match the
canonical MIT text byte for byte with the declared copyright line and one final
newline. Non-text files are opaque to content checks, but their names and paths
are still checked.

The personal-path audit distinguishes a generic home-relative configuration
(`~/...`, such as `~/.codex` when documented) from a concrete machine or user
reference. It blocks absolute paths under `/Users/<user>`, `/home/<user>`,
`/Volumes/...`, `/private/var/folders/...`, equivalent Windows paths, and
`~<user>/...`. The token audit covers documented common patterns including AWS
`AKIA`/`ASIA`, GitHub, GitLab `glpat-`, npm `npm_`, Hugging Face `hf_`, Stripe
`sk_live_`, Slack, Bearer, JWT, private keys, and generic credential
assignments. It does not replace a complete secret scanner.

For remote capabilities, the evaluation ignores code fences and searches the
Markdown prose for remote execution, queue, or worker references. If it finds
one, it requires `## Experimental` and checks for a single line or sentence in
that section containing both `remote` and `experimental`; a label inferred
from an entire code block is insufficient.

## Evaluation

Path: `evals/oss-release-contract/test_release_contract.py`.

| Test case | Covered scenario |
|---|---|
| `test_public_readme_is_english_and_versioned` | English README and v0.1.0 |
| `test_installation_documents_supported_aes_sync` | supported installation |
| `test_readme_distinguishes_core_and_optional_components` | core/optional assets |
| `test_privacy_is_default` | default privacy |
| `test_mit_license_is_present_and_complete` | complete MIT license |
| `test_distribution_excludes_pilot_plans_and_macos_artifacts` | exclusions |
| `test_remote_capabilities_are_marked_experimental` | experimental remote capabilities |
| `test_distribution_content_contains_no_personal_paths_or_tokens` | security audit and generic-path distinction |
| `test_distribution_content_contains_no_consumer_names` | consumer and plan names |
| `test_public_artifacts_are_regular_files` | no symlink indirection |

The evaluation is intentionally RED until the real repository contains the
public artifacts and satisfies this contract.

## Acceptance criteria

- [ ] The root README is in English and references v0.1.0.
- [ ] Installation contains one multiline aes-sync code fence with `--source`
  repeated for the real `--check`, and distinguishes core and optional assets.
- [ ] Privacy states that content and telemetry are not sent by default.
- [ ] Root `LICENSE` matches the complete MIT text byte for byte and has one
  final newline.
- [ ] No pilot plans or macOS artifacts appear in tracked paths or text.
- [ ] Every remote execution, queue, or worker reference is accompanied by an
  Experimental section with a line or sentence mentioning remote and
  experimental.
- [ ] The audit detects no personal paths, tokens, private keys, or consumer
  names in distributed files.
- [ ] README and LICENSE are not symlinks.
- [ ] `python3 -m pytest --collect-only -q evals/oss-release-contract` collects
  all ten cases without errors.

## HITL

None for this contract and evaluation. Publishing the actual release requires
a separate human decision; the declared copyright owner is part of the license
contract.
