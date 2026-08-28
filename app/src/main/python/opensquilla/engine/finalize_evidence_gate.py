"""Finalize-time red-evidence gate for coding-agent runs.

Challenges a finalization attempt that contradicts the run's own dynamic
evidence: the model is about to finish with a non-empty workspace diff while
(a) the latest execution-level command after its final source edit is red,
(b) a red command was later made green only by excluding/deselecting the
failing case, (c) most post-final-edit executions are red, (d) no
execution-level command ran after its final source edit, or (e) a
self-written reproduction/diagnostic script never passed (still red, or
deleted before a passing run was observed).

Strict mode (``OPENSQUILLA_FINALIZE_EVIDENCE_STRICT``) adds one finalize-time
trigger on top of the same machinery: (f) ``zero_verification`` — no
execution-level command survived skip filtering at any point in the run, so
the change being shipped was never exercised at all. Red-first bookkeeping
(``red_first_satisfied`` / candidate counts, fed by the same deselection
profile machinery) is still tracked and reported for offline replay
diagnostics, but it does NOT gate: transcript replay over real runs showed
that "never observed failing then passing" is common on legitimately solved
runs (direct fix + existing suite green), so a trigger on it would challenge
correct finishes far too often.

Polarity contract: a failing self-written reproduction is BINDING evidence and
green results from unrelated suites do not override it; the challenge never
demands patch minimality, never devalues self-written repros, fires only at
finalize-time, and never blocks submission (bounded challenges, then the run
finishes normally).

The tracker is a pure, I/O-free state machine so the exact same semantics run
inside the live agent loop and in offline transcript replay
(``scripts/experiments/replay_finalize_gate.py``).
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from opensquilla.execution_status import execution_status_for_tool_result

FINALIZE_EVIDENCE_GATE_CHALLENGE_LIMIT = 2

# Minimum red executions after the final edit for the red-majority trigger:
# small windows red-majority trivially (1 red of 1 run) and are already
# covered by the last-execution-red trigger.
RED_MAJORITY_MIN_COUNT = 5

_ERROR_STATUSES = frozenset({"error", "timeout", "cancelled"})

# Tools whose successful calls mutate files. ``apply_patch`` may carry no
# single path argument; callers pass path=None and it counts as a source edit.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "create_source",
        "edit_source",
        "apply_patch",
        "write_scratch",
    }
)
EXECUTION_TOOL_NAMES: frozenset[str] = frozenset(
    {"background_process", "exec_command", "execute_code"}
)

# Command heads that carry no verification signal. Anything NOT in this set is
# treated as execution-level ("could have verified the patch"), which errs
# toward suppressing the gate rather than firing it: an unknown green command
# after the final edit counts as verification.
_INSPECTION_COMMAND_HEADS: frozenset[str] = frozenset(
    {
        "cat",
        "tac",
        "head",
        "tail",
        "less",
        "more",
        "ls",
        "dir",
        "find",
        "fd",
        "grep",
        "egrep",
        "fgrep",
        "zgrep",
        "rg",
        "ag",
        "ack",
        "sed",
        "awk",
        "cut",
        "tr",
        "sort",
        "uniq",
        "wc",
        "nl",
        "paste",
        "column",
        "jq",
        "yq",
        "echo",
        "printf",
        "pwd",
        "cd",
        "which",
        "whereis",
        "type",
        "file",
        "stat",
        "du",
        "df",
        "tree",
        "diff",
        "cmp",
        "comm",
        "md5sum",
        "sha1sum",
        "sha256sum",
        "printenv",
        "export",
        "set",
        "unset",
        "hexdump",
        "xxd",
        "od",
        "strings",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "date",
        "whoami",
        "id",
        "uname",
        "hostname",
        "nproc",
        "sleep",
        "true",
        "false",
        "test",
        "git",
        "mkdir",
        "rmdir",
        "touch",
        "cp",
        "mv",
        "rm",
        "chmod",
        "chown",
        "ln",
        "tar",
        "gzip",
        "gunzip",
        "zip",
        "unzip",
        "man",
        "help",
        "info",
        "history",
        "alias",
    }
)
_WRAPPER_COMMAND_HEADS: frozenset[str] = frozenset(
    {"sudo", "time", "timeout", "nice", "stdbuf", "nohup", "command", "exec", "env"}
)
# Lint/style detectors whose exit code 1 means "findings in the input", not
# "the command failed". In detector-repo issues (rubocop, ruff, ...) a
# findings exit is frequently the EXPECTED pass state of a scratch data file,
# so exit 1 from an all-detector command carries no red polarity. Exit >= 2
# (real tool error) stays red.
_DETECTOR_COMMAND_HEADS: frozenset[str] = frozenset(
    {
        "rubocop",
        "ruff",
        "flake8",
        "pylint",
        "pycodestyle",
        "pyflakes",
        "pydocstyle",
        "bandit",
        "mypy",
        "pyright",
        "eslint",
        "jshint",
        "standard",
        "stylelint",
        "prettier",
        "shellcheck",
        "phpcs",
        "php-cs-fixer",
        "phpstan",
        "psalm",
        "golangci-lint",
        "staticcheck",
        "credo",
        "hlint",
        "clang-tidy",
        "cppcheck",
        "checkstyle",
        "ktlint",
        "detekt",
    }
)
# Exit codes that indicate the command itself was malformed (not found /
# not executable), not that a verification failed. A one-off typo the model
# immediately abandons must not stay "outstanding" forever.
_COMMAND_FORM_ERROR_EXITS: frozenset[int] = frozenset({126, 127})
# Execution-status reasons meaning the command never actually ran (policy /
# approval denials, harness-internal tool errors, cancellations, harness
# blocks minted with "The tool was not run") or produced no outcome yet (a
# background launch still running, an approval still pending). These carry no
# verification signal in either polarity. Genuine failures keep
# "nonzero_exit" / "masked_pipeline_failure" / a timeout reason and must
# stay red.
_NON_VERIFICATION_STATUS_REASONS: frozenset[str] = frozenset(
    {
        "denied",
        "approval_denied",
        "runtime_error",
        "cancelled",
        # Harness-level blocks: the call was refused before dispatch.
        "provider_context_projection_reused",
        "projected_diagnostic_requires_retrieval",
        "tool_failure_loop_exhausted",
        "tool_run_budget_exhausted",
        "invalid_tool_arguments",
        # No outcome yet: launched or awaiting approval, not a result.
        "background_running",
        "approval_pending",
    }
)
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TIMEOUT_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")

# Self-written reproduction/diagnostic artifacts. Registration happens only on
# observed agent writes, so repository files never enter the registry unless
# the agent itself (re)wrote them. Markers mirror
# ``final_diff_contract._SCRATCH_PATH_PATTERNS`` but match exact stem tokens
# only: real source files whose names merely CONTAIN a marker substring
# (``preprocessing.py``, ``inspectdb.py``, ``DebugOverlaps.java``) must count
# as source edits, not scratch artifacts.
_REPRO_STEM_MARKERS: frozenset[str] = frozenset(
    {
        "debug",
        "repro",
        "reproduce",
        "reproduction",
        "scratch",
        "verify",
        "inspect",
        "investigate",
        "poc",
    }
)
_STEM_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_SCRIPT_EXTENSION_RE = re.compile(
    r"\.(py|js|mjs|cjs|ts|rb|php|sh|bash|zsh|pl|go|rs|java|c|cc|cpp|exp|tcl)$",
    re.I,
)
_SCRATCH_DIR_MARKERS: tuple[str, ...] = ("/squilla-scratch/", "/tmp/", "/var/tmp/")

# Heredoc operator with a letter-led delimiter (EOF, RUBY, PHP, ...). The
# letter requirement keeps bit-shift expressions like ``1 << 2`` inside
# inline interpreter code from being misread as heredocs.
_HEREDOC_RE = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_]\w*)\1")
# Interpreters whose first positional argument names the real program.
_INTERPRETER_HEADS: frozenset[str] = frozenset(
    {"python", "python2", "python3", "ruby", "php", "node", "nodejs", "perl", "sh", "bash", "zsh"}
)
# Flags whose values narrow which tests/cases a runner executes.
_NARROWING_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-k", "-run", "--run", "--filter", "--grep", "-g", "-t", "--tests"}
)
_NARROWING_MARKER_PREFIXES: tuple[str, ...] = (
    "--ignore",
    "--exclude",
    "--deselect",
    "--skip",
)


def _basename_marks_repro(basename: str) -> bool:
    if not _SCRIPT_EXTENSION_RE.search(basename):
        return False
    stem = basename.rsplit(".", 1)[0].lower()
    tokens = [token for token in _STEM_TOKEN_SPLIT_RE.split(stem) if token]
    return any(token in _REPRO_STEM_MARKERS for token in tokens)


def is_repro_script_path(path: str) -> bool:
    """Return whether *path* has an executable repro-script extension."""

    normalized = str(path or "").strip().replace("\\", "/")
    return bool(normalized and _SCRIPT_EXTENSION_RE.search(normalized))


def looks_repro_artifact_path(path: str) -> bool:
    """True when a written path looks like a self-written repro/diagnostic script.

    Outside the scratch dirs, only shallow paths qualify: root-level files
    (``repro.py``, ``debug_issue.sh``) or absolute paths directly under one
    directory (``/testbed/reproduce.py``). Nested repository paths
    (``app/views/debug.py``) are always source edits — misclassifying a
    real fix as a scratch artifact would silently disarm the gate for the
    whole run.
    """

    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    lowered = normalized.lower()
    for marker in _SCRATCH_DIR_MARKERS:
        if marker in f"/{lowered}" or lowered.startswith(marker.lstrip("/")):
            return bool(_SCRIPT_EXTENSION_RE.search(lowered))
    relative = normalized[2:] if normalized.startswith("./") else normalized.lstrip("/")
    max_depth = 1 if normalized.startswith("/") else 0
    if relative.count("/") > max_depth:
        return False
    return _basename_marks_repro(relative.rsplit("/", 1)[-1])


def _is_nonsource_scratch_note(normalized_path: str) -> bool:
    """True for non-script files under the scratch dirs (notes, summaries).

    Script-extension scratch files are repro artifacts (handled by
    ``looks_repro_artifact_path``); anything else under a scratch dir —
    ``/tmp/squilla-scratch/FIX_SUMMARY.md``, ``/tmp/notes.txt`` — is a note
    the agent wrote for itself, never a source edit, so it must not reset
    final-source-edit tracking. The generic temp roots only count as true
    absolute prefixes: a repository file under an in-tree ``tmp`` directory
    (``/testbed/tests/tmp/expected_output.txt``, relative ``tmp/config.yml``)
    is a source edit, and silently skipping one would disarm the gate.
    """

    lowered = normalized_path.lower()
    if _SCRIPT_EXTENSION_RE.search(lowered):
        return False
    if "/squilla-scratch/" in f"/{lowered}":
        return True
    return lowered.startswith(("/tmp/", "/var/tmp/"))


def _heredoc_delimiters(line: str, quote: str | None) -> tuple[list[str], str | None]:
    """Heredoc delimiters opened at unquoted positions of ``line``.

    Returns the delimiters plus the quote state at end of line, so ``<<``
    inside a quoted string (``echo "use << here"``) is never misread as a
    heredoc opener and quoted strings spanning lines keep their state.
    """

    delimiters: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            continue
        if ch == "<":
            match = _HEREDOC_RE.match(line, i)
            if match is not None:
                delimiters.append(match.group(2))
                i = match.end()
                continue
        i += 1
    return delimiters, quote


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc bodies so their content is not segment-split as commands."""

    if "<<" not in (command or ""):
        return command or ""
    output_lines: list[str] = []
    pending: list[str] = []
    quote: str | None = None
    for line in (command or "").split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        delimiters, quote = _heredoc_delimiters(line, quote)
        pending.extend(delimiters)
        output_lines.append(line)
    return "\n".join(output_lines)


