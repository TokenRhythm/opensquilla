"""Compile a LaTeX paper with xelatex + bibtex; print the log tail."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_TITLE_RE = re.compile(r"\\title\{(?P<title>.*?)\}", re.DOTALL)
_BIB_KEY_RE = re.compile(r"@\w+\s*\{\s*([^,\s]+)")
_CITE_RE = re.compile(
    r"\\cite[a-zA-Z*]*\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}",
)
_PAGE_COUNT_RE = re.compile(r"Output written on .+?\((\d+) pages?(?:,|\))")


def _read_first_existing(paths: list[Path]) -> str | None:
    for path in paths:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return _clean_latex_fragment(text)
    return None


def _clean_latex_fragment(text: str) -> str:
    """Extract the usable LaTeX fragment from common agent wrappers."""
    fenced = re.search(r"```(?:latex|tex)?\s*(.*?)```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)

    markers = [
        r"\begin{abstract}",
        r"\section{",
        r"\subsection{",
    ]
    starts = [text.find(marker) for marker in markers if text.find(marker) >= 0]
    if starts:
        text = text[min(starts):]

    lines = []
    for line in text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith(("**File:", "File written to:", "文件路径：")):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _extract_title(original: str) -> str:
    match = _TITLE_RE.search(original)
    if match:
        title = " ".join(match.group("title").split())
        if title:
            return title
    return "OpenClaw Self-Evolving Optimization"


def _contains_process_wrapper(text: str) -> bool:
    markers = (
        "```",
        "Let me ",
        "Now let me ",
        "File written to:",
        "**File:",
        "文件路径：",
        "has been written",
    )
    return any(marker in text for marker in markers)


def _looks_like_complete_clean_paper(original: str) -> bool:
    required = (
        r"\begin{abstract}",
        r"\section{Introduction}",
        r"\section{Method}",
        r"\section{Results}",
        r"\section{Discussion}",
    )
    return all(marker in original for marker in required) and not _contains_process_wrapper(
        original,
    )


def _cjk_preamble(title: str, body: str) -> str:
    needs_cjk = bool(_CJK_RE.search(title) or _CJK_RE.search(body))
    cjk_lines = ""
    if needs_cjk:
        font_dirs = [
            Path(value)
            for value in os.environ.get("OSFONTDIR", "").split(os.pathsep)
            if value
        ]
        managed_noto = any(
            (directory / "NotoSansCJK-Regular.ttc").is_file() for directory in font_dirs
        )
        if managed_noto:
            cjk_lines = r"""
\usepackage{fontspec}
\setmainfont[FontIndex=2]{NotoSansCJK-Regular.ttc}
"""
        else:
            cjk_lines = r"""
