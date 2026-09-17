"""Bounded, deterministic repairs for generated LaTeX manuscripts."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

_RESULT_SECTION_RE = re.compile(
    r"(?:\\begin\{abstract\}|\\section\*?\{(?:"
    r"Experiments?|Experimental\s+Evaluation|Evaluations?|Results?|Findings?|"
    r"Discussion|Conclusion|实验|评估|结果|讨论|结论)[^}]*\})"
    r"[\s\S]*?(?=\\end\{abstract\}|\\section\*?\{|\\end\{document\}|\Z)",
    re.IGNORECASE,
)
_FORECAST_CUE_RE = re.compile(
    r"\b(?:expect(?:ed)?|predict(?:ed)?|project(?:ed)?|anticipat(?:e|ed)|"
    r"estimat(?:e|ed)|hypothesi[sz](?:e|ed)?|hypothesis|likely|may|might|"
    r"could|would|should|will)\b|"
    r"预期|预计|预测|估计|有望|可能|将会|假设|目标|可使|可以|能够|"
    r"(?<!功)(?<!性)能(?=将|使|把|让|在|于)",
    re.IGNORECASE,
)
_OUTCOME_TERM_RE = re.compile(
    r"\b(?:accuracy|precision|recall|f1|score|latency|cost|overhead|"
    r"throughput|utility|performance|convergence|communication|rounds?|"
    r"epochs?|steps?|improv\w*|outperform\w*|increase\w*|decrease\w*|"
    r"reduc\w*|drop\w*|rise\w*|fall\w*|gain\w*)\b|"
    r"精度|准确率|召回率|分数|延迟|开销|吞吐|效用|性能|收敛|通信|"
    r"轮次|提升|提高|改善|增加|上升|降低|减少|下降|优于|高出|"
    r"达到|保持|维持|降至|升至|恶化",
    re.IGNORECASE,
)
_OWN_METHOD_RE = re.compile(
    r"\b(?:ours|our\s+(?:method|approach|framework|model|system|algorithm)|"
    r"the\s+(?:proposed|presented)\s+(?:method|approach|framework|model|system)|"
    r"this\s+(?:method|approach|framework|model|system|algorithm))\b|"
    r"本文(?:方法|模型|框架|方案|算法)?|本研究|所提(?:方法|模型|框架|方案|算法)|"
    r"我们(?:的)?(?:方法|模型|框架|方案|算法)|该(?:方法|机制|模型|框架|方案|算法)",
    re.IGNORECASE,
)
_DECLARED_THRESHOLD_RE = re.compile(
    r"\b(?:define|defined|set|predefined|target|threshold|criterion|"
    r"significance\s+(?:level|threshold))\b|"
    r"定义|设定|预设|预定义|目标|阈值|判定标准|显著性(?:水平|阈值)",
    re.IGNORECASE,
)
_CHANGE_TERM_RE = re.compile(
    r"\b(?:improv\w*|outperform\w*|increase\w*|decrease\w*|reduc\w*|"
    r"drop\w*|rise\w*|fall\w*|gain\w*|from)\b|"
    r"提升|提高|改善|增加|上升|降低|减少|下降|优于|高出|"
    r"达到|保持|维持|降至|升至|恶化|从",
    re.IGNORECASE,
)
_RESULT_MAGNITUDE_RE = re.compile(
    r"(?P<prefix>(?:约|至少|至多|小于|低于|高于|超过|不超过|"
    r"less\s+than|more\s+than|at\s+least|at\s+most|below|under|over|"
    r"[<>≤≥])?\s*)"
    r"(?P<first>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>\\?%|percentage\s+points?|points?|x|ms|milliseconds?|"
    r"seconds?|rounds?|epochs?|steps?|个百分点|倍|毫秒|秒|轮次|轮|步)"
    r"(?:\s*(?:--|-|至|到)\s*"
    r"(?P<second>\d+(?:\.\d+)?)\s*(?P<second_unit>\\?%|"
    r"percentage\s+points?|points?|x|ms|milliseconds?|seconds?|rounds?|"
    r"epochs?|steps?|个百分点|倍|毫秒|秒|轮次|轮|步)?)?"
    r"(?:以上|以下|以内|以外)?",
    re.IGNORECASE,
)
_CLAUSE_RE = re.compile(r"[^,，;；.!?。！？\n]+(?:[,，;；.!?。！？\n]+|\Z)")
_REDUNDANT_PLACEHOLDER_INPUTS = {
    "figure_placeholder_template": r"\\begin\{figure\*?\}",
    "table_placeholder_template": r"\\begin\{table\*?\}",
}
_SAFE_META_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _evidence_status(paper_contract: str) -> str:
    match = re.search(
        r"\bEVIDENCE_STATUS\s*:\s*(supplied|not_supplied)\b",
        paper_contract,
        re.IGNORECASE,
    )
    return match.group(1).lower() if match else "not_supplied"


def _normalize_punctuation(text: str) -> tuple[str, int]:
    replacements = text.count("\u2011") + text.count("\u2013")
    text = text.replace("\u2011", "-").replace("\u2013", "--")

    # A double em dash is ordinary Chinese punctuation. Only normalize an
    # isolated em dash; malformed longer runs remain visible to the strict
    # publication gate instead of being silently guessed at here.
    text, isolated_count = re.subn(r"(?<!\u2014)\u2014(?!\u2014)", "---", text)
    return text, replacements + isolated_count


def _remove_redundant_placeholder_inputs(text: str) -> tuple[str, int]:
    repairs = 0
    for template_name, inline_environment in _REDUNDANT_PLACEHOLDER_INPUTS.items():
        if not re.search(inline_environment, text):
            continue
        pattern = re.compile(
            rf"^[ \t]*\\input\s*\{{\s*{re.escape(template_name)}(?:\.tex)?\s*\}}"
            r"[ \t]*(?:%[^\n]*)?\n?",
            re.MULTILINE,
        )
        text, count = pattern.subn("", text)
        repairs += count
    return text, repairs


def _placeholder_for(clause: str) -> str:
    return "待实验确定" if re.search(r"[\u3400-\u9fff]", clause) else r"\textless TBD\textgreater"


def _magnitude_signature(match: re.Match[str]) -> tuple[str, str, str, str]:
    def normalize_unit(value: str | None) -> str:
        return re.sub(r"\s+", "", (value or "").casefold().lstrip("\\"))

    return (
        match.group("first"),
        normalize_unit(match.group("unit")),
        match.group("second") or "",
        normalize_unit(match.group("second_unit")),
    )


def _explicit_magnitudes(source_text: str) -> set[tuple[str, str, str, str]]:
    normalized, _ = _normalize_punctuation(source_text)
    return {_magnitude_signature(match) for match in _RESULT_MAGNITUDE_RE.finditer(normalized)}


def _magnitude_is_outcome(clause: str, match: re.Match[str]) -> bool:
    unit = match.group("unit").casefold().lstrip("\\")
    if unit in {"round", "rounds", "epoch", "epochs", "step", "steps", "轮", "轮次", "步"}:
        prefix = clause[max(0, match.start() - 24) : match.start()]
        suffix = clause[match.end() : min(len(clause), match.end() + 16)]
        return bool(
            _CHANGE_TERM_RE.search(prefix)
            or re.match(r"\s*(?:to|降至|升至)", suffix, re.I)
        )
    context = clause[max(0, match.start() - 48) : min(len(clause), match.end() + 24)]
    return bool(
        _OUTCOME_TERM_RE.search(context)
        or _CHANGE_TERM_RE.search(context)
        or str(match.group("prefix") or "").strip()
    )


def _sanitize_forecast_clause(
    clause: str,
    sentence_is_risky: bool,
    sentence_has_outcome: bool,
    explicit_magnitudes: set[tuple[str, str, str, str]],
) -> tuple[str, int]:
    if not sentence_is_risky or not (
        sentence_has_outcome or _OUTCOME_TERM_RE.search(clause)
    ):
        return clause, 0

    placeholder = _placeholder_for(clause)
    repairs = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal repairs
        if not _magnitude_is_outcome(clause, match):
            return match.group(0)
        prefix = clause[max(0, match.start() - 56) : match.start()]
        threshold_matches = list(_DECLARED_THRESHOLD_RE.finditer(prefix))
        if threshold_matches:
            after_threshold = prefix[threshold_matches[-1].end() :]
            # Preserve a named decision/metric threshold only when no outcome
            # change is asserted between the declaration and the value. This
            # avoids treating "target privacy budget ... expected accuracy
            # drop below 5%" as if 5% itself were a declared threshold.
            if (
                not _CHANGE_TERM_RE.search(after_threshold)
                and _magnitude_signature(match) in explicit_magnitudes
            ):
                return match.group(0)
        repairs += 1
        return placeholder

    return _RESULT_MAGNITUDE_RE.sub(replace, clause), repairs


def _neutralize_forecast_magnitudes(
    text: str,
    *,
    explicit_magnitudes: set[tuple[str, str, str, str]],
) -> tuple[str, int]:
    repairs = 0

    def sanitize_text(
        region: str,
        *,
        require_own_method: bool,
        allow_asserted_change: bool,
    ) -> str:
        nonlocal repairs
        pieces: list[str] = []
        cursor = 0
        # Forecast cues commonly occur in one comma-delimited clause and the
        # magnitude in the next. Track the cue over the complete sentence,
        # while editing only clauses that also name an outcome.
        for sentence_match in re.finditer(r"[^.!?。！？]+(?:[.!?。！？]+|\Z)", region):
            pieces.append(region[cursor : sentence_match.start()])
            sentence = sentence_match.group(0)
            sentence_has_forecast = bool(
                _FORECAST_CUE_RE.search(sentence)
                and (not require_own_method or _OWN_METHOD_RE.search(sentence))
            )
            sentence_has_outcome = bool(_OUTCOME_TERM_RE.search(sentence))
            sentence_has_asserted_change = bool(
                allow_asserted_change
                and _CHANGE_TERM_RE.search(sentence)
                and sentence_has_outcome
                and (not require_own_method or _OWN_METHOD_RE.search(sentence))
            )
            sentence_is_risky = sentence_has_forecast or sentence_has_asserted_change
            sentence_parts: list[str] = []
            sentence_cursor = 0
            for clause_match in _CLAUSE_RE.finditer(sentence):
                sentence_parts.append(sentence[sentence_cursor : clause_match.start()])
                repaired, count = _sanitize_forecast_clause(
                    clause_match.group(0),
                    sentence_is_risky,
                    sentence_has_outcome,
                    explicit_magnitudes,
                )
                sentence_parts.append(repaired)
                repairs += count
                sentence_cursor = clause_match.end()
            sentence_parts.append(sentence[sentence_cursor:])
            pieces.append("".join(sentence_parts))
            cursor = sentence_match.end()
        pieces.append(region[cursor:])
        return "".join(pieces)

    text = _RESULT_SECTION_RE.sub(
        lambda match: sanitize_text(
            match.group(0),
            require_own_method=False,
            allow_asserted_change=True,
        ),
        text,
    )
    # A manuscript can also forecast its own results in the introduction or
    # method description. Scrub those sentences while leaving cited prior-work
    # numbers alone unless they are presented as outcomes of this manuscript.
    text = sanitize_text(text, require_own_method=True, allow_asserted_change=True)
    return text, repairs


def sanitize_manuscript(
    text: str,
    *,
    evidence_status: str,
    explicit_source_text: str = "",
) -> tuple[str, int, int, int]:
    text, punctuation_repairs = _normalize_punctuation(text)
    text, redundant_input_repairs = _remove_redundant_placeholder_inputs(text)
    evidence_repairs = 0
    if evidence_status == "not_supplied":
        text, evidence_repairs = _neutralize_forecast_magnitudes(
            text,
            explicit_magnitudes=_explicit_magnitudes(explicit_source_text),
        )
    return text, punctuation_repairs, evidence_repairs, redundant_input_repairs


def _package_parts(package: str) -> tuple[str, str]:
    manuscript_match = re.search(
        r"MANUSCRIPT_TEX:\s*([\s\S]+?)(?:REFERENCES_BIB:|COMPILE_NOTES:|\Z)",
        package,
    )
    bibliography_match = re.search(
        r"REFERENCES_BIB:\s*([\s\S]+?)(?:MANUSCRIPT_PLAN:|"
        r"TARGET_LENGTH_EXPANSION_PLAN:|REFERENCE_PLACEHOLDERS:|COMPILE_NOTES:|\Z)",
        package,
    )
    return (
        manuscript_match.group(1).strip() if manuscript_match else "",
        bibliography_match.group(1).strip() if bibliography_match else "",
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _workspace_file(
    raw_path: str,
    *,
    label: str = "manuscript",
    expected_path: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve an existing file without allowing a generated path to escape cwd.

    ``skill_exec`` runs bundled scripts with the meta-skill workspace as their
    working directory.  The manuscript manifest is produced by an LLM step, so
    its path is data rather than authority: absolute paths, ``..`` traversal,
    and symlinks must not grant write access outside that trusted workspace.
    """

    workspace = Path.cwd().resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    if candidate.is_symlink():
        return None, f"{label} path must not be a symlink"
    resolved = candidate.resolve()
    if resolved != workspace and not resolved.is_relative_to(workspace):
        return None, f"{label} path escapes the skill workspace"
    if expected_path is not None and resolved != expected_path.resolve():
        return None, f"{label} path does not belong to this meta-skill run"
    if not resolved.is_file():
        return None, f"{label} path was not found in the skill workspace"
    return resolved, None