def _split_shell_segments(command: str) -> list[str]:
    """Split on newlines, ``;``, ``|``, ``||``, ``&&`` outside quotes.

    Newlines separate commands exactly like ``;`` (heredoc bodies are already
    stripped), and a backslash-escaped separator (``find ... -exec ... \\;``)
    stays inside its segment.
    """

    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    text = _strip_heredoc_bodies(command or "")
    while i < len(text):
        ch = text[i]
        if quote == "'":
            current.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\":
            current.append(text[i : i + 2])
            i += 2
            continue
        if quote == '"':
            current.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch in {";", "\n"}:
            segments.append("".join(current))
            current = []
            i += 1
            continue
        if ch == "|":
            segments.append("".join(current))
            current = []
            i += 2 if text[i : i + 2] == "||" else 1
            continue
        if text[i : i + 2] == "&&":
            segments.append("".join(current))
            current = []
            i += 2
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def _segment_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _segment_head(tokens: Sequence[str]) -> str:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ENV_ASSIGNMENT_RE.match(token):
            index += 1
            continue
        head = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if head in _WRAPPER_COMMAND_HEADS:
            index += 1
            if head == "timeout":
                while index < len(tokens) and (
                    tokens[index].startswith("-") or _TIMEOUT_DURATION_RE.match(tokens[index])
                ):
                    index += 1
            elif head == "env":
                while index < len(tokens) and tokens[index].startswith("-"):
                    index += 1
            elif head == "command":
                # ``command -v prog`` probes for existence; it runs nothing.
                probe = False
                while index < len(tokens) and tokens[index].startswith("-"):
                    if set(tokens[index][1:]) & {"v", "V"}:
                        probe = True
                    index += 1
                if probe:
                    return ""
            continue
        if head == "bundle" and index + 1 < len(tokens):
            follower = tokens[index + 1].replace("\\", "/").rsplit("/", 1)[-1].lower()
            if follower == "exec":
                index += 2
                continue
        if head == "npx":
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            continue
        return head
    return ""


