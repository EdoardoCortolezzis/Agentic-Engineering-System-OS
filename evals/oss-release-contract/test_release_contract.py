"""Deterministic evaluation of the OSS v0.1.0 release contract.

The evaluation is RED until the repository root contains the public artifacts.
``AES_REPO_ROOT`` is a test-only override for an isolated fixture repository.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


REPO = Path(os.environ.get("AES_REPO_ROOT", Path(__file__).resolve().parents[2]))
README = REPO / "README.md"
LICENSE = REPO / "LICENSE"
_AUDIT_EXCLUSIONS = {
    "evals/oss-release-contract/test_release_contract.py",
    "specs/008-oss-release-contract/spec.md",
}

MIT_LICENSE = """MIT License

Copyright (c) 2026 Edoardo Cortolezzis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


def _readme() -> str:
    assert README.is_file() and not README.is_symlink(), (
        "README.md must exist at the repository root as a regular file."
    )
    return README.read_text(encoding="utf-8")


def _tracked_paths() -> list[Path]:
    assert REPO.is_dir(), f"Repository root does not exist: {REPO}"
    result = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    paths = [Path(item) for item in result.stdout.decode().split("\0") if item]
    assert paths, "The repository must contain tracked files for the audit."
    return paths


def _distribution_files() -> list[Path]:
    return [REPO / relative for relative in _tracked_paths()]


def _text_distribution_files() -> list[Path]:
    return [
        path
        for path in _distribution_files()
        if path.relative_to(REPO).as_posix() not in _AUDIT_EXCLUSIONS
    ]


def _markdown_section(source: str, heading: str) -> str | None:
    """Return one level-two Markdown section without its heading."""
    match = re.search(
        rf"(?ims)^##\s+{re.escape(heading)}\b(?P<section>.*?)(?=^##\s+|\Z)",
        source,
    )
    return match.group("section") if match else None


_CODE_FENCE = re.compile(r"(?ms)^ {0,3}```[^\n]*\n(?P<body>.*?)^ {0,3}```[ \t]*$")


def _without_code_fences(source: str) -> str:
    """Remove fenced code blocks before evaluating prose-level requirements."""
    return _CODE_FENCE.sub("\n", source)


