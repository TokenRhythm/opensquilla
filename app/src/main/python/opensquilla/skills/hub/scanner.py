"""Security scanner for SKILL.md files before installation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# Patterns that indicate prompt injection attempts
_PROMPT_INJECTION = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"override\s+(all\s+)?instructions", re.I),
    re.compile(r"you\s+are\s+now\s+(a\s+)?new\s+ai", re.I),
    re.compile(r"disregard\s+(all\s+)?(prior|previous)", re.I),
    re.compile(r"forget\s+(all\s+)?rules", re.I),
    re.compile(r"system\s*:\s*you\s+are", re.I),
]

# Patterns that indicate shell injection
_SHELL_INJECTION = [
    re.compile(r"\$\("),  # $(command)
    re.compile(r"`[^`]*\$\([^)]+\)[^`]*`"),  # backtick with subshell: `$(cmd)`
]

# Patterns that indicate data exfiltration
_EXFILTRATION = [
    re.compile(r"\b(curl|wget|nc|ncat)\s+['\"]?https?://(?!localhost|127\.0\.0\.1)", re.I),
    re.compile(r"\bfetch\s*\(\s*['\"]https?://(?!localhost|127\.0\.0\.1)", re.I),
]

# Hidden unicode patterns
_HIDDEN_UNICODE = [
    re.compile(r"[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff]"),  # zero-width chars
    re.compile(r"[\u202a-\u202e]"),  # directional overrides
]


@dataclass
class ScanFinding:
    """A single security finding."""

    category: str  # "prompt_injection" | "shell_injection" | "exfiltration" | "hidden_unicode"
    severity: str  # "warning" | "dangerous"
    line: int
    text: str
    pattern: str


@dataclass
class ScanResult:
    """Result of scanning a skill."""

    verdict: str = "safe"  # "safe" | "warning" | "dangerous"
    findings: list[ScanFinding] = field(default_factory=list)
    strategy: str = "skill-md-v1"


def _strip_code_blocks(text: str) -> str:
    """Replace fenced code blocks with blank lines to preserve line numbering."""

    def _replace_with_blanks(m: re.Match[str]) -> str:
        return "\n" * m.group(0).count("\n")

    return re.sub(r"```[\s\S]*?```", _replace_with_blanks, text)


def scan_skill(skill_md_content: str) -> ScanResult:
    """Scan a SKILL.md file for security concerns.

    Returns a ScanResult with verdict and findings.
    Code blocks are excluded from shell/exfiltration checks
    (shell commands inside code examples are expected).
    """
    findings: list[ScanFinding] = []
    lines = skill_md_content.split("\n")
    stripped = _strip_code_blocks(skill_md_content)
    stripped_lines = stripped.split("\n")

    # Check prompt injection (full text — these are dangerous anywhere)
    for i, line in enumerate(lines, 1):
        for pat in _PROMPT_INJECTION:
            if pat.search(line):
                findings.append(
                    ScanFinding(
                        category="prompt_injection",
                        severity="dangerous",
                        line=i,
                        text=line.strip()[:100],
                        pattern=pat.pattern,
                    )
                )

    # Check shell injection (outside code blocks only)
    for i, line in enumerate(stripped_lines, 1):
        for pat in _SHELL_INJECTION:
            if pat.search(line):
                findings.append(
                    ScanFinding(
                        category="shell_injection",
                        severity="warning",
                        line=i,
                        text=line.strip()[:100],
                        pattern=pat.pattern,
                    )
                )

    # Check exfiltration (outside code blocks only)
    for i, line in enumerate(stripped_lines, 1):
        for pat in _EXFILTRATION:
            if pat.search(line):
                findings.append(
                    ScanFinding(
                        category="exfiltration",
                        severity="dangerous",
                        line=i,
                        text=line.strip()[:100],
                        pattern=pat.pattern,
                    )
                )

    # Check hidden unicode (full text)
    for i, line in enumerate(lines, 1):
        for pat in _HIDDEN_UNICODE:
            if pat.search(line):
                findings.append(
                    ScanFinding(
                        category="hidden_unicode",
                        severity="dangerous",
                        line=i,
                        text=repr(line.strip()[:80]),
                        pattern=pat.pattern,
                    )
                )

    # Determine verdict
    if any(f.severity == "dangerous" for f in findings):
        verdict = "dangerous"
    elif findings:
        verdict = "warning"
    else:
        verdict = "safe"

    return ScanResult(verdict=verdict, findings=findings, strategy="skill-md-v1")


def scan_skill_bundle(files: Mapping[str, str | bytes]) -> ScanResult:
    """Scan an install bundle, including text sidecars and binary inventory.

    ``scan_skill`` remains the SKILL.md scanner. This bundle-level wrapper keeps
    the installer verdict honest when a package contains additional files.
    Binary files are not inspected, so they become warning findings instead of
    allowing the bundle to be reported as fully safe.
    """
    findings: list[ScanFinding] = []
    for rel_path, content in sorted(files.items()):
        if isinstance(content, bytes):
            findings.append(
                ScanFinding(
                    category="unscanned_binary",
                    severity="warning",
                    line=0,
                    text=rel_path[:100],
                    pattern="binary file not scanned",
                )
            )
            continue

        result = scan_skill(content)
        for finding in result.findings:
            findings.append(
                ScanFinding(
                    category=finding.category,
                    severity=finding.severity,
                    line=finding.line,
                    text=f"{rel_path}: {finding.text}"[:100],
                    pattern=finding.pattern,
                )
            )

    if any(f.severity == "dangerous" for f in findings):
        verdict = "dangerous"
    elif findings:
        verdict = "warning"
    else:
        verdict = "safe"
    return ScanResult(verdict=verdict, findings=findings, strategy="bundle-v1")