def classify_gate_command(command: str) -> Literal["execution", "inspection"]:
    """Classify a shell command as execution-level or inspection-only.

    "execution" means the command could plausibly carry verification signal
    (interpreters, test runners, builds, package tools, unknown binaries);
    "inspection" means read-only/bookkeeping commands whose exit codes say
    nothing about patch correctness (grep, cat, git, rm, ...). Unknown heads
    classify as execution so the gate under-fires rather than over-fires.
    """

    for segment in _split_shell_segments(command or ""):
        head = _segment_head(_segment_tokens(segment))
        if head and head not in _INSPECTION_COMMAND_HEADS:
            return "execution"
    return "inspection"


def command_removal_targets(command: str) -> list[str]:
    """Return path tokens removed by ``rm`` segments of a shell command."""

    targets: list[str] = []
    for segment in _split_shell_segments(command or ""):
        tokens = _segment_tokens(segment)
        if _segment_head(tokens) != "rm":
            continue
        seen_rm = False
        for token in tokens:
            head = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if not seen_rm:
                if head == "rm":
                    seen_rm = True
                continue
            if token.startswith("-"):
                continue
            targets.append(token)
    return targets


# Git global flags that take a separate value token (``git -C /repo stash``).
_GIT_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)
# Stash subcommands by working-tree effect. ``list``/``show``/``drop``/
# ``clear``/``branch`` inspect or discard stash entries without reverting the
# current tree, so they carry no event.
_STASH_TREE_REVERT_FOLLOWERS: frozenset[str] = frozenset({"", "push", "save"})
_STASH_TREE_RESTORE_FOLLOWERS: frozenset[str] = frozenset({"pop", "apply"})


def _git_stash_event(tokens: Sequence[str]) -> Literal["revert", "restore"] | None:
    """Working-tree effect of one ``git stash`` segment, if any."""

    if _segment_head(tokens) != "git":
        return None
    positionals: list[str] = []
    seen_git = False
    index = 0
    while index < len(tokens) and len(positionals) < 2:
        token = tokens[index]
        lowered = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not seen_git:
            if lowered == "git":
                seen_git = True
            index += 1
            continue
        if token.startswith("-"):
            if token in _GIT_VALUE_FLAGS:
                index += 2
            else:
                index += 1
            continue
        positionals.append(token.lower())
        index += 1
    if not positionals or positionals[0] != "stash":
        return None
    follower = positionals[1] if len(positionals) > 1 else ""
    if follower in _STASH_TREE_RESTORE_FOLLOWERS:
        return "restore"
    if follower in _STASH_TREE_REVERT_FOLLOWERS:
        return "revert"
    return None


def scan_stash_effects(command: str, *, initially_stashed: bool) -> tuple[bool, bool]:
    """Walk a command's segments tracking the stash state of the working tree.

    Returns ``(ran_while_stashed, stashed_after)``: whether any
    execution-level segment ran while the tree was stash-reverted, and the
    tree state after the command (so the tracker can carry it across calls —
    ``git stash`` in one call and ``pytest`` in the next must behave exactly
    like ``git stash && pytest``).
    """

    stashed = initially_stashed
    ran_while_stashed = False
    for segment in _split_shell_segments(command or ""):
        tokens = _segment_tokens(segment)
        event = _git_stash_event(tokens)
        if event == "revert":
            stashed = True
            continue
        if event == "restore":
            stashed = False
            continue
        head = _segment_head(tokens)
        if stashed and head and head not in _INSPECTION_COMMAND_HEADS:
            ran_while_stashed = True
    return ran_while_stashed, stashed


def has_stash_reversal(command: str) -> bool:
    """True when an execution-level segment runs on a stash-reverted tree.

    ``git stash && <test>`` deliberately reverts the fix to confirm the bug
    still reproduces without it; a red result there is EXPECTED and must not
    count as failure evidence (``git stash pop``/``apply`` restore the fix
    before anything that follows them runs).
    """

    ran_while_stashed, _ = scan_stash_effects(command, initially_stashed=False)
    return ran_while_stashed


@dataclass(frozen=True)
class _SegmentProfile:
    """Comparable shape of one execution-level shell segment."""

    head: str
    positionals: frozenset[str]
    narrowing: frozenset[str]