def sanitize_payload(payload: dict[str, Any]) -> tuple[str, int]:
    paper_contract = str(payload.get("paper_contract") or "")
    user_request = str(payload.get("user_request") or "")
    package = str(payload.get("manuscript_package") or "")
    evidence_status = _evidence_status(paper_contract)

    path_match = re.search(r"^MANUSCRIPT_PATH:\s*(.+)$", package, re.MULTILINE)
    references_match = re.search(r"^REFERENCES_PATH:\s*(.+)$", package, re.MULTILINE)
    raw_run_id = str(payload.get("meta_run_id") or "").strip()
    expected_manuscript: Path | None = None
    expected_references: Path | None = None
    if raw_run_id:
        if _SAFE_META_RUN_ID_RE.fullmatch(raw_run_id) is None:
            return "SANITIZER: block\nBLOCKER: invalid runtime-owned meta_run_id", 2
        workspace = Path.cwd().resolve()
        paper_root = workspace / "paper"
        if paper_root.is_symlink():
            return "SANITIZER: block\nBLOCKER: paper root must not be a symlink", 2
        if paper_root.exists() and (
            not paper_root.is_dir() or paper_root.resolve() != paper_root
        ):
            return "SANITIZER: block\nBLOCKER: paper root escapes the skill workspace", 2
        run_dir = paper_root / raw_run_id
        if run_dir.is_symlink():
            return "SANITIZER: block\nBLOCKER: paper run directory must not be a symlink", 2
        if run_dir.exists() and (
            not run_dir.is_dir() or run_dir.resolve() != run_dir
        ):
            return "SANITIZER: block\nBLOCKER: paper run directory escapes the workspace", 2
        expected_manuscript = run_dir / "paper.tex"
        expected_references = run_dir / "references.bib"
    if path_match:
        manuscript_path, path_error = _workspace_file(
            path_match.group(1).strip(),
            expected_path=expected_manuscript,
        )
        if manuscript_path is None:
            return f"SANITIZER: block\nBLOCKER: {path_error}", 2
        references_path: Path | None = None
        if references_match:
            references_path, references_error = _workspace_file(
                references_match.group(1).strip(),
                label="references",
                expected_path=expected_references,
            )
            if references_path is None:
                return f"SANITIZER: block\nBLOCKER: {references_error}", 2
        original = manuscript_path.read_text(encoding="utf-8", errors="replace")
        (
            sanitized,
            punctuation_repairs,
            evidence_repairs,
            redundant_input_repairs,
        ) = sanitize_manuscript(
            original,
            evidence_status=evidence_status,
            explicit_source_text=f"{paper_contract}\n{user_request}",
        )
        if sanitized != original:
            _atomic_write(manuscript_path, sanitized)
        lines = [
            "SANITIZER: pass",
            f"MANUSCRIPT_PATH: {manuscript_path}",
        ]
        if references_path is not None:
            lines.append(f"REFERENCES_PATH: {references_path}")
        lines.extend(
            (
                f"SAFE_PUNCTUATION_REPAIRS: {punctuation_repairs}",
                f"EVIDENCE_PLACEHOLDER_REPAIRS: {evidence_repairs}",
                f"REDUNDANT_PLACEHOLDER_INPUT_REPAIRS: {redundant_input_repairs}",
                f"EVIDENCE_STATUS: {evidence_status}",
            )
        )
        return "\n".join(lines), 0

    manuscript, bibliography = _package_parts(package)
    if not manuscript:
        return "SANITIZER: block\nBLOCKER: manuscript package contains no LaTeX artifact", 2

    (
        sanitized,
        punctuation_repairs,
        evidence_repairs,
        redundant_input_repairs,
    ) = sanitize_manuscript(
        manuscript,
        evidence_status=evidence_status,
        explicit_source_text=f"{paper_contract}\n{user_request}",
    )
    output = [
        "MANUSCRIPT_TEX:",
        sanitized,
        "REFERENCES_BIB:",
        bibliography or "% no verified references",
        "COMPILE_NOTES:",
        "- deterministic pre-gate LaTeX sanitization complete",
        f"- safe punctuation repairs: {punctuation_repairs}",
        f"- evidence placeholder repairs: {evidence_repairs}",
        f"- redundant placeholder input repairs: {redundant_input_repairs}",
        f"- evidence status: {evidence_status}",
    ]
    return "\n".join(output), 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"SANITIZER: block\nBLOCKER: invalid JSON input: {exc}")
        return 2
    if not isinstance(payload, dict):
        print("SANITIZER: block\nBLOCKER: input must be a JSON object")
        return 2
    try:
        output, returncode = sanitize_payload(payload)
    except (OSError, UnicodeError) as exc:
        print(f"SANITIZER: block\nBLOCKER: could not sanitize manuscript: {exc}")
        return 2
    print(output)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