def _sentences(source: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", source.replace("\n", " "))


def test_public_readme_is_english_and_versioned() -> None:
    """The root README exposes an English v0.1.0 public entry point."""
    source = _readme()
    assert re.search(r"(?im)^#\s+.+\bv0\.1\.0\b", source), (
        "README.md must have an English release headline containing v0.1.0."
    )
    for heading in ("Installation", "Components", "Privacy"):
        assert re.search(rf"(?im)^##\s+{heading}\b", source), (
            f"README.md must have an English '{heading}' section."
        )
    for phrase in ("This release", "Install with", "By default"):
        assert re.search(rf"(?i)\b{re.escape(phrase)}\b", source), (
            f"README.md must contain the essential English phrase '{phrase}'."
        )
    assert not re.search(r"(?im)^#+\s+(Installazione|Componenti|Privacy predefinita)\b", source)


def test_installation_documents_supported_aes_sync() -> None:
    """One Installation code fence contains the real install/check sequence."""
    source = _readme()
    installation = _markdown_section(source, "Installation")
    assert installation is not None, "README.md must contain an Installation section."
    fences = [match.group("body") for match in _CODE_FENCE.finditer(installation)]
    assert fences, "Installation must contain a fenced command block."

    for block in fences:
        commands = re.sub(r"\\[ \t]*\n", " ", block)
        segments = re.split(r"&&|\|\||;|\n", commands)
        source_seen = False
        for segment in segments:
            has_script = re.search(r"(?i)\b(?:[\w./-]+/)?aes-sync\.sh\b", segment)
            has_source = re.search(r"(?i)(?<!\w)--source\s+[^\s`]+", segment)
            has_check = re.search(r"(?i)(?<!\w)--check\b", segment)
            if has_script and has_source:
                source_seen = True
            if source_seen and has_check and (has_script or has_source):
                return

    assert False, (
        "One Installation code fence must contain aes-sync --source followed by "
        "the real aes-sync --check command."
    )


def test_readme_distinguishes_core_and_optional_components() -> None:
    """The Components section distinguishes core from optional assets."""
    source = _readme()
    components = re.search(
        r"(?ims)^##\s+Components\b(?P<section>.*?)(?=^##\s+|\Z)", source
    )
    assert components, "README.md must contain a Components section."
    section = components.group("section")
    assert re.search(r"(?i)\bcore\b", section), "Components must identify core assets."
    assert re.search(r"(?i)\boptional\b", section), (
        "Components must identify optional assets."
    )


def test_privacy_is_default() -> None:
    """The Privacy section explicitly disables content and telemetry by default."""
    source = _readme()
    section = re.search(r"(?ims)^##\s+Privacy\b(?P<section>.*?)(?=^##\s+|\Z)", source)
    assert section, "README.md must contain a Privacy section."
    text = section.group("section").replace("\n", " ")
    assert re.search(r"(?i)\bby default\b", text), (
        "The Privacy section must state the default behavior."
    )
    for term in ("telemetry", "content"):
        assert re.search(rf"(?i)\b{term}\b", text), (
            f"The default privacy statement must cover {term}."
        )
        assert re.search(
            rf"(?i)(?:no|without|never|disabled|off|not sent|does not send)"
            rf"[^.!?;]{{0,40}}\b{term}\b|\b{term}\b[^.!?;]{{0,40}}"
            rf"(?:(?:is|are)\s+)?(?:not sent|is disabled|never sent|disabled by default)",
            text,
        ), f"The default privacy statement must explicitly exclude {term}."


def test_mit_license_is_present_and_complete() -> None:
    """LICENSE matches the complete MIT text and declared copyright owner."""
    assert LICENSE.is_file() and not LICENSE.is_symlink(), (
        "LICENSE must exist at the repository root as a regular file."
    )
    assert LICENSE.read_bytes() == f"{MIT_LICENSE}\n".encode("utf-8"), (
        "LICENSE must match the complete MIT license text byte-for-byte, "
        "including exactly one final newline."
    )


def test_distribution_excludes_pilot_plans_and_macos_artifacts() -> None:
    """Tracked paths and text exclude pilot plans and macOS metadata."""
    forbidden_path_terms = ("pilot", "pilota", "__macosx")
    forbidden_names = {".ds_store", ".localized", ".com.apple.timemachine.donotpresent"}
    pilot_plan_content = re.compile(
        r"(?ix)\b(?:"
        r"pilot[- ](?:plans?|projects?|features?|phase)|"
        r"pian[oi](?:\s+di)?\s+(?:progett[oi]\s+)?pilot[ae]|"
        r"progett[oi]\s+pilot[ae]"
        r")\b"
    )
    for path in _distribution_files():
        relative = path.relative_to(REPO)
        lowered_parts = {part.casefold() for part in relative.parts}
        assert not any(
            term in part for part in lowered_parts for term in forbidden_path_terms
        ), (
            f"Forbidden pilot or macOS path is tracked: {relative}"
        )
        assert path.name.casefold() not in forbidden_names, (
            f"Forbidden macOS metadata is tracked: {relative}"
        )
        assert not any(part.endswith(".localized") for part in lowered_parts), (
            f"Localized macOS metadata is tracked: {relative}"
        )
        assert not path.name.startswith("._"), f"AppleDouble file is tracked: {relative}"

    for path in _text_distribution_files():
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert not pilot_plan_content.search(content), (
            f"Pilot plan content found in tracked distribution file: {path}"
        )


def test_remote_capabilities_are_marked_experimental() -> None:
    """Remote execution, queue, or worker prose is labeled Experimental."""
    source = _readme()
    experimental = _markdown_section(source, "Experimental")
    assert experimental is not None, "README.md must contain an Experimental section."

    prose = _without_code_fences(source)
    remote_reference = re.search(
        r"(?i)\bremote\s+(?:executions?|queues?|workers?)\b|"
        r"\b(?:executions?|queues?|workers?)\s+(?:is\s+)?remote\b",
        prose,
    )
    if not remote_reference:
        return

    experimental_prose = _without_code_fences(experimental)
    without_headings = re.sub(r"(?m)^\s*#{1,6}[^\n]*\n?", "", experimental_prose)
    candidates = [line for line in without_headings.splitlines() if line.strip()]
    candidates.extend(_sentences(without_headings))
    assert any(
        re.search(r"(?i)\bremote\b", candidate)
        and re.search(r"(?i)\bexperimental\b", candidate)
        for candidate in candidates
    ), (
        "The Experimental section must contain a Markdown line or sentence "
        "mentioning remote and experimental together."
    )


def test_distribution_content_contains_no_personal_paths_or_tokens() -> None:
    """Textual distribution files contain no concrete local paths, tokens, or keys."""
    personal_path = re.compile(
        r"(?:^|[\s(`\"'=]|file://)"
        r"(?:/(?:Users|home|Volumes|private/var/folders)/[^\s'\"`)>]+|"
        r"~[A-Za-z0-9._-]+[/\\][^\s'\"`)>]+|"
        r"[A-Za-z]:[\\/](?:Users|home)[\\/][^\s'\"`)>]+)"
    )
    token = re.compile(
        r"(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|"
        r"ASIA[0-9A-Z]{16}|glpat-[A-Za-z0-9_-]{20,}|"
        r"npm_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|"
        r"sk_live_[A-Za-z0-9]{16,}|"
        r"xox[baprs]-[A-Za-z0-9-]{20,}|Bearer\s+[A-Za-z0-9._~+/=-]{20,}|"
        # YAML-style values may be unquoted. For shell/env assignments, the
        # generic lower-case names deliberately require `name=value` with no
        # whitespace around `=`: `token = ...` is commonly an identifier in a
        # test fixture, not a credential declaration. Upper-case environment
        # names remain covered with optional spacing around the separator.
        r"(?:api[_-]?key|access[_-]?token|secret|token)\s*:\s*['\"]?"
        r"[A-Za-z0-9._~+/=-]{20,}['\"]?|"
        r"(?:api[_-]?key|access[_-]?token|secret|token)="
        r"['\"]?[A-Za-z0-9._~+/=-]{20,}['\"]?|"
        # Keep environment assignments on one line: ``\s`` also matches
        # newlines and could join an empty assignment to the next variable.
        r"(?:[A-Z][A-Z0-9_]*_)?(?:TOKEN|KEY|SECRET)[ \t]*[:=][ \t]*['\"]?"
        r"[A-Za-z0-9._~+/=-]{20,}['\"]?|"
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
    )
    # ``~/...`` is a generic home-relative configuration form. A concrete
    # username after ``~`` (or an absolute machine path) remains forbidden.
    assert not personal_path.search("CODEX_HOME=~/.codex")
    for concrete in (
        "/Users/edo/.codex",
        "/home/alice/.codex",
        "/Volumes/PortableSSD/project",
        "~edo/.codex",
        r"C:\Users\Alice\.codex",
    ):
        assert personal_path.search(concrete), f"Concrete path not detected: {concrete}"
    assert token.search("access_token: abcdefghijklmnopqrstuvwxyz")
    assert token.search("access_token=abcdefghijklmnopqrstuvwxyz")
    assert token.search("TOKEN=abcdefghijklmnopqrstuvwxyz")
    assert token.search("GH_TOKEN=abcdefghijklmnopqrstuvwxyz")
    assert not token.search("GH_TOKEN=\nAES_PRODUCT_MANAGER=")
    assert not token.search("token = TEST_FIXTURE_IDENTIFIER_VALUE")
    for path in _text_distribution_files():
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert not personal_path.search(source), f"Personal path found in {path}."
        assert not token.search(source), f"Token or private key found in {path}."


def test_distribution_content_contains_no_consumer_names() -> None:
    """Textual distribution files contain no pilot consumer names."""
    consumer_names = ("AutoClip", "AutoSocial", "LeggiITA")
    for path in _text_distribution_files():
        relative = path.relative_to(REPO).as_posix().casefold()
        content = path.read_bytes().lower()
        for name in consumer_names:
            encoded = name.casefold().encode("ascii")
            assert name.casefold() not in relative, (
                f"Consumer name found in distribution path: {path}."
            )
            assert encoded not in content, (
                f"Consumer name found in distribution content: {path}."
            )


def test_public_artifacts_are_regular_files() -> None:
    """README and LICENSE are present at the root without symlink indirection."""
    for artifact in (README, LICENSE):
        assert artifact.exists(), f"Public artifact is missing: {artifact.name}."
        assert artifact.is_file() and not artifact.is_symlink(), (
            f"Public artifact must be a regular file, not a symlink: {artifact.name}."
        )
