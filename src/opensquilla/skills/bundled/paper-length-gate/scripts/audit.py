"""Deterministic, artifact-backed LaTeX readiness checks."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from opensquilla.skills.paper_visibility import find_unsafe_text_visibility_controls

_MAX_TEX_BYTES = 5 * 1024 * 1024
_TARGET_RE = re.compile(
    r"^\s*TARGET_PAGES\s*:\s*(?:>=|≥|at\s+least\s+)?(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MODE_RE = re.compile(r"^\s*PAPER_MODE\s*:\s*([A-Z_]+)\s*$", re.MULTILINE)
_PATH_RE = re.compile(r"^\s*MANUSCRIPT_PATH\s*:\s*(.+?)\s*$", re.MULTILINE)
_INLINE_RE = re.compile(
    r"MANUSCRIPT_TEX\s*:\s*([\s\S]+?)(?:^\s*REFERENCES_BIB\s*:|"
    r"^\s*COMPILE_NOTES\s*:|\Z)",
    re.MULTILINE,
)
_SECTION_RE = re.compile(r"\\section\*?\s*\{([^{}]+)\}", re.IGNORECASE)
_CITE_RE = re.compile(
    r"\\cite[A-Za-z*]*\s*(?:\[[^\]]*\]\s*){0,2}\{([^{}]+)\}",
    re.IGNORECASE,
)
_INVISIBLE_CONDITIONAL_RE = re.compile(
    r"\\(?:if[A-Za-z@]*|else|fi)\b",
    re.IGNORECASE,
)
# These are delivery-oriented floors, not page-count estimates.  Title matter,
# floats, and the bibliography can move the compiled count, so compile_pdf
# remains authoritative.  The floor still has to scale with the target: the old
# fixed 600-unit compact floor accepted one-page drafts for the default
# four-page contract.
_ENGLISH_UNITS_PER_TARGET_PAGE = 500
_CJK_UNITS_PER_TARGET_PAGE = 900

_REQUIRED_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("introduction", ("introduction", "引言", "绪论")),
    (
        "related_work",
        (
            "related work",
            "background",
            "literature review",
            "相关工作",
            "研究现状",
            "文献综述",
        ),
    ),
    (
        "method",
        (
            "method",
            "methods",
            "methodology",
            "approach",
            "system model",
            "problem formulation",
            "architecture",
            "方法",
            "系统模型",
            "问题建模",
            "算法设计",
            "模型",
            "框架",
        ),
    ),
    (
        "evaluation",
        (
            "experiment",
            "experiments",
            "experimental evaluation",
            "performance evaluation",
            "evaluation",
            "results",
            "实验",
            "实验设计",
            "性能评价",
            "评估",
            "结果",
        ),
    ),
    ("discussion", ("discussion", "analysis", "讨论", "分析")),
    ("conclusion", ("conclusion", "conclusions", "结论", "总结")),
)


def _paper_contract(payload: dict[str, Any]) -> tuple[str, int | None, str]:
    contract = str(payload.get("paper_contract") or "")
    target_match = _TARGET_RE.search(contract)
    target = int(target_match.group(1)) if target_match else None
    mode_match = _MODE_RE.search(contract)
    mode = mode_match.group(1) if mode_match else ""
    return contract, target, mode


def _inline_tex(manifest: str) -> str:
    match = _INLINE_RE.search(manifest)
    if match:
        return match.group(1).strip()
    raw = re.search(r"(\\documentclass[\s\S]+?\\end\{document\})", manifest)
    return raw.group(1).strip() if raw else ""


def _read_manuscript(
    manifest: str,
    mode: str,
) -> tuple[str, str, int, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    path_match = _PATH_RE.search(manifest)
    if not path_match:
        inline = _inline_tex(manifest)
        if inline and mode == "COMPACT_SKELETON":
            warnings.append(
                "inline manuscript compatibility path used; compile_pdf will persist it"
            )
            return inline, "inline:MANUSCRIPT_TEX", len(inline.encode("utf-8")), blockers, warnings
        blockers.append("MANUSCRIPT_PATH is missing from the manuscript manifest")
        return "", "missing", 0, blockers, warnings

    raw_path = path_match.group(1).strip()
    if not raw_path or "\x00" in raw_path:
        blockers.append("MANUSCRIPT_PATH is empty or contains a NUL byte")
        return "", "invalid", 0, blockers, warnings

    workspace = Path.cwd().resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        blockers.append(f"MANUSCRIPT_PATH cannot be resolved: {exc}")
        return "", raw_path, 0, blockers, warnings

    if resolved != workspace and not resolved.is_relative_to(workspace):
        blockers.append("MANUSCRIPT_PATH escapes the active workspace")
    if resolved.suffix.lower() != ".tex":
        blockers.append("MANUSCRIPT_PATH must reference a .tex file")
    if not resolved.is_file():
        blockers.append("MANUSCRIPT_PATH does not reference a regular file")
    if blockers:
        return "", str(resolved), 0, blockers, warnings

    try:
        size = resolved.stat().st_size
    except OSError as exc:
        blockers.append(f"MANUSCRIPT_PATH cannot be inspected: {exc}")
        return "", str(resolved), 0, blockers, warnings
    if size <= 0:
        blockers.append("manuscript .tex file is empty")
        return "", str(resolved), size, blockers, warnings
    if size > _MAX_TEX_BYTES:
        blockers.append(f"manuscript .tex exceeds the {_MAX_TEX_BYTES}-byte safety limit")
        return "", str(resolved), size, blockers, warnings

    try:
        manuscript = resolved.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        blockers.append(f"manuscript .tex is not readable UTF-8 text: {exc}")
        return "", str(resolved), size, blockers, warnings
    if not manuscript.strip():
        blockers.append("manuscript .tex contains only whitespace")
    return manuscript, str(resolved), size, blockers, warnings


def _without_comments(text: str) -> str:
    """Remove TeX comments using backslash parity rather than one-char lookbehind."""

    visible_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        comment_at: int | None = None
        for index, character in enumerate(line):
            if character != "%":
                continue
            slash_count = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                slash_count += 1
                cursor -= 1
            if slash_count % 2 == 0:
                comment_at = index
                break
        visible_lines.append(line if comment_at is None else line[:comment_at] + "\n")
    return "".join(visible_lines)


def _content_units(text: str) -> tuple[int, int, int]:
    clean = _without_comments(text)
    body_match = re.search(
        r"\\begin\s*\{document\}([\s\S]*?)\\end\s*\{document\}",
        clean,
        re.IGNORECASE,
    )
    body = body_match.group(1) if body_match else clean
    body = re.sub(r"\\[A-Za-z@]+\*?", " ", body)
    body = re.sub(r"[{}\[\]$&]", " ", body)
    cjk_characters = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", body))
    word_tokens = len(re.findall(r"\b[A-Za-z0-9][A-Za-z0-9'’-]*\b", body))
    return cjk_characters + word_tokens, word_tokens, cjk_characters


def _repeated_prose_ratio(text: str) -> tuple[int, int, float]:
    clean = _without_comments(text)
    body_match = re.search(
        r"\\begin\s*\{document\}([\s\S]*?)\\end\s*\{document\}",
        clean,
        re.IGNORECASE,
    )
    body = body_match.group(1) if body_match else clean
    segments: list[str] = []
    for raw in re.split(r"\n+|(?<=[.!?。！？])\s+", body):
        plain = re.sub(r"\\[A-Za-z@]+\*?", " ", raw)
        plain = re.sub(r"[{}\[\]$&]", " ", plain)
        normalized = re.sub(r"\s+", " ", plain).strip().casefold()
        units = len(re.findall(r"\b[A-Za-z0-9][A-Za-z0-9'’-]*\b", normalized))
        units += len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", normalized))
        if units >= 8:
            segments.append(normalized)
    if not segments:
        return 0, 0, 0.0
    unique = len(set(segments))
    return len(segments), unique, (len(segments) - unique) / len(segments)


def _has_section(headings: list[str], aliases: tuple[str, ...]) -> bool:
    normalized = [re.sub(r"\s+", " ", heading.strip().casefold()) for heading in headings]
    return any(any(alias.casefold() in heading for alias in aliases) for heading in normalized)


def audit(payload: dict[str, Any]) -> dict[str, Any]:
    _contract, target, mode = _paper_contract(payload)
    manifest = str(payload.get("manuscript_package") or "")
    manuscript, source, byte_count, blockers, warnings = _read_manuscript(manifest, mode)

    if target is None:
        blockers.append("TARGET_PAGES must be a machine-readable integer")
        target_value = 0
    else:
        target_value = target
        if not 1 <= target <= 50:
            blockers.append(f"TARGET_PAGES must be between 1 and 50; got {target}")
    if mode not in {"FULL_MANUSCRIPT", "COMPACT_SKELETON"}:
        blockers.append("PAPER_MODE must be FULL_MANUSCRIPT or COMPACT_SKELETON")

    content_units = word_tokens = cjk_characters = 0
    headings: list[str] = []
    cite_keys: set[str] = set()
    citation_commands = 0
    required_found = 0
    prose_segments = unique_prose_segments = 0
    repeated_prose_ratio = 0.0
    required_total = len(_REQUIRED_SECTIONS) + 1  # named sections plus abstract
    if manuscript:
        clean = _without_comments(manuscript)
        invisible_conditionals = sorted(set(_INVISIBLE_CONDITIONAL_RE.findall(clean)))
        if invisible_conditionals:
            blockers.append(
                "manuscript contains TeX conditionals that can hide counted prose: "
                + ", ".join(invisible_conditionals[:8])
            )
        invisible_commands = find_unsafe_text_visibility_controls(clean)
        if invisible_commands:
            blockers.append(
                "manuscript contains commands that can make counted prose invisible: "
                + ", ".join(invisible_commands[:8])
            )
        if not re.search(r"\\documentclass(?:\[[^\]]*\])?\s*\{[^{}]+\}", clean):
            blockers.append("manuscript is missing \\documentclass")
        if not re.search(r"\\begin\s*\{document\}", clean):
            blockers.append("manuscript is missing \\begin{document}")
        if not re.search(r"\\end\s*\{document\}", clean):
            blockers.append("manuscript is missing \\end{document}")

        headings = _SECTION_RE.findall(clean)
        abstract_found = bool(
            re.search(r"\\begin\s*\{abstract\}", clean, re.IGNORECASE)
            and re.search(r"\\end\s*\{abstract\}", clean, re.IGNORECASE)
        )
        required_found = int(abstract_found)
        if not abstract_found:
            blockers.append("required section missing: abstract")
        for name, aliases in _REQUIRED_SECTIONS:
            if _has_section(headings, aliases):
                required_found += 1
            else:
                blockers.append(f"required section missing: {name}")

        cite_groups = _CITE_RE.findall(clean)
        citation_commands = len(cite_groups)
        for group in cite_groups:
            cite_keys.update(key.strip() for key in group.split(",") if key.strip())
        if not cite_keys:
            blockers.append("manuscript contains no LaTeX citation keys")

        content_units, word_tokens, cjk_characters = _content_units(clean)
        prose_segments, unique_prose_segments, repeated_prose_ratio = (
            _repeated_prose_ratio(clean)
        )
        if prose_segments >= 8 and repeated_prose_ratio >= 0.35:
            blockers.append(
                "manuscript contains excessive repeated prose: "
                f"{repeated_prose_ratio:.0%} duplicate substantive segments"
            )
        cjk_dominant = cjk_characters > word_tokens
        units_per_page = (
            _CJK_UNITS_PER_TARGET_PAGE
            if cjk_dominant
            else _ENGLISH_UNITS_PER_TARGET_PAGE
        )
        base_floor = 1200 if mode == "FULL_MANUSCRIPT" else 700
        minimum_units = max(base_floor, target_value * units_per_page)
        recommended_units = max(minimum_units, target_value * (units_per_page + 100))
        if content_units < minimum_units:
            blockers.append(
                "manuscript body is below target-correlated readiness floor: "
                f"{content_units}/{minimum_units} content units for "
                f"{target_value} target pages"
            )
        elif content_units < recommended_units:
            warnings.append(
                f"body scale is below the pre-compile recommendation: "
                f"{content_units}/{recommended_units} content units; "
                "compiled page count is authoritative"
            )

    verdict = "block" if blockers else ("warn" if warnings else "pass")
    return {
        "verdict": verdict,
        "target_pages": target_value,
        "mode": mode or "missing",
        "source": source,
        "bytes": byte_count,
        "content_units": content_units,
        "minimum_content_units": minimum_units if manuscript else 0,
        "word_tokens": word_tokens,
        "cjk_characters": cjk_characters,
        "prose_segments": prose_segments,
        "unique_prose_segments": unique_prose_segments,
        "repeated_prose_ratio": repeated_prose_ratio,
        "section_count": len(headings),
        "required_found": required_found,
        "required_total": required_total,
        "citation_commands": citation_commands,
        "distinct_cite_keys": len(cite_keys),
        "blockers": blockers,
        "warnings": warnings,
    }


def _render(result: dict[str, Any]) -> str:
    lines = [
        f"LENGTH_GATE: {result['verdict']}",
        f"TARGET_PAGES: {result['target_pages']}",
        "PAGE_COUNT_AUTHORITY: compile_pdf/pypdf",
        f"PAPER_MODE: {result['mode']}",
        f"MANUSCRIPT_SOURCE: {result['source']}",
        f"MANUSCRIPT_BYTES: {result['bytes']}",
        f"ESTIMATED_CONTENT_UNITS: {result['content_units']}",
        f"MINIMUM_CONTENT_UNITS: {result['minimum_content_units']}",
        f"ESTIMATED_WORDS: {result['word_tokens']}",
        f"CJK_CHARACTERS: {result['cjk_characters']}",
        f"PROSE_SEGMENTS: {result['prose_segments']}",
        f"UNIQUE_PROSE_SEGMENTS: {result['unique_prose_segments']}",
        f"REPEATED_PROSE_RATIO: {result['repeated_prose_ratio']:.3f}",
        f"SECTION_COUNT: {result['section_count']}",
        f"REQUIRED_SECTIONS: {result['required_found']}/{result['required_total']}",
        f"CITATION_COMMANDS: {result['citation_commands']}",
        f"DISTINCT_CITE_KEYS: {result['distinct_cite_keys']}",
        "BLOCKERS:",
        *(f"- {item}" for item in result["blockers"] or ["none"]),
        "WARNINGS:",
        *(f"- {item}" for item in result["warnings"] or ["none"]),
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        payload = {}
        rendered = _render(
            {
                "verdict": "block",
                "target_pages": 0,
                "mode": "missing",
                "source": "missing",
                "bytes": 0,
                "content_units": 0,
                "minimum_content_units": 0,
                "word_tokens": 0,
                "cjk_characters": 0,
                "prose_segments": 0,
                "unique_prose_segments": 0,
                "repeated_prose_ratio": 0.0,
                "section_count": 0,
                "required_found": 0,
                "required_total": len(_REQUIRED_SECTIONS) + 1,
                "citation_commands": 0,
                "distinct_cite_keys": 0,
                "blockers": [f"invalid JSON input: {exc}"],
                "warnings": [],
            }
        )
        print(rendered)
        print(rendered, file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        payload = {}
        payload_error = "input must be a JSON object"
    else:
        payload_error = ""
    result = audit(payload)
    if payload_error:
        result["blockers"].insert(0, payload_error)
        result["verdict"] = "block"
    rendered = _render(result)
    print(rendered)
    if result["verdict"] == "block":
        if payload.get("report_only") is True:
            return 0
        print(rendered, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