def _segment_profile(segment: str) -> _SegmentProfile | None:
    tokens = _segment_tokens(segment)
    head = _segment_head(tokens)
    if not head or head in _INSPECTION_COMMAND_HEADS:
        return None
    is_interpreter = head in _INTERPRETER_HEADS
    positionals: set[str] = set()
    narrowing: set[str] = set()
    seen_head = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        normalized = token.replace("\\", "/")
        lowered = normalized.rsplit("/", 1)[-1].lower()
        if not seen_head:
            if lowered == head:
                seen_head = True
            index += 1
            continue
        if is_interpreter and token == "-m" and index + 1 < len(tokens):
            # ``python -m pytest ...``: the module names the program (already
            # folded into the profile head), not a test target.
            index += 2
            continue
        if token in _NARROWING_VALUE_FLAGS or lowered in _NARROWING_VALUE_FLAGS:
            value = tokens[index + 1] if index + 1 < len(tokens) else ""
            narrowing.add(f"{lowered}={value}")
            index += 2
            continue
        if any(token.startswith(prefix) for prefix in _NARROWING_MARKER_PREFIXES):
            narrowing.add(token)
            index += 1
            continue
        if token.startswith("-"):
            if "=" in token:
                flag, _, value = token.partition("=")
                if flag in _NARROWING_VALUE_FLAGS:
                    narrowing.add(f"{flag}={value}")
            index += 1
            continue
        if ">" in token or token.startswith("<") or token == "2>&1":
            index += 1
            continue
        positionals.add(normalized)
        index += 1
    return _SegmentProfile(
        # Resolve interpreter launches to the real program so
        # ``python -m pytest tests/x.py`` and ``pytest tests/x.py`` compare
        # as the same runner in deselection matching.
        head=_effective_program(tokens, head),
        positionals=frozenset(positionals),
        narrowing=frozenset(narrowing),
    )


def command_execution_profiles(command: str) -> list[_SegmentProfile]:
    """Profiles for the execution-classified segments of a command."""

    profiles: list[_SegmentProfile] = []
    for segment in _split_shell_segments(command or ""):
        profile = _segment_profile(segment)
        if profile is not None:
            profiles.append(profile)
    return profiles


def _narrowing_is_deselection(entry: str) -> bool:
    if any(entry.startswith(prefix) for prefix in _NARROWING_MARKER_PREFIXES):
        return True
    _, _, value = entry.partition("=")
    normalized = value.strip().strip("'\"").lower()
    return normalized.startswith("not ") or normalized.startswith("!")


def green_profiles_deselect_red(
    green: Sequence[_SegmentProfile],
    red: Sequence[_SegmentProfile],
) -> bool:
    """True when a green run re-covers a red run's targets minus the failure.

    This is the audited false-green signature: the model makes the failing
    command pass by EXCLUDING the failing case (added ``-k "not X"``,
    ``--ignore``/``--exclude``/``--deselect`` narrowing the red run did not
    have) rather than by fixing the source.
    """

    for red_segment in red:
        for green_segment in green:
            if green_segment.head != red_segment.head:
                continue
            if not red_segment.positionals <= green_segment.positionals:
                continue
            added = green_segment.narrowing - red_segment.narrowing
            if added and any(_narrowing_is_deselection(entry) for entry in added):
                return True
    return False


def _positional_bases(positionals: frozenset[str]) -> frozenset[str]:
    """Positional targets normalized to their base path.

    Strips pytest-style ``::node`` selectors so ``tests/test_x.py::test_a``
    (the focused red repro) and ``tests/test_x.py`` (the full-file green
    re-run) compare as the same target, and leading ``./`` so relative
    spellings compare equal.
    """

    bases: set[str] = set()
    for token in positionals:
        base = token.split("::", 1)[0]
        if base.startswith("./"):
            base = base[2:]
        bases.add(base.rstrip("/"))
    return frozenset(bases)


def _bases_cover(green_bases: frozenset[str], red_bases: frozenset[str]) -> bool:
    """Every red base equals a green base or sits under a green directory."""

    for red_base in red_bases:
        if not any(
            red_base == green_base or red_base.startswith(f"{green_base}/")
            for green_base in green_bases
        ):
            return False
    return True


def green_profiles_recover_red(
    green: Sequence[_SegmentProfile],
    red: Sequence[_SegmentProfile],
) -> bool:
    """True when a green run re-covers a red run's targets without deselection.

    The strict-mode red-first resolution rule: same runner, all of the red
    run's positional targets re-covered (compared by base path, so a focused
    ``file::node`` red matches a full-file or full-directory green re-run),
    and no ADDED deselection narrowing. A green that passes by excluding the
    failing case is the deselection false-green signature
    (``green_profiles_deselect_red``), not a resolution, so it never counts
    here. Zero-positional matching errs toward suppression: a bare runner
    invocation (``pytest``) re-covers everything.
    """

    for red_segment in red:
        red_bases = _positional_bases(red_segment.positionals)
        for green_segment in green:
            if green_segment.head != red_segment.head:
                continue
            green_bases = _positional_bases(green_segment.positionals)
            if green_bases and not _bases_cover(green_bases, red_bases):
                continue
            added = green_segment.narrowing - red_segment.narrowing
            if any(_narrowing_is_deselection(entry) for entry in added):
                continue
            return True
    return False


def is_detector_findings_exit(
    command: str,
    exit_code: int | None,
    timed_out: bool,
) -> bool:
    """True when exit code 1 came exclusively from lint/style detectors.

    Detector exit 1 means "findings in the input data", whose polarity is
    ambiguous (in detector-repo issues a finding is frequently the expected
    outcome), so it must not count as red verification evidence. Detectors
    launched through an interpreter (``php php-cs-fixer fix ...``) or
    ``python -m`` are resolved to the real program name.
    """

    if exit_code != 1 or timed_out:
        return False
    execution_heads = [
        _effective_program(_segment_tokens(segment), head)
        for segment, head in (
            (segment, _segment_head(_segment_tokens(segment)))
            for segment in _split_shell_segments(command or "")
        )
        if head and head not in _INSPECTION_COMMAND_HEADS
    ]
    return bool(execution_heads) and all(
        head in _DETECTOR_COMMAND_HEADS for head in execution_heads
    )