\usepackage{fontspec}
\usepackage{xeCJK}
\IfFontExistsTF{WenQuanYi Zen Hei}{%
  \setCJKmainfont{WenQuanYi Zen Hei}%
}{%
  \IfFontExistsTF{Droid Sans Fallback}{%
    \setCJKmainfont{Droid Sans Fallback}%
  }{}%
}
"""
    return (
        "\\documentclass[11pt]{article}\n"
        "% xelatex is Unicode-native; configure a CJK-capable font when needed.\n"
        f"{cjk_lines}"
        "\\usepackage{amsmath}\n"
        "\\usepackage{amssymb}\n"
        "\\usepackage{amsfonts}\n"
        "\\usepackage{graphicx}\n"
        "\\usepackage{hyperref}\n"
    )


def _section_fragments(cwd: Path) -> dict[str, str]:
    workspace = cwd.parent
    candidates = {
        "abstract": [
            workspace / "abstract.tex",
            cwd / "section-abstract.tex",
            cwd / "abstract.tex",
        ],
        "introduction": [
            workspace / "introduction.tex",
            cwd / "introduction.tex",
        ],
        "method": [
            cwd / "method.tex",
            workspace / "method.tex",
        ],
        "results": [
            workspace / "results.tex",
            cwd / "results_section.tex",
            cwd / "results.tex",
        ],
        "discussion": [
            workspace / "discussion.tex",
            cwd / "discussion.tex",
        ],
    }
    fragments: dict[str, str] = {}
    for name, paths in candidates.items():
        fragment = _read_first_existing(paths)
        if fragment:
            fragments[name] = fragment
    return fragments


def _prepare_tex_for_compile(tex_path: Path) -> bool:
    """Rewrite paper.tex from clean section files when agent output was noisy."""
    original = tex_path.read_text(encoding="utf-8")
    if _looks_like_complete_clean_paper(original):
        if _CJK_RE.search(original) and "\\usepackage{xeCJK}" not in original:
            insertion = _cjk_preamble(_extract_title(original), original)
            begin = original.find("\\begin{document}")
            if begin >= 0:
                tex_path.write_text(insertion + original[begin:], encoding="utf-8")
                return True
        return False

    cwd = tex_path.parent
    fragments = _section_fragments(cwd)
    ordered_names = ["abstract", "introduction", "method", "results", "discussion"]
    if all(name in fragments for name in ordered_names):
        body = "\n\n".join(fragments[name] for name in ordered_names)
        title = _extract_title(original)
        tex_path.write_text(
            _cjk_preamble(title, body)
            + f"\\title{{ {title} }}\n"
            + "\\author{OpenSquilla meta-paper-write}\n"
            + "\\date{\\today}\n"
            + "\\begin{document}\n"
            + "\\maketitle\n"
            + body
            + "\n\\bibliographystyle{plain}\n"
            + "\\bibliography{references}\n"
            + "\\end{document}\n",
            encoding="utf-8",
        )
        return True

    if _CJK_RE.search(original) and "\\usepackage{xeCJK}" not in original:
        insertion = _cjk_preamble(_extract_title(original), original)
        begin = original.find("\\begin{document}")
        if begin >= 0:
            tex_path.write_text(insertion + original[begin:], encoding="utf-8")
            return True
    return False


def _paper_contract_enabled(tex_path: Path) -> bool:
    tex = tex_path.read_text(encoding="utf-8")
    return "\\bibliography{" in tex and (tex_path.parent / "references.bib").is_file()


def _bib_keys(bib_path: Path) -> set[str]:
    if not bib_path.is_file():
        return set()
    return {
        match.group(1).strip()
        for match in _BIB_KEY_RE.finditer(bib_path.read_text(encoding="utf-8"))
    }


def _cited_keys(tex: str) -> set[str]:
    keys: set[str] = set()
    for match in _CITE_RE.finditer(tex):
        for raw_key in match.group(1).split(","):
            key = raw_key.strip()
            if key:
                keys.add(key)
    return keys


def _validate_citation_contract(
    tex_path: Path,
    *,
    min_cited_refs: int = 20,
) -> list[str]:
    if not _paper_contract_enabled(tex_path):
        return []
    tex = tex_path.read_text(encoding="utf-8")
    cited = _cited_keys(tex)
    defined = _bib_keys(tex_path.parent / "references.bib")
    errors: list[str] = []
    missing = sorted(cited - defined)
    if missing:
        errors.append(f"undefined citation keys: {', '.join(missing)}")
    valid_cited = cited & defined
    if len(valid_cited) < min_cited_refs:
        errors.append(
            f"paper must include at least {min_cited_refs} cited references; "
            f"found {len(valid_cited)}",
        )
    return errors


def _validate_page_contract(log_text: str, *, min_pages: int = 10) -> list[str]:
    matches = _PAGE_COUNT_RE.findall(log_text)
    if not matches:
        return ["could not determine compiled PDF page count from xelatex log"]
    pages = int(matches[-1])
    if pages < min_pages:
        return [f"paper must be at least {min_pages} pages; compiled PDF has {pages} pages"]
    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tex_path")
    args = parser.parse_args()

    tex_path = Path(args.tex_path)
    if not tex_path.is_file():
        print(f"error: {tex_path} does not exist", file=sys.stderr)
        sys.exit(2)
    if shutil.which("xelatex") is None:
        print("error: xelatex not in PATH", file=sys.stderr)
        sys.exit(3)

    _prepare_tex_for_compile(tex_path)

    citation_errors = _validate_citation_contract(tex_path, min_cited_refs=20)
    if citation_errors:
        for error in citation_errors:
            print(f"error: {error}", file=sys.stderr)
        sys.exit(5)

    cwd = tex_path.parent
    stem = tex_path.stem
    passes = [
        ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
        ["bibtex", stem],
        ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
        ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
    ]
    full_log: list[str] = []
    for cmd in passes:
        rc, log = _run(cmd, cwd)
        full_log.append(f"--- {' '.join(cmd)} (rc={rc}) ---\n{log}")
        # xelatex returns 0 even on minor issues; only fail-hard on the
        # final pass.
        if rc != 0 and cmd[0] == "xelatex" and cmd is passes[-1]:
            print("\n".join(full_log[-3:]), file=sys.stderr)
            sys.exit(rc)

    pdf = cwd / f"{stem}.pdf"
    if not pdf.is_file():
        print("error: compile produced no PDF", file=sys.stderr)
        print("\n".join(full_log[-3:]), file=sys.stderr)
        sys.exit(4)

    if _paper_contract_enabled(tex_path):
        page_errors = _validate_page_contract("\n".join(full_log), min_pages=10)
        if page_errors:
            for error in page_errors:
                print(f"error: {error}", file=sys.stderr)
            print("\n".join(full_log[-3:]), file=sys.stderr)
            sys.exit(6)

    # On success: emit a clean user-facing deliverable as stdout. The
    # full xelatex log tail is verbose and confusing in the chat surface
    # (overfull-hbox warnings, font info, page break trackers) — route it
    # to stderr where it survives in the gateway log for debugging
    # without becoming the meta-skill's final_text payload.
    log_tail = "\n".join("\n".join(full_log).splitlines()[-40:])
    print(log_tail, file=sys.stderr)
    try:
        size_kb = pdf.stat().st_size / 1024
        print(f"Paper compiled successfully: {pdf} ({size_kb:.1f} KB)")
    except OSError:
        print(f"Paper compiled successfully: {pdf}")


if __name__ == "__main__":
    main()
