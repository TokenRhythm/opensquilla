"""Content classification for unified-diff patches.

Used by endgame policies that must distinguish diagnostic instrumentation
(added print/log statements) from substantive changes: the endgame git freeze
may allow reverting instrumentation-only diffs, and final-diff salvage must
not resurrect them into a collected patch. Classification is deliberately
conservative — anything it cannot positively identify as instrumentation
counts as a substantive change, so misreads fail toward keeping protections
active.
"""

from __future__ import annotations

import re

# One line of diagnostic output in the common ecosystems: stdout/stderr print
# calls, logging-framework calls, and debugger statements. Matched against
# added lines with leading whitespace stripped. Multi-line calls only match on
# their first line; continuation lines fail the match and classify the patch
# as substantive — the conservative direction.
_INSTRUMENTATION_LINE_RE = re.compile(
    r"""^(?:
        print\s*\( | pprint\s*\( |
        sys\.(?:stdout|stderr)\.write\s*\( |
        traceback\.print[a-z_]*\s*\( |
        (?:logging|logger|log)\.
            (?:debug|info|warn|warning|error|exception|critical|trace|log)\s*\( |
        console\.(?:log|error|warn|info|debug|trace|dir)\s*\( |
        process\.(?:stdout|stderr)\.write\s*\( |
        fmt\.[A-Za-z]*[Pp]rint[A-Za-z]*\s*\( |
        log\.(?:Print|Println|Printf)\s*\( |
        puts\s+["'] |
        println!\s*\( | eprintln!\s*\( | print!\s*\( | eprint!\s*\( | dbg!\s*\( |
        System\.(?:out|err)\.print[A-Za-z]*\s*\( |
        [A-Za-z_][A-Za-z0-9_]*\.printStackTrace\s*\( |
        (?:std::)?(?:cout|cerr)\s*<< |
        printf\s*\( | fprintf\s*\( | puts\s*\( | perror\s*\( |
        var_dump\s*\( | print_r\s*\( | error_log\s*\( |
        \$stderr\.puts\b | \$stdout\.puts\b |
        debugger\b
    )""",
    re.VERBOSE,
)


def iter_patch_line_changes(patch: str) -> tuple[list[str], list[str]]:
    """Split a unified diff into (added, removed) content lines.

    File headers (``+++``/``---``), hunk headers, and context lines are
    excluded; the leading ``+``/``-`` marker is stripped from the returned
    lines.
    """

    added: list[str] = []
    removed: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def is_instrumentation_only_patch(patch: str) -> bool:
    """Whether a unified diff only adds diagnostic print/log lines.

    True requires: at least one added non-blank line, every added non-blank
    line matching the instrumentation patterns, and no removed lines at all —
    any deletion means existing behavior changed, which is never
    instrumentation-only.
    """

    if not patch or not patch.strip():
        return False
    added, removed = iter_patch_line_changes(patch)
    if removed:
        return False
    content_lines = [line.strip() for line in added if line.strip()]
    if not content_lines:
        return False
    return all(_INSTRUMENTATION_LINE_RE.match(line) for line in content_lines)