def _effective_program(tokens: Sequence[str], head: str) -> str:
    """Resolve an interpreter invocation to the program it actually runs."""

    if head not in _INTERPRETER_HEADS:
        return head
    seen_head = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if not seen_head:
            if lowered == head:
                seen_head = True
            index += 1
            continue
        if token == "-m" and index + 1 < len(tokens):
            return tokens[index + 1].lower()
        if token.startswith("-"):
            index += 1
            continue
        program = lowered
        if "." in program:
            program = program.rsplit(".", 1)[0]
        return program
    return head


def execution_signals_from_result(
    *,
    tool_name: str,
    content_text: str,
    execution_status: Mapping[str, Any] | None,
    is_error: bool,
) -> tuple[bool, int | None, bool, str | None]:
    """Return ``(red, exit_code, timed_out, status_reason)`` for a tool result.

    Prefers the canonical execution-status sidecar; falls back to re-deriving
    it from the raw result text (``exit_code=N`` first line) so live-loop and
    transcript-replay callers see identical semantics.
    """

    status: Mapping[str, Any] | None = execution_status
    if not isinstance(status, Mapping) or status.get("status") in (None, "", "unknown"):
        derived = execution_status_for_tool_result(tool_name, content_text)
        if derived is not None:
            status = derived
    if isinstance(status, Mapping):
        raw_status = str(status.get("status") or "")
        raw_exit = status.get("exit_code")
        exit_code = (
            raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else None
        )
        timed_out = bool(status.get("timed_out"))
        raw_reason = status.get("reason")
        status_reason = str(raw_reason) if raw_reason else None
        red = (
            raw_status in _ERROR_STATUSES
            or timed_out
            or (exit_code is not None and exit_code != 0)
            or bool(is_error)
        )
        return red, exit_code, timed_out, status_reason
    return bool(is_error), None, False, None


@dataclass
class GateExecutionRecord:
    """One execution-level command observation."""

    command: str
    red: bool
    exit_code: int | None
    timed_out: bool
    status_reason: str | None
    failure_anchors: list[str]
    iteration: int
    artifact_paths: list[str] = field(default_factory=list)
    sequence: int = 0
    profiles: list[_SegmentProfile] = field(default_factory=list)


@dataclass
class _ReproArtifact:
    path: str
    ever_green: bool = False
    red_run_count: int = 0
    last_record: GateExecutionRecord | None = None
    deleted: bool = False


@dataclass
class _RedFirstCandidate:
    """A red observed without the patch applied.

    Either a red before the first source edit or a red taken on a
    stash-reverted tree: both demonstrate the failure the run is supposed to
    fix. A later green on the patched tree that re-covers the candidate's
    shape (or re-runs one of its tracked artifacts) resolves red-first.
    """

    profiles: list[_SegmentProfile]
    artifact_paths: list[str]


@dataclass(frozen=True)
class FinalizeEvidenceObservation:
    """Finalize-time summary of unresolved red evidence."""

    triggers: list[str]
    red_command: str | None
    red_exit_code: int | None
    red_timed_out: bool
    red_status_reason: str | None
    red_failure_anchors: list[str]
    red_artifact_paths: list[str]
    deleted_never_green_repro_paths: list[str]
    post_edit_execution_count: int
    post_edit_red_count: int
    source_edit_seen: bool
    has_workspace_diff: bool
    # Strict-mode evidence summary. Computed in both modes so offline replay
    # can compare trigger sets on the same traces; the strict TRIGGERS only
    # fire when the tracker runs with strict=True.
    verification_command_count: int = 0
    red_first_satisfied: bool = False
    red_first_candidate_count: int = 0
    strict: bool = False

    @property
    def should_challenge(self) -> bool:
        return bool(self.triggers)

    @property
    def primary_reason(self) -> str:
        return self.triggers[0] if self.triggers else "finalize_evidence_ok"

    def to_event_details(self) -> dict[str, Any]:
        return {
            "triggers": self.triggers,
            "primary_reason": self.primary_reason,
            "should_challenge": self.should_challenge,
            "red_command": self.red_command,
            "red_exit_code": self.red_exit_code,
            "red_timed_out": self.red_timed_out,
            "red_status_reason": self.red_status_reason,
            "red_failure_anchors": self.red_failure_anchors,
            "red_artifact_paths": self.red_artifact_paths,
            "deleted_never_green_repro_paths": self.deleted_never_green_repro_paths,
            "post_edit_execution_count": self.post_edit_execution_count,
            "post_edit_red_count": self.post_edit_red_count,
            "source_edit_seen": self.source_edit_seen,
            "has_workspace_diff": self.has_workspace_diff,
            "verification_command_count": self.verification_command_count,
            "red_first_satisfied": self.red_first_satisfied,
            "red_first_candidate_count": self.red_first_candidate_count,
            "strict": self.strict,
        }


