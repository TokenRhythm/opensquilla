"""Cross-platform artifact operations for ``meta-paper-write``."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from opensquilla.skills.paper_visibility import find_unsafe_text_visibility_controls

_SAFE_META_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SECTION_NAMES = (
    "abstract",
    "introduction",
    "related_work",
    "method",
    "experiments",
    "discussion",
    "conclusion",
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_XETEX_CJK_LINEBREAK_LINES = (
    r'\XeTeXlinebreaklocale "zh"',
    r"\XeTeXlinebreakskip = 0pt plus 1pt",
)
_XETEX_CJK_LINEBREAK_COMMAND_RE = re.compile(
    r"^[ \t]*\\XeTeXlinebreak(?:locale|skip)\b[^\r\n]*(?:\r?\n|\Z)",
    re.MULTILINE,
)
_LENGTH_REPAIR_IDS = frozenset({"precompile", "page-shortfall"})
_MAX_LENGTH_EXPANSION_BYTES = 96 * 1024
_MIN_VISIBLE_PAGE_UNITS = 20
_MIN_SUBSTANTIVE_PAGE_UNITS = 80
_REFERENCE_HEADING_RE = re.compile(
    r"^(?:references|bibliography|参考文献|参考资料)\s*$",
    re.IGNORECASE,
)

class PaperArtifactError(RuntimeError):
    """Raised when an artifact operation fails its deterministic contract."""


def _without_tex_comments(text: str) -> str:
    """Remove TeX comments without treating escaped percent signs as comments."""

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


def _reject_invisible_text_controls(tex: str) -> None:
    controls = find_unsafe_text_visibility_controls(_without_tex_comments(tex))
    if controls:
        raise PaperArtifactError(
            "COMPILE_FAILED: manuscript contains TeX text-visibility controls: "
            + ", ".join(controls[:8])
        )


def _text(payload: Mapping[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise PaperArtifactError(f"PAPER_ARTIFACT_RUNTIME_FAILED: {key} must be text")
    return value


def _boolean(payload: Mapping[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise PaperArtifactError(f"PAPER_ARTIFACT_RUNTIME_FAILED: {key} must be boolean")
    return value


def _compile_input_fingerprint(tex: str, bibliography: str) -> str:
    digest = hashlib.sha256()
    digest.update(tex.encode("utf-8"))
    digest.update(b"\0REFERENCES\0")
    digest.update(bibliography.encode("utf-8"))
    return digest.hexdigest()


def _validated_run_id(payload: Mapping[str, Any], prefix: str) -> str:
    run_id = _text(payload, "meta_run_id").strip()
    if _SAFE_META_RUN_ID_RE.fullmatch(run_id) is None:
        raise PaperArtifactError(f"{prefix}: invalid runtime-owned meta_run_id")
    return run_id


def _paper_run_dir(
    payload: Mapping[str, Any],
    *,
    prefix: str,
    create: bool,
) -> Path:
    run_id = _validated_run_id(payload, prefix)
    workspace = Path.cwd().resolve()
    paper_root = workspace / "paper"
    if paper_root.is_symlink():
        raise PaperArtifactError(f"{prefix}: paper root must not be a symlink")
    if create:
        paper_root.mkdir(exist_ok=True)
    if paper_root.exists() and (
        not paper_root.is_dir() or paper_root.resolve() != paper_root
    ):
        raise PaperArtifactError(f"{prefix}: paper root escaped the workspace")

    paper_dir = paper_root / run_id
    if paper_dir.is_symlink():
        raise PaperArtifactError(f"{prefix}: paper run directory must not be a symlink")
    if create:
        paper_dir.mkdir(exist_ok=True)
    if paper_dir.exists() and (
        not paper_dir.is_dir() or paper_dir.resolve() != paper_dir
    ):
        raise PaperArtifactError(f"{prefix}: paper run directory escaped the workspace")
    return paper_dir


def _clean_latex_fence(text: str, *, multiline: bool = False) -> str:
    flags = re.MULTILINE if multiline else 0
    text = re.sub(r"^```(?:latex|tex)?\s*\n", "", text, flags=flags)
    return re.sub(r"\n```\s*$", "", text).strip()


def _scrub_placeholder_table_cells(tex: str) -> str:
    """Scrub numeric-looking data cells from placeholder tables."""

    numeric = re.compile(
        r"^\s*(?:\\textbf\{)?[-+]?\d[\d,]*(?:\.\d+)?\s*"
        r"(?:%|ms|s|x|MB|GB|points?)?(?:\})?\s*$",
        re.IGNORECASE,
    )
    output: list[str] = []
    in_tabular = False
    after_midrule = False
    for line in tex.splitlines():
        if r"\begin{tabular}" in line:
            in_tabular = True
            after_midrule = False
            output.append(line)
            continue
        if in_tabular and r"\end{tabular}" in line:
            in_tabular = False
            after_midrule = False
            output.append(line)
            continue
        if in_tabular and r"\midrule" in line:
            after_midrule = True
            output.append(line)
            continue
        if in_tabular and after_midrule and "&" in line and r"\bottomrule" not in line:
            suffix = r" \\" if line.rstrip().endswith(r"\\") else ""
            row = line.rstrip()
            if suffix:
                row = row[:-2].rstrip()
            cells = [cell.strip() for cell in row.split("&")]
            if len(cells) > 1:
                cells = [
                    cells[0],
                    *("---" if numeric.match(cell) else cell for cell in cells[1:]),
                ]
                indent_match = re.match(r"^\s*", line)
                indent = indent_match.group(0) if indent_match else ""
                line = indent + " & ".join(cells) + suffix
        output.append(line)
    return "\n".join(output)


def persist_sections(payload: Mapping[str, Any]) -> str:
    """Persist generated section fragments and return a compact manifest."""

    paper_dir = _paper_run_dir(payload, prefix="PERSIST_SECTIONS_FAILED", create=True)
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, Mapping):
        raise PaperArtifactError("PERSIST_SECTIONS_FAILED: sections must be an object")
    sections = {name: _text(raw_sections, name) for name in _SECTION_NAMES}

    out_dir = paper_dir / "sections"
    if out_dir.is_symlink():
        raise PaperArtifactError(
            "PERSIST_SECTIONS_FAILED: sections directory must not be a symlink"
        )
    out_dir.mkdir(exist_ok=True)
    if not out_dir.is_dir() or out_dir.resolve() != out_dir:
        raise PaperArtifactError(
            "PERSIST_SECTIONS_FAILED: sections directory escaped the paper run"
        )

    lines = ["SECTION_ARTIFACTS:"]
    total = 0
    for name, text in sections.items():
        body = _clean_latex_fence(text, multiline=True)
        path = out_dir / f"{name}.tex"
        if path.is_symlink():
            raise PaperArtifactError(
                f"PERSIST_SECTIONS_FAILED: section path is a symlink: {name}"
            )
        path.write_text(body, encoding="utf-8")
        chars = len(body)
        total += chars
        first_line = next(
            (line.strip() for line in body.splitlines() if line.strip()),
            "",
        )
        lines.append(
            f"- {name}: path={path.as_posix()} chars={chars} "
            f"first_line={first_line[:120]!r}"
        )
    lines.extend(
        (
            f"TOTAL_SECTION_CHARS: {total}",
            "CONTEXT_POLICY: downstream steps must read section files from disk "
            "and pass only paths/summaries to LLM prompts",
        )
    )
    return "\n".join(lines)


def _latex_escape(text: str) -> str:
    text = text.replace("\\", r"\textbackslash{}")
    for character in "&%$#_{}":
        text = text.replace(character, "\\" + character)
    return text.replace("~", r"\textasciitilde{}").replace(
        "^", r"\textasciicircum{}"
    )


def _ensure_xetex_cjk_line_breaking(tex: str) -> str:
    """Install a dependency-free, deterministic CJK line-breaking preamble.

    The managed TinyTeX closure includes XeTeX and ``fontspec`` but deliberately
    does not fetch optional packages at runtime. XeTeX's built-in locale and
    glue primitives provide natural line-break opportunities between CJK
    characters without ``xeCJK``, ``ctex``, or a ``tlmgr`` network update.
    Place the canonical settings at the end of the preamble so a generated
    manuscript cannot accidentally leave an earlier incompatible setting in
    effect.
    """

    if _CJK_RE.search(tex) is None:
        return tex
    begin_document = tex.find(r"\begin{document}")
    if begin_document < 0:
        raise PaperArtifactError(
            "COMPILE_FAILED: CJK manuscript found but LaTeX preamble is missing"
        )
    preamble = _XETEX_CJK_LINEBREAK_COMMAND_RE.sub("", tex[:begin_document])
    if preamble and not preamble.endswith("\n"):
        preamble += "\n"
    block = "\n".join(_XETEX_CJK_LINEBREAK_LINES) + "\n"
    return preamble + block + tex[begin_document:]


def assemble_manuscript_tex(payload: Mapping[str, Any]) -> str:
    """Assemble persisted section fragments into the run-owned manuscript."""

    paper_dir = _paper_run_dir(payload, prefix="ASSEMBLE_FAILED", create=True)
    section_dir = paper_dir / "sections"
    if (
        section_dir.is_symlink()
        or not section_dir.is_dir()
        or section_dir.resolve() != section_dir
    ):
        raise PaperArtifactError("ASSEMBLE_FAILED: sections directory escaped the paper run")
    sections = {name: section_dir / f"{name}.tex" for name in _SECTION_NAMES}
    if any(path.is_symlink() for path in sections.values()):
        raise PaperArtifactError("ASSEMBLE_FAILED: section files must not be symlinks")
    section_text = {
        name: path.read_text(encoding="utf-8") if path.is_file() else ""
        for name, path in sections.items()
    }

    bib = _text(payload, "bib_text").strip()
    writing_plan = _text(payload, "writing_plan")
    topic_fallback = _text(payload, "topic", "Untitled Manuscript")
    title_match = re.search(
        r"^\s*TITLE\s*:\s*(.+?)\s*$",
        writing_plan,
        re.MULTILINE,
    )
    raw_title = (
        title_match.group(1).strip() if title_match else topic_fallback
    ) or topic_fallback
    title_tex = _latex_escape(raw_title)
    any_cjk = _CJK_RE.search(raw_title) is not None or any(
        _CJK_RE.search(value) for value in section_text.values()
    )
    preamble = [
        r"\documentclass{article}",
        r"\usepackage{fontspec}" if any_cjk else r"% no CJK",
        r"\setmainfont[FontIndex=2]{NotoSansCJK-Regular.ttc}"
        if any_cjk
        else r"% no CJK font",
        *(_XETEX_CJK_LINEBREAK_LINES if any_cjk else ()),
        r"\usepackage{graphicx}",
        r"\usepackage{booktabs}",
        r"\usepackage{amsmath,amssymb}",
        r"\usepackage{algorithm}",
        r"\usepackage{algorithmic}",
        r"\usepackage[hidelinks]{hyperref}",
        r"\usepackage{geometry}",
        r"\geometry{margin=2.5cm}",
        r"\title{" + title_tex + r"}",
        r"\author{OpenSquilla meta-paper-write}",
        r"\date{\today}",
        r"\begin{document}",
        r"\maketitle",
    ]
    body_parts = [section_text[name] for name in _SECTION_NAMES]
    tail = [
        r"\bibliographystyle{plain}",
        r"\bibliography{references}",
        r"\end{document}",
    ]
    tex = (
        "\n".join(preamble)
        + "\n\n"
        + "\n\n".join(part for part in body_parts if part)
        + "\n\n"
        + "\n".join(tail)
    )
    tex = _scrub_placeholder_table_cells(tex)
    tex_path = paper_dir / "paper.tex"
    bib_path = paper_dir / "references.bib"
    if tex_path.is_symlink() or bib_path.is_symlink():
        raise PaperArtifactError("ASSEMBLE_FAILED: output files must not be symlinks")
    tex_path.write_text(tex, encoding="utf-8")
    bib_path.write_text(bib if bib else "% no verified references", encoding="utf-8")
    present = ", ".join(name for name, value in section_text.items() if value)
    return "\n".join(
        (
            f"MANUSCRIPT_PATH: {tex_path.resolve()}",
            f"REFERENCES_PATH: {bib_path.resolve()}",
            f"MANUSCRIPT_CHARS: {len(tex)}",
            f"REFERENCES_CHARS: {len(bib)}",
            "COMPILE_NOTES:",
            "- assembled section-by-section via paper-section-author",
            f"- sections present: {present}",
            f"- total section chars: {sum(len(value) for value in section_text.values())}",
            "- context policy: full manuscript persisted on disk; downstream prompts "
            "should use path/summary only",
        )
    )


def citation_map(payload: Mapping[str, Any]) -> str:
    """Build the deterministic citation provenance table from run artifacts."""

    run_dir = _paper_run_dir(payload, prefix="CITATION_MAP_FAILED", create=False)
    expected_tex = run_dir / "paper.tex"
    expected_bib = run_dir / "references.bib"
    if expected_tex.is_symlink() or expected_bib.is_symlink():
        raise PaperArtifactError("CITATION_MAP_FAILED: artifact files must not be symlinks")

    manifest = _text(payload, "manifest")
    manuscript_match = re.search(r"MANUSCRIPT_PATH:\s*(.+)", manifest)
    references_match = re.search(r"REFERENCES_PATH:\s*(.+)", manifest)
    tex_path = (
        Path(manuscript_match.group(1).strip()).resolve()
        if manuscript_match
        else expected_tex
    )
    bib_path = (
        Path(references_match.group(1).strip()).resolve()
        if references_match
        else expected_bib
    )
    if tex_path != expected_tex or bib_path != expected_bib:
        raise PaperArtifactError(
            "CITATION_MAP_FAILED: manifest path does not match this meta run"
        )

    tex = (
        tex_path.read_text(encoding="utf-8", errors="ignore")
        if tex_path.is_file()
        else ""
    )
    bib = (
        bib_path.read_text(encoding="utf-8", errors="ignore")
        if bib_path.is_file()
        else _text(payload, "refbib")
    )
    cite_counts: dict[str, int] = {}
    for group in re.findall(r"\\cite\{([^}]+)\}", tex):
        for key in (value.strip() for value in group.split(",")):
            if key:
                cite_counts[key] = cite_counts.get(key, 0) + 1

    entries: dict[str, dict[str, str]] = {}
    for match in re.finditer(
        r"@\w+\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@\w+\s*\{|\Z)",
        bib,
        re.DOTALL,
    ):
        key = match.group(1).strip()
        body = match.group(2)
        title = re.search(r"title\s*=\s*[\{\"]([^}\"]+)", body, re.IGNORECASE)
        url = re.search(
            r"(?:url|howpublished)\s*=\s*[\{\"]([^}\"]+)",
            body,
            re.IGNORECASE,
        )
        doi = re.search(r"doi\s*=\s*[\{\"]([^}\"]+)", body, re.IGNORECASE)
        eprint = re.search(r"eprint\s*=\s*[\{\"]([^}\"]+)", body, re.IGNORECASE)
        locator = (url.group(1) if url else "")
        if not locator and doi:
            locator = f"doi:{doi.group(1)}"
        if not locator and eprint:
            locator = f"arXiv:{eprint.group(1)}"
        entries[key] = {
            "title": title.group(1).strip() if title else "",
            "locator": locator,
        }

    strong_domains = (
        "arxiv.org",
        "aclanthology.org",
        "dl.acm.org",
        "openreview.net",
        "ieee.org",
        "nature.com",
        "science.org",
        "biorxiv.org",
        "pnas.org",
    )
    weak_markers = (
        "medium.com",
        "wikipedia.org",
        "github.com",
        "stackoverflow.com",
        "twitter.com",
        "x.com",
    )

    def quality(locator: str, *, invalid: bool = False) -> str:
        lowered = locator.lower()
        if invalid:
            return "INVALID"
        if (
            any(domain in lowered for domain in strong_domains)
            or "doi:" in lowered
            or "arxiv:" in lowered
        ):
            return "STRONG"
        if any(marker in lowered for marker in weak_markers):
            return "WEAK"
        return "OK" if locator else "WEAK"

    lines = [
        "CITATION_MAP:",
        "",
        "| Cite Key | Cited Times | Title | URL / DOI / arXiv | Source Quality |",
        "|---|---:|---|---|---|",
    ]
    invalid = weak = strong = ok = unused = 0
    for key in sorted(set(cite_counts) | set(entries)):
        count = cite_counts.get(key, 0)
        entry = entries.get(key)
        invalid_row = entry is None
        source_quality = quality(
            entry["locator"] if entry else "",
            invalid=invalid_row,
        )
        if invalid_row:
            invalid += 1
        elif count == 0:
            unused += 1
            source_quality = "UNUSED"
        elif source_quality == "STRONG":
            strong += 1
        elif source_quality == "OK":
            ok += 1
        elif source_quality == "WEAK":
            weak += 1
        title = entry["title"] if entry else "(MISSING IN BIB)"
        locator = entry["locator"] if entry else "-"
        lines.append(f"| {key} | {count} | {title} | {locator} | {source_quality} |")
    lines.extend(
        (
            "",
            f"SUMMARY: total_cite_keys={len(cite_counts)}, strong={strong}, "
            f"ok={ok}, weak={weak}, invalid={invalid}, unused={unused}",
            f"ARTIFACTS: manuscript={tex_path} references={bib_path}",
        )
    )
    return "\n".join(lines)


def _extract_compile_inputs(
    payload: Mapping[str, Any],
    paper: Path,
) -> tuple[str, str, int]:
    package = _text(payload, "manuscript_package")
    paper_contract = _text(payload, "paper_contract")
    target_match = re.search(
        r"^\s*TARGET_PAGES\s*:\s*(\d+)\s*$",
        paper_contract,
        re.MULTILINE,
    )
    if not target_match:
        raise PaperArtifactError("COMPILE_FAILED: TARGET_PAGES missing from paper contract")
    target_pages = int(target_match.group(1))
    if not 1 <= target_pages <= 50:
        raise PaperArtifactError(
            f"COMPILE_FAILED: TARGET_PAGES must be between 1 and 50; got {target_pages}"
        )

    manuscript_match = re.search(
        r"MANUSCRIPT_TEX:\s*(.+?)(?:REFERENCES_BIB:|COMPILE_NOTES:|\Z)",
        package,
        re.DOTALL,
    )
    tex_body = manuscript_match.group(1).strip() if manuscript_match else ""
    bibliography_match = re.search(
        r"REFERENCES_BIB:\s*(.+?)(?:COMPILE_NOTES:|\Z)",
        package,
        re.DOTALL,
    )
    bibliography = bibliography_match.group(1).strip() if bibliography_match else ""

    if not tex_body:
        fenced = re.search(
            r"```(?:latex|tex)?\s*(\\documentclass[\s\S]+?\\end\{document\})",
            package,
        )
        if fenced:
            tex_body = fenced.group(1).strip()
    if not tex_body:
        raw = re.search(r"(\\documentclass[\s\S]+?\\end\{document\})", package)
        if raw:
            tex_body = raw.group(1).strip()

    expected_tex = paper / "paper.tex"
    expected_bib = paper / "references.bib"
    if not tex_body:
        path_match = re.search(r"MANUSCRIPT_PATH:\s*(.+)", package)
        bib_path_match = re.search(r"REFERENCES_PATH:\s*(.+)", package)
        if path_match:
            manuscript_path = Path(path_match.group(1).strip()).resolve()
            if manuscript_path != expected_tex:
                raise PaperArtifactError(
                    "COMPILE_FAILED: manuscript path does not match this meta run"
                )
            if manuscript_path.is_file():
                tex_body = manuscript_path.read_text(encoding="utf-8")
        if bib_path_match:
            bibliography_path = Path(bib_path_match.group(1).strip()).resolve()
            if bibliography_path != expected_bib:
                raise PaperArtifactError(
                    "COMPILE_FAILED: references path does not match this meta run"
                )
            if bibliography_path.is_file():
                bibliography = bibliography_path.read_text(encoding="utf-8")

    tex_body = _clean_latex_fence(tex_body)
    if not tex_body:
        raise PaperArtifactError(
            "COMPILE_FAILED: MANUSCRIPT_TEX block missing; refusing to create degraded PDF\n"
            f"PACKAGE_PREVIEW:\n{package[:2000]}"
        )
    return tex_body, bibliography, target_pages


def _length_expansion_fragment(raw: str) -> str:
    """Extract one bounded, body-only LaTeX expansion fragment.

    The LLM proposes prose, while this runtime owns all file mutation.  Keeping
    the fragment body-only preserves the existing preamble, bibliography, cite
    map, and document boundaries across both preflight and page-shortfall
    repairs.
    """

    match = re.search(
        r"%\s*BEGIN_LENGTH_EXPANSION\s*\n([\s\S]*?)"
        r"\n%\s*END_LENGTH_EXPANSION",
        raw,
        re.IGNORECASE,
    )
    if match is None:
        raise PaperArtifactError(
            "LENGTH_EXPANSION_FAILED: expansion markers are missing or incomplete"
        )
    fragment = match.group(1).strip()
    if not fragment:
        raise PaperArtifactError("LENGTH_EXPANSION_FAILED: expansion fragment is empty")
    if len(fragment.encode("utf-8")) > _MAX_LENGTH_EXPANSION_BYTES:
        raise PaperArtifactError(
            "LENGTH_EXPANSION_FAILED: expansion exceeds the 96-KiB safety limit"
        )
    forbidden = re.search(
        r"\\(?:documentclass|usepackage|begin\s*\{document\}|"
        r"end\s*\{document\}|bibliography|addbibresource|input|include|"
        r"write18|openout|read|includegraphics|cite[A-Za-z*]*|newpage|"
        r"clearpage|pagebreak|vspace|hspace|addvspace|linespread|fontsize|"
        r"enlargethispage)\b",
        fragment,
        re.IGNORECASE,
    )
    if forbidden:
        raise PaperArtifactError(
            "LENGTH_EXPANSION_FAILED: body fragment contains forbidden command "
            f"{forbidden.group(0)}"
        )
    if re.search(r"\\section\*?\s*\{", fragment, re.IGNORECASE):
        raise PaperArtifactError(
            "LENGTH_EXPANSION_FAILED: use subsection-level prose; top-level sections are locked"
        )
    content = re.sub(r"\\[A-Za-z@]+\*?", " ", fragment)
    content = re.sub(r"[{}\[\]$&]", " ", content)
    english_tokens = re.findall(r"\b[A-Za-z0-9][A-Za-z0-9'’-]*\b", content)
    units = len(english_tokens) + len(_CJK_RE.findall(content))
    if units < 180:
        raise PaperArtifactError(
            "LENGTH_EXPANSION_FAILED: expansion must contain at least 180 substantive units"
        )
    if len(english_tokens) >= 180:
        lexical_diversity = len({token.casefold() for token in english_tokens}) / len(
            english_tokens
        )
        if lexical_diversity < 0.08:
            raise PaperArtifactError(
                "LENGTH_EXPANSION_FAILED: expansion contains excessive repeated filler"
            )
    return fragment


def materialize_manuscript(payload: Mapping[str, Any]) -> str:
    """Persist an inline compact package into the run-owned artifact paths."""

    paper = _paper_run_dir(payload, prefix="MATERIALIZE_FAILED", create=True)
    tex_body, bibliography, _target_pages = _extract_compile_inputs(payload, paper)
    tex_body = _prepare_tex(tex_body)
    tex_path = paper / "paper.tex"
    bib_path = paper / "references.bib"
    if tex_path.is_symlink() or bib_path.is_symlink():
        raise PaperArtifactError("MATERIALIZE_FAILED: paper files must not be symlinks")
    tex_path.write_text(tex_body, encoding="utf-8")
    bib_path.write_text(bibliography, encoding="utf-8")
    return "\n".join(
        (
            f"MANUSCRIPT_PATH: {tex_path.resolve()}",
            f"REFERENCES_PATH: {bib_path.resolve()}",
            f"MANUSCRIPT_CHARS: {len(tex_body)}",
            f"REFERENCES_CHARS: {len(bibliography)}",
            "MATERIALIZED: yes",
        )
    )


def apply_length_expansion(payload: Mapping[str, Any]) -> str:
    """Materialize and append one idempotent, bounded manuscript expansion."""

    paper = _paper_run_dir(payload, prefix="LENGTH_EXPANSION_FAILED", create=True)
    repair_id = _text(payload, "repair_id").strip()
    if repair_id not in _LENGTH_REPAIR_IDS:
        raise PaperArtifactError("LENGTH_EXPANSION_FAILED: invalid repair_id")
    tex_body, bibliography, _target_pages = _extract_compile_inputs(payload, paper)
    tex_body = _prepare_tex(tex_body)
    fragment = _length_expansion_fragment(_text(payload, "expansion"))
    start_marker = f"% BEGIN OPENSQUILLA LENGTH EXPANSION {repair_id}"
    end_marker = f"% END OPENSQUILLA LENGTH EXPANSION {repair_id}"
    inserted = start_marker not in tex_body
    if inserted:
        block = f"{start_marker}\n{fragment}\n{end_marker}\n"
        insertion_match = re.search(
            r"(?=\\section\*?\s*\{(?:Conclusion|Conclusions|结论|总结)\})",
            tex_body,
            re.IGNORECASE,
        )
        if insertion_match is None:
            insertion_match = re.search(
                r"(?=\\bibliographystyle|\\bibliography|\\end\s*\{document\})",
                tex_body,
                re.IGNORECASE,
            )
        if insertion_match is None:
            raise PaperArtifactError(
                "LENGTH_EXPANSION_FAILED: manuscript has no safe body insertion point"
            )
        index = insertion_match.start()
        tex_body = tex_body[:index] + block + tex_body[index:]

    tex_path = paper / "paper.tex"
    bib_path = paper / "references.bib"
    if tex_path.is_symlink() or bib_path.is_symlink():
        raise PaperArtifactError("LENGTH_EXPANSION_FAILED: paper files must not be symlinks")
    tex_path.write_text(tex_body, encoding="utf-8")
    bib_path.write_text(bibliography, encoding="utf-8")
    return "\n".join(
        (
            f"MANUSCRIPT_PATH: {tex_path.resolve()}",
            f"REFERENCES_PATH: {bib_path.resolve()}",
            f"LENGTH_EXPANSION_ID: {repair_id}",
            f"LENGTH_EXPANSION_APPLIED: {'yes' if inserted else 'already-present'}",
            f"MANUSCRIPT_CHARS: {len(tex_body)}",
        )
    )


def _prepare_tex(tex_body: str) -> str:
    if r"\documentclass" not in tex_body:
        tex_body = (
            r"\documentclass{article}"
            "\n"
            r"\usepackage{fontspec}"
            "\n"
            r"\setmainfont[FontIndex=2]{NotoSansCJK-Regular.ttc}"
            "\n"
            r"\usepackage{graphicx}\usepackage{booktabs}\usepackage{amsmath}"
            "\n"
            r"\usepackage{algorithm}\usepackage{algorithmic}"
            "\n"
            r"\usepackage[hidelinks]{hyperref}"
            "\n"
            r"\begin{document}"
            "\n"
            + tex_body
            + "\n"
            + r"\bibliographystyle{plain}"
            + "\n"
            + r"\bibliography{references}"
            + "\n"
            + r"\end{document}"
            + "\n"
        )

    tex_body = tex_body.replace(r"\usepackage{xeCJK}", r"\usepackage{fontspec}")
    tex_body = re.sub(
        r"\\setCJKmainfont\{[^}]+\}",
        r"\\setmainfont[FontIndex=2]{NotoSansCJK-Regular.ttc}",
        tex_body,
    )
    if _CJK_RE.search(tex_body) and "fontspec" not in tex_body:
        tex_body = tex_body.replace(
            r"\documentclass{article}",
            r"\documentclass{article}" + "\n" + r"\usepackage{fontspec}",
            1,
        )
    if _CJK_RE.search(tex_body) and "setmainfont" not in tex_body:
        tex_body = tex_body.replace(
            r"\usepackage{fontspec}",
            r"\usepackage{fontspec}"
            + "\n"
            + r"\setmainfont[FontIndex=2]{NotoSansCJK-Regular.ttc}",
            1,
        )
    tex_body = tex_body.replace(
        r"\usepackage{hyperref}",
        r"\usepackage[hidelinks]{hyperref}",
        1,
    )

    algorithm_packages: list[str] = []
    if r"\begin{algorithm}" in tex_body and r"\usepackage{algorithm}" not in tex_body:
        algorithm_packages.append(r"\usepackage{algorithm}")
    if (
        r"\begin{algorithmic}" in tex_body
        and r"\usepackage{algorithmic}" not in tex_body
        and r"\usepackage{algpseudocode}" not in tex_body
    ):
        legacy_commands = re.search(
            r"\\(?:STATE|FOR|FORALL|ENDFOR|IF|ELSE|ENDIF|WHILE|ENDWHILE|"
            r"REQUIRE|ENSURE)\b",
            tex_body,
        )
        algorithm_packages.append(
            r"\usepackage{algorithmic}"
            if legacy_commands
            else r"\usepackage{algpseudocode}"
        )
    if algorithm_packages:
        insertion = "\n".join(algorithm_packages) + "\n"
        begin_document = tex_body.find(r"\begin{document}")
        if begin_document < 0:
            raise PaperArtifactError(
                "COMPILE_FAILED: algorithm environment found but preamble is missing"
            )
        tex_body = tex_body[:begin_document] + insertion + tex_body[begin_document:]
    tex_body = _ensure_xetex_cjk_line_breaking(tex_body)
    return _scrub_placeholder_table_cells(tex_body)


def _compile_commands(paper: Path) -> tuple[list[str], str]:
    logs: list[str] = []
    final_xelatex_output = ""
    compile_env = dict(os.environ)
    compile_env.update(
        {
            "openin_any": "p",
            "openout_any": "p",
            "TEXINPUTS": f".{os.pathsep}",
            "BIBINPUTS": f".{os.pathsep}",
            "BSTINPUTS": f".{os.pathsep}",
        }
    )
    commands = (
        [
            "xelatex",
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "paper.tex",
        ],
        ["bibtex", "paper"],
        [
            "xelatex",
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "paper.tex",
        ],
        [
            "xelatex",
            "-no-shell-escape",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "paper.tex",
        ],
    )
    deadline = time.monotonic() + 110.0
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PaperArtifactError(
                "COMPILE_FAILED: TeX command sequence exceeded the 110-second compile budget"
            )
        try:
            result = subprocess.run(  # noqa: S603 - fixed internal argv, no shell.
                command,
                cwd=paper,
                capture_output=True,
                text=True,
                env=compile_env,
                check=False,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired as exc:
            raise PaperArtifactError(
                f"COMPILE_FAILED: {' '.join(command)} timed out within the "
                "110-second compile budget"
            ) from exc
        command_log = (
            f"--- {' '.join(command)} (rc={result.returncode}) ---\n"
            f"{result.stdout}\n{result.stderr}"
        )
        logs.append(command_log)
        if command[0] == "xelatex":
            final_xelatex_output = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0:
            raise PaperArtifactError(
                f"COMPILE_FAILED: {' '.join(command)} exited with "
                f"status {result.returncode}\n{'\n'.join(logs[-2:])}"
            )
    return logs, final_xelatex_output


def _validate_final_latex_quality(paper: Path, final_xelatex_output: str) -> None:
    paper_log = paper / "paper.log"
    if paper_log.is_symlink():
        raise PaperArtifactError("COMPILE_FAILED: paper output files must not be symlinks")
    final_quality_log = final_xelatex_output
    if paper_log.is_file():
        final_quality_log += "\n" + paper_log.read_text(
            encoding="utf-8",
            errors="replace",
        )
    missing_glyphs = sorted(
        set(re.findall(r"^Missing character:.*$", final_quality_log, re.MULTILINE))
    )
    overfull_points = [
        float(value)
        for value in re.findall(
            r"Overfull \\hbox \((\d+(?:\.\d+)?)pt too wide\)",
            final_quality_log,
        )
    ]
    severe_overfull = [value for value in overfull_points if value >= 20.0]
    unresolved = sorted(
        {
            line.strip()
            for line in final_quality_log.splitlines()
            if re.search(
                r"LaTeX Warning: (?:Citation|Reference) .+ undefined|"
                r"There were undefined references",
                line,
            )
        }
    )
    if not (missing_glyphs or severe_overfull or unresolved):
        return
    messages = ["COMPILE_FAILED: LATEX_OUTPUT_QUALITY_GATE"]
    if missing_glyphs:
        messages.extend(
            (f"LATEX_MISSING_GLYPHS: {len(missing_glyphs)}", *missing_glyphs[:20])
        )
    if severe_overfull:
        messages.append(
            "LATEX_LAYOUT_OVERFLOW: "
            f"max={max(severe_overfull):.2f}pt threshold=20.00pt"
        )
    if unresolved:
        messages.extend(
            (f"LATEX_UNRESOLVED_REFERENCES: {len(unresolved)}", *unresolved[:20])
        )
    raise PaperArtifactError("\n".join(messages))


def _pdf_page_content_report(reader: Any, target_pages: int) -> dict[str, Any]:
    """Measure extractable, visible content without counting cover/reference padding."""

    page_texts: list[str] = []
    page_units: list[int] = []
    reference_heading_pages: list[bool] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise PaperArtifactError(
                "COMPILE_FAILED: could not extract generated PDF page "
                f"{page_number} text: {exc}"
            ) from exc
        reference_heading_pages.append(
            any(
                _REFERENCE_HEADING_RE.fullmatch(re.sub(r"\s+", " ", line).strip())
                for line in text.splitlines()
                if line.strip()
            )
        )
        normalized = re.sub(r"\s+", " ", text).strip()
        page_texts.append(normalized)
        english_tokens = len(re.findall(r"\b[A-Za-z0-9][A-Za-z0-9'’-]*\b", normalized))
        cjk_characters = len(_CJK_RE.findall(normalized))
        page_units.append(english_tokens + cjk_characters)

    reference_start: int | None = None
    earliest_reference_page = max(1, len(page_texts) // 2)
    for index, has_reference_heading in enumerate(reference_heading_pages):
        if index < earliest_reference_page:
            continue
        if has_reference_heading:
            reference_start = index
            break

    exempt_near_empty = {0} if page_texts else set()
    reference_pages: set[int] = set()
    if reference_start is not None:
        reference_pages.update(range(reference_start, len(page_texts)))
        exempt_near_empty.update(reference_pages)

    near_empty_pages = [
        index + 1
        for index, units in enumerate(page_units)
        if units < _MIN_VISIBLE_PAGE_UNITS and index not in exempt_near_empty
    ]
    substantive_pages = [
        index + 1
        for index, units in enumerate(page_units)
        if units >= _MIN_SUBSTANTIVE_PAGE_UNITS and index not in reference_pages
    ]
    return {
        "page_units": page_units,
        "near_empty_pages": near_empty_pages,
        "substantive_pages": substantive_pages,
        "reference_pages": sorted(index + 1 for index in reference_pages),
        "target_met": (
            len(substantive_pages) >= target_pages and not near_empty_pages
        ),
    }


def compile_pdf(payload: Mapping[str, Any]) -> str:
    """Compile and validate a real PDF using the managed TeX toolchain."""

    from pypdf import PdfReader

    paper = _paper_run_dir(payload, prefix="COMPILE_FAILED", create=True)
    expected_tex = paper / "paper.tex"
    expected_bib = paper / "references.bib"
    if expected_tex.is_symlink() or expected_bib.is_symlink():
        raise PaperArtifactError("COMPILE_FAILED: paper input/output files must not be symlinks")
    tex_body, bibliography, target_pages = _extract_compile_inputs(payload, paper)
    _reject_invisible_text_controls(tex_body)
    tex_body = _prepare_tex(tex_body)
    enforce_page_target = _boolean(payload, "enforce_page_target", True)
    reuse_existing = _boolean(payload, "reuse_existing", False)
    stale_paths = tuple(
        paper / f"paper{suffix}"
        for suffix in (".pdf", ".aux", ".bbl", ".blg", ".log", ".out", ".toc")
    )
    if any(path.is_symlink() for path in stale_paths):
        raise PaperArtifactError("COMPILE_FAILED: paper output files must not be symlinks")
    pdf_path = paper / "paper.pdf"
    fingerprint_path = paper / "paper.compile-input.sha256"
    if fingerprint_path.is_symlink():
        raise PaperArtifactError("COMPILE_FAILED: compile fingerprint must not be a symlink")
    input_fingerprint = _compile_input_fingerprint(tex_body, bibliography)
    inputs_unchanged = bool(
        reuse_existing
        and expected_tex.is_file()
        and expected_bib.is_file()
        and pdf_path.is_file()
        and fingerprint_path.is_file()
        and expected_tex.read_text(encoding="utf-8") == tex_body
        and expected_bib.read_text(encoding="utf-8") == bibliography
        and fingerprint_path.read_text(encoding="ascii").strip() == input_fingerprint
    )
    logs: list[str] = []
    compile_action = "reused" if inputs_unchanged else "compiled"
    if inputs_unchanged:
        _validate_final_latex_quality(paper, "")
    else:
        expected_tex.write_text(tex_body, encoding="utf-8")
        expected_bib.write_text(bibliography, encoding="utf-8")
        fingerprint_path.unlink(missing_ok=True)
        for path in stale_paths:
            path.unlink(missing_ok=True)
        logs, final_xelatex_output = _compile_commands(paper)
        _validate_final_latex_quality(paper, final_xelatex_output)
        fingerprint_path.write_text(input_fingerprint + "\n", encoding="ascii")

    if pdf_path.is_symlink():
        raise PaperArtifactError("COMPILE_FAILED: paper output files must not be symlinks")
    pdf = pdf_path.resolve()
    if not pdf.is_file():
        log_text = (
            (paper / "paper.log").read_text(encoding="utf-8", errors="ignore")
            if (paper / "paper.log").is_file()
            else ""
        )
        log_tail = "\n".join(log_text.splitlines()[-80:])
        raise PaperArtifactError(
            f"COMPILE_FAILED:\n{'\n'.join(logs[-3:])}\n\n"
            f"=== paper.log tail ===\n{log_tail}"
        )
    try:
        reader = PdfReader(str(pdf))
        pages = len(reader.pages)
    except Exception as exc:
        raise PaperArtifactError(
            f"COMPILE_FAILED: could not read generated PDF page count: {exc}"
        ) from exc
    content_report = _pdf_page_content_report(reader, target_pages)
    total_page_target_met = pages >= target_pages
    page_target_met = total_page_target_met and bool(content_report["target_met"])
    if not page_target_met and enforce_page_target:
        near_empty = ",".join(str(item) for item in content_report["near_empty_pages"])
        raise PaperArtifactError(
            "COMPILE_FAILED: PDF_PAGE_TARGET_NOT_MET: "
            f"requested at least {target_pages} substantive pages; compiled PDF has "
            f"{pages} total and {len(content_report['substantive_pages'])} substantive; "
            f"near-empty non-front/reference pages={near_empty or 'none'}"
        )
    lines = [
        f"PDF_PATH: {pdf}",
        f"PDF_PAGES: {pages}",
        f"PDF_SUBSTANTIVE_PAGES: {len(content_report['substantive_pages'])}",
        "PDF_PAGE_CONTENT_UNITS: "
        + ",".join(str(item) for item in content_report["page_units"]),
        "PDF_NEAR_EMPTY_PAGES: "
        + (
            ",".join(str(item) for item in content_report["near_empty_pages"])
            or "none"
        ),
        "PDF_REFERENCE_PAGES: "
        + (
            ",".join(str(item) for item in content_report["reference_pages"])
            or "none"
        ),
        f"PDF_TARGET_PAGES: {target_pages}",
        f"PDF_PAGE_STATUS: {'met' if page_target_met else 'shortfall'}",
        f"PDF_COMPILE_ACTION: {compile_action}",
        f"PDF_BYTES: {pdf.stat().st_size}",
        f"TEX_BYTES: {expected_tex.stat().st_size}",
        f"BIB_BYTES: {expected_bib.stat().st_size}",
    ]
    if not page_target_met:
        near_empty = ",".join(str(item) for item in content_report["near_empty_pages"])
        lines.append(
            "PDF_PAGE_TARGET_NOT_MET: "
            f"requested at least {target_pages} substantive pages; compiled PDF has "
            f"{pages} total and {len(content_report['substantive_pages'])} substantive; "
            f"near-empty non-front/reference pages={near_empty or 'none'}"
        )
    return "\n".join(lines)


_OPERATIONS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "persist_sections": persist_sections,
    "assemble_manuscript_tex": assemble_manuscript_tex,
    "materialize_manuscript": materialize_manuscript,
    "apply_length_expansion": apply_length_expansion,
    "citation_map": citation_map,
    "compile_pdf": compile_pdf,
}


def execute(payload: Mapping[str, Any]) -> str:
    """Execute the selected operation and return its text output contract."""

    operation = _text(payload, "operation").strip()
    handler = _OPERATIONS.get(operation)
    if handler is None:
        raise PaperArtifactError(
            "PAPER_ARTIFACT_RUNTIME_FAILED: operation must be one of "
            + ", ".join(sorted(_OPERATIONS))
        )
    return handler(payload)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, Mapping):
            raise PaperArtifactError("PAPER_ARTIFACT_RUNTIME_FAILED: payload must be an object")
        output = execute(payload)
    except (json.JSONDecodeError, PaperArtifactError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
