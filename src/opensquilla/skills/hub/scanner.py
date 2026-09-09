"""Security scanner for SKILL.md files before installation."""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from opensquilla.skills.io_worker import check_staging_cancelled

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
    total_findings: int = 0
    truncated: bool = False


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


_SAMPLE_LIMIT = 100


def _text_chunks(path: Path) -> Iterator[str]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    with path.open("rb") as handle:
        while raw := handle.read(64 * 1024):
            check_staging_cancelled()
            yield decoder.decode(raw)
    yield decoder.decode(b"", final=True)


def _fence_parts(path: Path) -> Iterator[tuple[bool, str]]:
    pending = ""
    for chunk in _text_chunks(path):
        pending += chunk
        while (index := pending.find("```")) >= 0:
            yield False, pending[:index]
            yield True, "```"
            pending = pending[index + 3 :]
        if len(pending) > 2:
            yield False, pending[:-2]
            pending = pending[-2:]
    if pending:
        yield False, pending


class _LineScan:
    """Bounded line matching, including arbitrarily long whitespace runs."""

    def __init__(self, groups: list[tuple[str, str, list[re.Pattern[str]]]]) -> None:
        self.patterns = [
            (category, severity, pattern)
            for category, severity, patterns in groups
            for pattern in patterns
        ]
        self.samples: list[ScanFinding] = []
        self.count = 0
        self.verdict = "safe"
        self.line = 1
        self.window = ""
        self.prefix = ""
        self.matched: set[int] = set()
        self.in_backtick = False
        self.subshell = 0
        self.subshell_complete = False
        self.shell_tail = ""

    def feed(self, text: str) -> None:
        parts = text.split("\n")
        for index, part in enumerate(parts):
            if index:
                self.finish_line()
            if len(self.prefix) < 100:
                sample = part if self.prefix else part.lstrip()
                self.prefix += sample[: 100 - len(self.prefix)]
            for number, (category, _severity, pattern) in enumerate(self.patterns):
                if category == "hidden_unicode" and pattern.search(part):
                    self.matched.add(number)
            # Every unbounded run in the word patterns is whitespace. Collapse
            # those runs before retaining overlap, without altering line boundaries.
            normalized = re.sub(r"[^\S\n]+", " ", part)
            if self.window.endswith(" "):
                normalized = normalized.lstrip(" ")
            for offset in range(0, len(normalized), 2048):
                self.window += normalized[offset : offset + 2048]
                if len(self.window) > 512:
                    self.match(final=False)
                    self.window = self.window[-256:]
            self.backticks(part)

    def backticks(self, text: str) -> None:
        # The second shell pattern has an unbounded backtick body. Track its
        # delimiters explicitly instead of retaining that body in the overlap.
        if not any(category == "shell_injection" for category, _, _ in self.patterns):
            return
        text = self.shell_tail + text
        self.shell_tail = "$" if text.endswith("$") else ""
        if self.shell_tail:
            text = text[:-1]
        for token in re.finditer(r"`|\$\(|\)|[^`$)]+|\$(?!\()", text):
            value = token.group()
            if value == "`":
                if self.in_backtick and self.subshell_complete:
                    for number, (category, _, pattern) in enumerate(self.patterns):
                        if category == "shell_injection" and pattern is _SHELL_INJECTION[1]:
                            self.matched.add(number)
                self.in_backtick = not self.in_backtick
                self.subshell = 0
                self.subshell_complete = False
            elif self.in_backtick:
                if value == "$(":
                    if self.subshell:
                        self.subshell = 2
                    else:
                        self.subshell = 1
                elif value == ")":
                    if self.subshell == 2:
                        self.subshell_complete = True
                    self.subshell = 0
                elif self.subshell:
                    self.subshell = 2

    def match(self, *, final: bool) -> None:
        for number, (category, _, pattern) in enumerate(self.patterns):
            if number in self.matched or category == "hidden_unicode":
                continue
            if pattern is _SHELL_INJECTION[1]:
                continue
            for match in pattern.finditer(self.window):
                # Leave enough lookahead for the localhost exclusion and enough
                # overlap for a word that spans two transport chunks.
                if final or match.end() <= len(self.window) - 128:
                    self.matched.add(number)
                    break

    def finish_line(self) -> None:
        self.match(final=True)
        for number in sorted(self.matched):
            category, severity, pattern = self.patterns[number]
            self.count += 1
            if severity == "dangerous" or self.verdict == "safe":
                self.verdict = severity
            if len(self.samples) < _SAMPLE_LIMIT:
                text = (
                    repr(self.prefix.strip()[:80])
                    if category == "hidden_unicode"
                    else self.prefix.strip()
                )
                self.samples.append(
                    ScanFinding(category, severity, self.line, text, pattern.pattern)
                )
        self.line += 1
        self.window = self.prefix = self.shell_tail = ""
        self.matched.clear()
        self.in_backtick = self.subshell_complete = False
        self.subshell = 0


def scan_skill_tree(directory: Path) -> ScanResult:
    """Scan every byte with bounded buffers and a bounded diagnostic sample."""
    result = ScanResult(strategy="bundle-v1")
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Cannot scan a symbolic link in Skill staging")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        # Validate all UTF-8 and count paired fences before interpreting examples.
        # An unmatched opening fence remains ordinary text, as in the old scanner.
        try:
            fences = sum(is_fence for is_fence, _ in _fence_parts(path))
        except UnicodeDecodeError:
            result.total_findings += 1
            if result.verdict == "safe":
                result.verdict = "warning"
            if len(result.findings) < _SAMPLE_LIMIT:
                result.findings.append(
                    ScanFinding(
                        "unscanned_binary",
                        "warning",
                        0,
                        relative[:100],
                        "binary file not scanned",
                    )
                )
            continue
        full = _LineScan(
            [
                ("prompt_injection", "dangerous", _PROMPT_INJECTION),
                ("hidden_unicode", "dangerous", _HIDDEN_UNICODE),
            ]
        )
        outside = _LineScan(
            [
                ("shell_injection", "warning", _SHELL_INJECTION),
                ("exfiltration", "dangerous", _EXFILTRATION),
            ]
        )
        paired_fences = fences - fences % 2
        inside = False
        for is_fence, text in _fence_parts(path):
            full.feed(text)
            if is_fence and paired_fences:
                inside = not inside
                paired_fences -= 1
            elif inside:
                outside.feed("\n" * text.count("\n"))
            else:
                outside.feed(text)
        full.finish_line()
        outside.finish_line()
        for scan in (full, outside):
            result.total_findings += scan.count
            if scan.verdict == "dangerous" or result.verdict == "safe":
                result.verdict = scan.verdict
        order = {
            name: index
            for index, name in enumerate(
                ("prompt_injection", "shell_injection", "exfiltration", "hidden_unicode"),
            )
        }
        for finding in sorted(
            full.samples + outside.samples, key=lambda f: (order[f.category], f.line)
        ):
            if len(result.findings) >= _SAMPLE_LIMIT:
                break
            finding.text = f"{relative}: {finding.text}"[:100]
            result.findings.append(finding)
    result.truncated = result.total_findings > len(result.findings)
    return result