class FinalizeEvidenceTracker:
    """Pure in-run state machine feeding the finalize-time evidence gate."""

    _MAX_POST_EDIT_EXECUTIONS = 200
    _MAX_RED_FIRST_CANDIDATES = 50

    def __init__(self, *, strict: bool = False) -> None:
        self._strict = bool(strict)
        self._source_edit_seen = False
        self._post_edit_executions: list[GateExecutionRecord] = []
        self._artifacts: dict[str, _ReproArtifact] = {}
        self._sequence = 0
        self._tree_stashed = False
        # Strict-mode evidence state. Tracked in both modes (cheap, and it
        # lets offline replay compare strict trigger sets on the same traces);
        # the strict triggers themselves only fire when ``strict=True``.
        self._verification_command_count = 0
        self._red_first_candidates: list[_RedFirstCandidate] = []
        self._red_first_candidate_count = 0
        self._red_first_satisfied = False

    def observe_write(
        self,
        path: str | None,
        *,
        is_error: bool = False,
        iteration: int = 0,
        scratch: bool = False,
    ) -> None:
        """Record a successful write-tool call.

        Repro-like writes (and any script written through a scratch-write
        tool) register a tracked artifact and do NOT reset the post-edit
        execution window; every other write counts as a source edit and
        resets it.
        """

        if is_error:
            return
        normalized = str(path or "").strip().replace("\\", "/")
        is_artifact = bool(normalized) and (
            looks_repro_artifact_path(normalized)
            or (scratch and bool(_SCRIPT_EXTENSION_RE.search(normalized.lower())))
        )
        if is_artifact:
            artifact = self._artifacts.get(normalized)
            if artifact is None:
                self._artifacts[normalized] = _ReproArtifact(path=normalized)
            else:
                # Rewritten after (possible) deletion: track the new copy.
                artifact.deleted = False
            return
        if normalized and _is_nonsource_scratch_note(normalized):
            # A scratch note (FIX_SUMMARY.md, notes.txt under /tmp) is
            # neither a tracked artifact nor a source edit: it must not
            # reset the post-edit execution window.
            return
        if scratch:
            return
        self._source_edit_seen = True
        self._post_edit_executions = []

    def observe_execution(
        self,
        command: str,
        *,
        red: bool,
        exit_code: int | None = None,
        timed_out: bool = False,
        status_reason: str | None = None,
        failure_anchors: Sequence[str] = (),
        iteration: int = 0,
        evidence_credit: bool = True,
    ) -> None:
        """Record an execution-tool call (``exec_command``-like).

        ``evidence_credit=False`` (scratch verify-mirror hash guard) keeps
        side-effect tracking — deletions and stash state — but withholds all
        verification crediting: the command ran against mirror copies that
        no longer match their workspace originals, so its outcome says
        nothing about the workspace in either polarity.
        """

        command_text = str(command or "")
        if status_reason in _NON_VERIFICATION_STATUS_REASONS:
            # Policy/approval denials, harness-internal tool errors, and
            # cancellations mean the command never ran: a denied ``rm``
            # deleted nothing, a denied ``git stash`` stashed nothing, and a
            # denied test verified nothing in either polarity. Skip before
            # any side-effect tracking.
            return
        removed = command_removal_targets(command_text)
        if removed:
            self._mark_deleted(removed)
        # Track the stash state of the working tree across calls: ``git
        # stash`` in one call and ``pytest`` in the next behave exactly like
        # ``git stash && pytest`` in one call.
        ran_while_stashed, self._tree_stashed = scan_stash_effects(
            command_text, initially_stashed=self._tree_stashed
        )
        if classify_gate_command(command_text) != "execution":
            return
        if not evidence_credit:
            # Hash-guarded mirror run against diverged copies: side effects
            # above still counted, but the outcome is not verification
            # evidence — no verification count, no red-first candidacy, no
            # post-edit record. Green here must not satisfy red-first and
            # red here must not become an outstanding failure.
            return
        if ran_while_stashed:
            # The fix is deliberately stashed away: red here confirms the bug
            # reproduces WITHOUT the patch and green would say nothing about
            # it. Neither polarity is evidence about the current workspace —
            # but a red on the reverted tree demonstrates the failure without
            # the patch, which is exactly what strict red-first asks for, so
            # it registers a red-first candidate (and the run still counts as
            # execution activity for zero-verification).
            if not (exit_code in _COMMAND_FORM_ERROR_EXITS and not timed_out):
                self._verification_command_count += 1
                if bool(red) and not is_detector_findings_exit(
                    command_text, exit_code, timed_out
                ):
                    self._note_red_first_candidate(
                        command_execution_profiles(command_text),
                        self._referenced_artifacts(command_text),
                    )
            return
        if exit_code in _COMMAND_FORM_ERROR_EXITS and not timed_out:
            # Command-not-found / not-executable says nothing about the
            # patch under test; a typo or missing interpreter must not
            # become red evidence. (Side effects above still count: in
            # ``rm x && ./missing.sh`` the rm ran before the 127.)
            return
        red_flag = bool(red)
        if red_flag and is_detector_findings_exit(command_text, exit_code, timed_out):
            red_flag = False
        self._sequence += 1
        record = GateExecutionRecord(
            command=command_text[:500],
            red=red_flag,
            exit_code=exit_code,
            timed_out=bool(timed_out),
            status_reason=status_reason,
            failure_anchors=[str(anchor)[:220] for anchor in list(failure_anchors)[:3]],
            iteration=int(iteration),
            artifact_paths=self._referenced_artifacts(command_text),
            sequence=self._sequence,
            profiles=command_execution_profiles(command_text),
        )
        for artifact_path in record.artifact_paths:
            artifact = self._artifacts[artifact_path]
            artifact.last_record = record
            if record.red:
                artifact.red_run_count += 1
            else:
                artifact.ever_green = True
        self._verification_command_count += 1
        if record.red:
            # Any surviving red demonstrates a failure the run can later show
            # resolved; a later patched-tree green matching it satisfies
            # red-first. Pre-edit, post-edit, and stash-reverted reds all
            # qualify — restricting to pre-first-edit reds would fire on the
            # legitimate edit-then-reproduce flow.
            self._note_red_first_candidate(record.profiles, record.artifact_paths)
        elif self._source_edit_seen:
            self._resolve_red_first(record)
        if self._source_edit_seen:
            self._post_edit_executions.append(record)
            if len(self._post_edit_executions) > self._MAX_POST_EDIT_EXECUTIONS:
                del self._post_edit_executions[0]

    def _note_red_first_candidate(
        self,
        profiles: Sequence[_SegmentProfile],
        artifact_paths: Sequence[str],
    ) -> None:
        self._red_first_candidate_count += 1
        if self._red_first_satisfied:
            return
        if len(self._red_first_candidates) < self._MAX_RED_FIRST_CANDIDATES:
            self._red_first_candidates.append(
                _RedFirstCandidate(
                    profiles=list(profiles),
                    artifact_paths=list(artifact_paths),
                )
            )

    def _resolve_red_first(self, record: GateExecutionRecord) -> None:
        """Match a patched-tree green against demonstrated-failure candidates."""

        if self._red_first_satisfied or not self._red_first_candidates:
            return
        for candidate in self._red_first_candidates:
            if candidate.artifact_paths and any(
                path in record.artifact_paths for path in candidate.artifact_paths
            ):
                self._red_first_satisfied = True
                return
            if candidate.profiles:
                if green_profiles_recover_red(record.profiles, candidate.profiles):
                    self._red_first_satisfied = True
                    return
            elif not candidate.artifact_paths:
                # A red with no comparable shape cannot be re-matched; err
                # toward suppression and let any patched-tree green resolve it.
                self._red_first_satisfied = True
                return

    def build_observation(self, *, has_workspace_diff: bool) -> FinalizeEvidenceObservation:
        triggers: list[str] = []
        red_record: GateExecutionRecord | None = None
        deleted_never_green: list[str] = []
        post_edit_red = [record for record in self._post_edit_executions if record.red]
        if has_workspace_diff and self._source_edit_seen:
            last_execution = (
                self._post_edit_executions[-1] if self._post_edit_executions else None
            )
            if last_execution is not None and last_execution.red:
                triggers.append("red_execution_after_final_edit")
                red_record = last_execution
            outstanding = self._outstanding_red_repro_record()
            if outstanding is not None:
                if "red_execution_after_final_edit" not in triggers:
                    triggers.append("red_repro_outstanding_after_final_edit")
                    red_record = outstanding
                elif red_record is not None and outstanding is not red_record:
                    triggers.append("red_repro_outstanding_after_final_edit")
            unresolved = self._deselected_red_record()
            if unresolved is not None:
                if red_record is None:
                    triggers.append("red_evidence_deselected_after_final_edit")
                    red_record = unresolved
                elif unresolved is not red_record:
                    triggers.append("red_evidence_deselected_after_final_edit")
            if (
                len(post_edit_red) >= RED_MAJORITY_MIN_COUNT
                and len(post_edit_red) * 2 > len(self._post_edit_executions)
            ):
                triggers.append("red_majority_after_final_edit")
                if red_record is None:
                    red_record = post_edit_red[-1]
            deleted_never_green = sorted(
                artifact.path
                for artifact in self._artifacts.values()
                if artifact.deleted and not artifact.ever_green and artifact.red_run_count > 0
            )
            if deleted_never_green:
                triggers.append("never_green_repro_deleted")
            if not self._post_edit_executions:
                triggers.append("no_execution_after_final_edit")
            if self._strict:
                # The strict trigger appends AFTER the base ones so base-mode
                # primary reasons (and their dedup keys) are unperturbed.
                # Red-first state is reported but deliberately not a trigger:
                # green-only runs are routinely legitimate.
                if self._verification_command_count == 0:
                    triggers.append("zero_verification")
        return FinalizeEvidenceObservation(
            triggers=triggers,
            red_command=red_record.command if red_record else None,
            red_exit_code=red_record.exit_code if red_record else None,
            red_timed_out=bool(red_record.timed_out) if red_record else False,
            red_status_reason=red_record.status_reason if red_record else None,
            red_failure_anchors=list(red_record.failure_anchors) if red_record else [],
            red_artifact_paths=list(red_record.artifact_paths) if red_record else [],
            deleted_never_green_repro_paths=deleted_never_green,
            post_edit_execution_count=len(self._post_edit_executions),
            post_edit_red_count=len(post_edit_red),
            source_edit_seen=self._source_edit_seen,
            has_workspace_diff=bool(has_workspace_diff),
            verification_command_count=self._verification_command_count,
            red_first_satisfied=self._red_first_satisfied,
            red_first_candidate_count=self._red_first_candidate_count,
            strict=self._strict,
        )

    def _outstanding_red_repro_record(self) -> GateExecutionRecord | None:
        """Latest post-final-edit run of a tracked artifact that stayed red.

        Deleted artifacts are excluded: they cannot be re-run as-is, so they
        are handled by the ``never_green_repro_deleted`` trigger instead.
        """

        candidates = [
            artifact.last_record
            for artifact in self._artifacts.values()
            if artifact.last_record is not None
            and artifact.last_record.red
            and not artifact.deleted
            and any(artifact.last_record is record for record in self._post_edit_executions)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record.sequence)

    def _deselected_red_record(self) -> GateExecutionRecord | None:
        """Latest post-final-edit red whose later green excluded the failure.

        Command-form failures (exit 126/127) are excluded so an abandoned
        typo cannot participate.
        """

        for index in range(len(self._post_edit_executions) - 1, -1, -1):
            record = self._post_edit_executions[index]
            if not record.red or not record.profiles:
                continue
            if record.exit_code in _COMMAND_FORM_ERROR_EXITS:
                continue
            for later in self._post_edit_executions[index + 1 :]:
                if later.red or not later.profiles:
                    continue
                if green_profiles_deselect_red(later.profiles, record.profiles):
                    return record
        return None

    def _referenced_artifacts(self, command: str) -> list[str]:
        """Artifacts referenced by execution-classified segments only.

        ``rm repro.py && pytest tests/`` must not attribute the pytest result
        to the deleted script, so tokens of inspection segments (rm, cp, git,
        ...) never count as references.
        """

        if not self._artifacts:
            return []
        tokens: list[str] = []
        for segment in _split_shell_segments(command):
            segment_tokens = _segment_tokens(segment)
            head = _segment_head(segment_tokens)
            if head and head not in _INSPECTION_COMMAND_HEADS:
                tokens.extend(segment_tokens)
        referenced: list[str] = []
        for path, artifact in self._artifacts.items():
            basename = path.rsplit("/", 1)[-1]
            for token in tokens:
                normalized = token.replace("\\", "/").strip("'\"")
                if (
                    normalized == path
                    or normalized.endswith(f"/{path}")
                    or normalized == basename
                    or normalized.endswith(f"/{basename}")
                ):
                    referenced.append(artifact.path)
                    break
        return referenced

    def _mark_deleted(self, targets: Sequence[str]) -> None:
        for target in targets:
            normalized = str(target or "").replace("\\", "/").strip("'\"").rstrip("/")
            if not normalized:
                continue
            basename = normalized.rsplit("/", 1)[-1]
            for path, artifact in self._artifacts.items():
                artifact_basename = path.rsplit("/", 1)[-1]
                if (
                    normalized == path
                    or normalized.endswith(f"/{path}")
                    or basename == path
                    or artifact_basename == basename
                    or path.startswith(f"{normalized}/")
                    or f"/{normalized}/" in f"/{path}"
                ):
                    artifact.deleted = True


def finalize_evidence_gate_key(observation: FinalizeEvidenceObservation) -> str:
    """Dedup key: the same unresolved red state never re-fires the gate.

    The key is derived purely from the red *evidence* (reason, triggers, the
    failing command and its exit code, failure anchors, and deleted-repro
    paths). It deliberately excludes volatile progress counters such as
    ``post_edit_execution_count``: once the caller has challenged a given red
    state and recorded its key, running further commands that leave the same
    evidence in place must not mint a fresh key and re-fire the challenge. This
    is what makes the caller's ``keys.add(key)`` guard a genuine one-shot latch
    per red state rather than a counter that quietly resets on every execution.
    """

    payload = {
        "primary_reason": observation.primary_reason,
        "triggers": observation.triggers,
        "red_command": observation.red_command,
        "red_exit_code": observation.red_exit_code,
        "red_failure_anchors": observation.red_failure_anchors,
        "deleted_never_green_repro_paths": observation.deleted_never_green_repro_paths,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


_BINDING_SENTENCE = (
    "A failing reproduction or verification you ran yourself is binding evidence "
    "that the issue is not fixed yet; green results from other tests, builds, or "
    "suites do not override it."
)

# Model-visible one-shot contract. Paired with the caller's per-red-state latch
# (see finalize_evidence_gate_key), this tells the model the check will not
# repeat for the same failing state, so it should resolve or justify it now
# instead of treating a recurring challenge as pressure to keep re-finalizing.
_ONE_SHOT_NOTICE = (
    " This evidence check is one-shot for this exact failing state: it will not "
    "repeat for the same evidence, so resolve it or justify finishing now."
)


def finalize_evidence_challenge_message(observation: FinalizeEvidenceObservation) -> str:
    """One bounded, artifact-specific challenge. Never demands minimality."""

    reason = observation.primary_reason
    if reason in {
        "red_execution_after_final_edit",
        "red_repro_outstanding_after_final_edit",
        "red_evidence_deselected_after_final_edit",
        "red_majority_after_final_edit",
    }:
        command = observation.red_command or "the failing command"
        if reason == "red_evidence_deselected_after_final_edit":
            lead = (
                f"You are about to finish, but after `{command}` failed following "
                "your final source edit, the later passing run excluded or "
                "deselected part of what failed instead of fixing it. The original "
                "command is still unverified"
            )
        elif reason == "red_majority_after_final_edit":
            lead = (
                "You are about to finish, but most of the commands you ran after "
                f"your final source edit failed ({observation.post_edit_red_count} "
                f"of {observation.post_edit_execution_count}), most recently: "
                f"`{command}`"
            )
        else:
            lead = (
                "You are about to finish, but the latest execution after your final "
                f"source edit is still failing: `{command}`"
            )
        exit_text = ""
        if observation.red_timed_out:
            exit_text = " (timed out)"
        elif observation.red_exit_code is not None:
            exit_text = f" (exit code {observation.red_exit_code})"
        anchor_text = ""
        if observation.red_failure_anchors:
            rendered = " | ".join(observation.red_failure_anchors[:3])
            anchor_text = f" Failure signal: {rendered}."
        artifact_text = ""
        if observation.red_artifact_paths:
            artifact_text = (
                " It runs your own script(s): "
                f"{', '.join(observation.red_artifact_paths[:3])}."
            )
        return (
            "[Finalize evidence check]\n"
            f"{lead}{exit_text}.{artifact_text}"
            f"{anchor_text} {_BINDING_SENTENCE} Do not finalize yet. Re-run that "
            "exact command against the current workspace state; if it still fails, "
            "use its output to revise the source fix and re-run until it passes. "
            "Only if the command itself is invalid (wrong path, stale script, or "
            "expectations that contradict the issue report) may you finish, and "
            "then explicitly justify that in your final answer." + _ONE_SHOT_NOTICE
        )
    if reason == "never_green_repro_deleted":
        paths = ", ".join(observation.deleted_never_green_repro_paths[:3])
        return (
            "[Finalize evidence check]\n"
            f"You are about to finish, but your own script(s) {paths} failed on "
            "every recorded run and were deleted before a passing run was "
            f"observed. {_BINDING_SENTENCE} Do not finalize yet. Recreate or "
            "re-run a reproduction that follows the issue report against the "
            "current workspace state and confirm it passes; if it fails, use its "
            "output to revise the source fix first." + _ONE_SHOT_NOTICE
        )
    if "zero_verification" in observation.triggers:
        # ``zero_verification`` implies ``no_execution_after_final_edit`` (a
        # zero-count run has an empty post-edit window), so it never becomes
        # the primary reason; it enriches the no-execution shape instead.
        return (
            "[Finalize evidence check]\n"
            "You are about to finish, but no execution-level command ran at any "
            "point in this run: the patch you are shipping has never been "
            "exercised. Do not finalize yet. Write and run a reproduction of "
            "the reported issue against the current workspace state; confirm it "
            "fails without your change (for example after `git stash`) and "
            "passes with it (after `git stash pop`), and use any failure output "
            f"to revise the source fix first. {_BINDING_SENTENCE}"
        )
    return (
        "[Finalize evidence check]\n"
        "You are about to finish, but no execution-level command ran after your "
        "final source edit, so the patch you are shipping is unverified in its "
        "current state. Do not finalize yet. Re-run your reproduction of the "
        "issue, or the most relevant focused test, against the current workspace "
        f"state and confirm it passes. {_BINDING_SENTENCE}" + _ONE_SHOT_NOTICE
    )
