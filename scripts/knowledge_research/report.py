"""Deterministic HTML rendering for verified Knowledge research state."""

from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

if __package__:
    from .references import build_bibliography, source_format
    from .table_views import (
        MAX_PARSE_CHARS,
        MAX_SPAN,
        markdown_table_html,
        summarize_table,
        table_quality_view,
    )
else:  # pragma: no cover
    from references import (  # type: ignore[import-not-found,no-redef]
        build_bibliography,
        source_format,
    )
    from table_views import (  # type: ignore[import-not-found,no-redef]
        MAX_PARSE_CHARS,
        MAX_SPAN,
        markdown_table_html,
        summarize_table,
        table_quality_view,
    )


_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U000323af]")
_LABELS = {
    "en": {
        "references": "References",
        "reading_summary": "Reading coverage overview",
        "reading_summary_unavailable": "Reading coverage could not be calculated for this report.",
        "reading_summary_meaning": (
            "This measures source text projected through Knowledge tools; it is not a "
            "measure of model comprehension."
        ),
        "coverage": "Coverage: ",
        "coverage_unavailable": "unavailable",
        "notes": "Source Notes",
        "parsed": "Parsed table",
        "original": "Original PDF crop",
        "image_alt": "Original PDF table crop",
        "text_version": ", text version",
        "unsafe_html": ("Parsed HTML cannot be safely displayed; consult the original PDF crop."),
        "oversize_text": ("Parsed text exceeds the display limit; consult the original PDF crop."),
        "quality_note": (
            "Some tables have not been fully checked against the original PDF images. "
            "Text recognition or extraction may omit content; the included tables "
            "may not cover every table in the source."
        ),
    },
    "zh-CN": {
        "references": "\u53c2\u8003\u6587\u732e",
        "reading_summary": "\u8d44\u6599\u9605\u8bfb\u8986\u76d6\u6982\u89c8",
        "reading_summary_unavailable": (
            "\u672c\u62a5\u544a\u6682\u65e0\u53ef\u7528\u7684\u9605\u8bfb\u8986\u76d6\u5ea6\u7edf\u8ba1\u3002"
        ),
        "reading_summary_meaning": (
            "\u8be5\u6307\u6807\u53ea\u8868\u793a Knowledge \u5de5\u5177"
            "\u5b9e\u9645\u6295\u5f71\u7684"
            "\u6e90\u6587\u672c\u8303\u56f4\uff0c"
            "\u4e0d\u7b49\u4e8e\u6a21\u578b\u5df2\u7406\u89e3\u7684\u7a0b\u5ea6\u3002"
        ),
        "coverage": "\u8986\u76d6\u7387\uff1a",
        "coverage_unavailable": "\u672a\u7edf\u8ba1",
        "notes": "\u8d44\u6599\u8bf4\u660e",
        "parsed": "\u8868\u683c\u6587\u5b57",
        "original": "\u539f\u59cb PDF \u622a\u56fe",
        "image_alt": "\u539f\u59cb PDF \u8868\u683c\u622a\u56fe",
        "text_version": "\uff0c\u6587\u5b57\u7248",
        "unsafe_html": (
            "\u8868\u683c\u6587\u5b57\u65e0\u6cd5\u5b89\u5168\u663e\u793a\uff1b"
            "\u8bf7\u67e5\u770b\u539f\u59cb PDF \u622a\u56fe\u3002"
        ),
        "oversize_text": (
            "\u8868\u683c\u6587\u5b57\u8d85\u51fa\u663e\u793a\u8303\u56f4\uff1b"
            "\u8bf7\u67e5\u770b\u539f\u59cb PDF \u622a\u56fe\u3002"
        ),
        "quality_note": (
            "\u90e8\u5206\u8868\u683c\u5c1a\u672a\u5b8c\u6210"
            "\u5b8c\u6574\u539f\u56fe\u6838\u5bf9\uff1b"
            "\u6587\u5b57\u8bc6\u522b\u6216\u63d0\u53d6\u53ef\u80fd\u6709\u9057\u6f0f\uff0c"
            "\u6536\u5f55\u8868\u683c\u4e0d\u4ee3\u8868\u539f\u6587\u5168\u90e8\u8868\u683c\u3002"
        ),
    },
}
_WARNINGS_ZH = {
    "The extracted table has a known content omission.": (
        "\u5df2\u77e5\u8868\u683c\u63d0\u53d6\u6709\u5185\u5bb9\u9057\u6f0f\u3002"
    ),
    "The extracted table does not match the original PDF crop.": (
        "\u63d0\u53d6\u7684\u8868\u683c\u4e0e\u539f\u59cb PDF \u622a\u56fe\u4e0d\u4e00\u81f4\u3002"
    ),
    "The table visual check failed; source completeness remains unknown.": (
        "\u8868\u683c\u539f\u56fe\u6838\u5bf9\u672a\u6210\u529f\uff0c"
        "\u5c1a\u4e0d\u80fd\u786e\u8ba4\u5185\u5bb9\u5b8c\u6574\u3002"
    ),
    "The year column is missing.": "\u8868\u683c\u7f3a\u5c11\u5e74\u4efd\u5217\u3002",
    "Annual totals mismatch the crop.": (
        "\u5e74\u5ea6\u5408\u8ba1\u4e0e\u539f\u59cb\u622a\u56fe\u4e0d\u4e00\u81f4\u3002"
    ),
}


def _report_language(state: Mapping[str, Any]) -> str:
    language = state.get("language")
    if language is not None:
        if not isinstance(language, str) or language not in _LABELS:
            raise ValueError("unsupported_report_language")
        return language
    texts = [str(state.get(key) or "") for key in ("title", "subtitle")]
    texts.extend(
        str(item.get(key) or "")
        for item in state["report"]["items"]
        for key in ("text", "caption", "section")
    )
    return "zh-CN" if any(_CJK.search(text) for text in texts) else "en"


def _warning_text(warning: str, language: str) -> str:
    if language == "en":
        return warning
    if warning in _WARNINGS_ZH:
        return _WARNINGS_ZH[warning]
    missing_year = re.fullmatch(r"Missing year column (.+)\.", warning)
    if missing_year:
        return "\u7f3a\u5c11\u5e74\u4efd\u5217\uff1a" + missing_year[1] + "\u3002"
    if _CJK.search(warning):
        return warning
    # Preserve unrecognized findings verbatim; do not invent a translation.
    return "\u6838\u9a8c\u63d0\u793a\uff08\u539f\u6587\uff09\uff1a" + warning


class _TableSanitizer(HTMLParser):
    _allowed = frozenset(
        {
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "th",
            "td",
            "caption",
            "colgroup",
            "col",
            "strong",
            "em",
            "code",
            "span",
            "b",
            "i",
            "sub",
            "sup",
            "p",
            "div",
        }
    )
    _void = frozenset({"col"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0
        self.suppressed: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template"}:
            self.suppressed.append(tag)
        if self.suppressed:
            return
        if tag == "br" and self.depth:
            self.parts.append("<br>")
            return
        if tag not in self._allowed:
            return
        for span_name in ("rowspan", "colspan"):
            if sum(name == span_name for name, _ in attrs) > 1:
                raise ValueError("duplicate_span_attribute")
        kept: list[str] = []
        for name, value in attrs:
            if (
                name == "style"
                and isinstance(value, str)
                and value in {"text-align:left", "text-align:center", "text-align:right"}
            ):
                kept.append(f' class="align-{value.removeprefix("text-align:")}"')
                continue
            if name not in {"colspan", "rowspan", "scope"} or value is None:
                continue
            if name in {"colspan", "rowspan"}:
                if not value.isascii() or not value.isdigit() or len(value) > 3:
                    continue
                if not 1 <= int(value) <= MAX_SPAN:
                    continue
            if name == "scope" and value not in {"row", "col", "rowgroup", "colgroup"}:
                continue
            kept.append(f' {name}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(kept)}>")
        if tag not in self._void:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.suppressed:
            if tag == self.suppressed[-1]:
                self.suppressed.pop()
            return
        if tag in self._allowed and tag not in self._void:
            self.parts.append(f"</{tag}>")
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(html.escape(data))


def _render_table_text(text_payload: Mapping[str, Any], *, language: str = "en") -> str:
    content = str(text_payload.get("content") or "")
    fmt = str(text_payload.get("format") or "").lower()
    is_markdown = fmt in {"md", "markdown", "text/markdown", "text/x-markdown"}
    if fmt in {"html", "text/html"} or (not is_markdown and "<table" in content.lower()):
        inspection = summarize_table({"text": text_payload})
        if any(issue != "outside_table_text" for issue in inspection["issues"]):
            return (
                '<p class="table-warning">' + html.escape(_LABELS[language]["unsafe_html"]) + "</p>"
            )
        parser = _TableSanitizer()
        parser.feed(content)
        parser.close()
        rendered = "".join(parser.parts)
        if "<table" in rendered:
            return rendered
    if len(content) > MAX_PARSE_CHARS:
        return (
            '<p class="table-warning">' + html.escape(_LABELS[language]["oversize_text"]) + "</p>"
        )
    markdown = _markdown_table(content)
    if markdown is not None:
        return markdown
    return f"<pre>{html.escape(content)}</pre>"


def _markdown_table(content: str) -> str | None:
    rendered, issues = markdown_table_html(content)
    if rendered is None or issues:
        return None
    parser = _TableSanitizer()
    parser.feed(rendered)
    parser.close()
    return "".join(parser.parts)


def _page(record: Mapping[str, Any]) -> tuple[int | None, int | None]:
    locator = record.get("locator")
    locator = locator if isinstance(locator, Mapping) else {}
    start = locator.get("pageStart")
    end = locator.get("pageEnd")
    page_range = locator.get("page")
    if isinstance(page_range, Mapping):
        start = start or page_range.get("start")
        end = end or page_range.get("end")
    if not isinstance(start, int):
        start = record.get("page") if isinstance(record.get("page"), int) else None
    if not isinstance(end, int):
        end = start
    return start, end


def _page_label(start: int | None, end: int | None, *, language: str = "en") -> str:
    if start is None:
        return ""
    if language == "zh-CN":
        pages = str(start) if end is None or end == start else f"{start}-{end}"
        return f"\uff0c\u7b2c {pages} \u9875"
    if end is None or end == start:
        return f", p. {start}"
    return f", pp. {start}-{end}"


def render_html_report(state: Mapping[str, Any]) -> str:
    language = _report_language(state)
    labels = _LABELS[language]
    ledger = state["ledger"]
    evidence = ledger["evidence"]
    files = ledger["files"]
    tables = ledger["tables"]
    bibliography = build_bibliography(state)
    reference_numbers = bibliography["fileReferenceNumbers"]
    references = bibliography["references"]

    def reference_coverage(number: int) -> str:
        reading = state.get("readingCoverage")
        if not isinstance(reading, Mapping):
            return ""
        row: Mapping[str, Any] = next(
            (item for item in reading.get("references", []) if item.get("number") == number),
            {},
        )
        value = row.get("percentage")
        label = labels["coverage_unavailable"]
        if (
            row.get("status") == "available"
            and isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0 <= value <= 100
        ):
            label = f"{value:.2f}%"
        return '<span class="reading-coverage">' + labels["coverage"] + label + "</span>"

    def reading_summary() -> str:
        reading = state.get("readingCoverage")
        if not isinstance(reading, Mapping):
            return ""
        summary = reading.get("summary")
        if not isinstance(summary, Mapping):
            return ""
        total = summary.get("bibliographyEntries")
        available = summary.get("availableReferences")
        overall = summary.get("overallPercentage")
        median_value = summary.get("medianPercentage")
        low10 = summary.get("below10pctReferences")
        if not isinstance(total, int) or not isinstance(available, int):
            return (
                '<p class="reading-summary-unavailable">'
                f'{labels["reading_summary_unavailable"]}</p>'
            )
        overall_label = (
            f"{float(overall):.2f}%"
            if isinstance(overall, int | float) and not isinstance(overall, bool)
            else labels["coverage_unavailable"]
        )
        median_label = (
            f"{float(median_value):.2f}%"
            if isinstance(median_value, int | float) and not isinstance(median_value, bool)
            else labels["coverage_unavailable"]
        )
        if language == "zh-CN":
            body = (
                f"已统计 {available}/{total} 条参考文献；总体覆盖率 {overall_label}，"
                f"中位数 {median_label}；低于 10% 的来源 "
                f"{low10 if isinstance(low10, int) else '未统计'} 条。"
            )
        else:
            body = (
                f"{available}/{total} references measured; overall {overall_label}, "
                f"median {median_label}; "
                f"{low10 if isinstance(low10, int) else 'unknown'} sources below 10%."
            )
        return (
            '<div class="reading-summary"><strong>'
            + labels["reading_summary"]
            + "</strong><p>"
            + body
            + "</p><p class=\"reading-summary-note\">"
            + labels["reading_summary_meaning"]
            + "</p></div>"
        )

    def evidence_citations(evidence_ids: list[str]) -> str:
        citation_labels: list[str] = []
        seen: set[str] = set()
        for evidence_id in evidence_ids:
            record = evidence[evidence_id]
            file_id = str(record["fileId"])
            number = reference_numbers[file_id]
            start, end = _page(record)
            suffix = (
                _page_label(start, end, language=language)
                if source_format(files[file_id]) == "PDF"
                else labels["text_version"]
            )
            label = f"[{number}{suffix}]"
            if label in seen:
                continue
            seen.add(label)
            citation_labels.append(
                f'<span class="citation"><a href="#ref-{number}">{label}</a></span>'
            )
        return " ".join(citation_labels)

    sections: dict[str, list[str]] = {}
    section_order: list[str] = []
    unknown_table_quality = False
    assessments = ledger.get("tableAssessments", {})
    assessments = assessments if isinstance(assessments, Mapping) else {}
    for item in state["report"]["items"]:
        section = str(item["section"])
        if section not in sections:
            sections[section] = []
            section_order.append(section)
        if item["kind"] == "claim":
            text = html.escape(str(item["text"])).replace("\n", "<br>")
            citations = evidence_citations(list(item["evidenceIds"]))
            sections[section].append(f"<p>{text} {citations}</p>")
            continue
        table = tables[item["tableId"]]
        file_id = str(table["fileId"])
        number = reference_numbers[file_id]
        start, end = _page(table)
        page_label = _page_label(start, end, language=language)
        citation = (
            f'<span class="citation"><a href="#ref-{number}">[{number}{page_label}]</a></span>'
        )
        parsed = _render_table_text(table["text"], language=language)
        screenshot = table["screenshot"]
        mime = html.escape(str(screenshot["mediaType"]), quote=True)
        data = html.escape(str(table["screenshotDataBase64"]), quote=True)
        caption = html.escape(str(item["caption"]))
        quality = table_quality_view(table, assessment=assessments.get(item["tableId"]))
        unknown_table_quality |= (
            quality["sourceCompleteness"] == "unknown" or quality["visualCheck"] == "not_performed"
        )
        warning_html = "".join(
            f'<p class="table-warning">{html.escape(_warning_text(warning, language))}</p>'
            for warning in quality["warnings"]
        )
        sections[section].append(
            '<figure class="table-evidence">'
            f"<figcaption>{caption} {citation}</figcaption>"
            f'<div class="parsed-table"><h3>{labels["parsed"]}</h3>'
            f"{parsed}</div>"
            f'<div class="original-table"><h3>{labels["original"]}</h3>'
            f'<img src="data:{mime};base64,{data}" alt="{labels["image_alt"]}"></div>'
            f"{warning_html}"
            "</figure>"
        )

    section_html = "".join(
        f"<section><h2>{html.escape(section)}</h2>{''.join(sections[section])}</section>"
        for section in section_order
    )
    reference_html = "".join(
        f'<li id="ref-{reference["number"]}">'
        + html.escape(str(reference["title"]))
        + ' <span class="filename">['
        + " + ".join(dict.fromkeys(member["format"] for member in reference["members"]))
        + "]</span>"
        + reference_coverage(reference["number"])
        + "</li>"
        for reference in references
    )
    subtitle = state.get("subtitle")
    subtitle_html = f'<div class="subtitle">{html.escape(str(subtitle))}</div>' if subtitle else ""
    title = html.escape(str(state["title"]))
    quality_note = (
        f'<section class="source-notes"><h2>{labels["notes"]}</h2>'
        f'<p class="table-quality-note">{labels["quality_note"]}</p></section>'
        if unknown_table_quality
        else ""
    )
    return f"""<!doctype html>
<html lang="{language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
@page {{
  size: A4; margin: 18mm 16mm 20mm;
  @bottom-right {{ content: counter(page) " / " counter(pages);
    font-family: Arial, "Noto Sans CJK SC", sans-serif; font-size: 9pt; color: #66717c; }}
}}
:root {{ color-scheme: light; font-family: Arial, "Noto Sans CJK SC", sans-serif; color: #202a35; }}
body {{ max-width: 860px; margin: 0 auto; padding: 40px 28px 64px;
  font-size: 16px; line-height: 1.8; background: #fff; }}
header {{ border-bottom: 2px solid #183153; padding-bottom: 20px; margin-bottom: 28px; }}
h1 {{ margin: 0; font-size: 32px; line-height: 1.4; letter-spacing: 0; }}
.subtitle {{ margin-top: 12px; color: #59636e; font-size: 14px; line-height: 1.65; }}
h2 {{ margin: 34px 0 14px; padding-bottom: 7px; border-bottom: 1px solid #d9e0e6;
  font-size: 22px; line-height: 1.45; letter-spacing: 0; color: #183153; }}
h3 {{ margin: 0 0 10px; font-size: 13px; letter-spacing: 0; color: #59636e; }}
p {{ margin: 0 0 16px; orphans: 3; widows: 3; }}
main > section:first-child > h2 {{ margin-top: 0; }}
h1, h2, h3, figcaption {{ break-after: avoid; }}
.citation {{ white-space: nowrap; color: #315779; font-size: .85em; font-weight: 400; }}
.citation a {{ color: inherit; text-decoration: none; }}
.citation a:hover, .citation a:focus-visible {{ text-decoration: underline; }}
.table-evidence {{ margin: 24px 0 30px; break-inside: avoid; }}
figcaption {{ font-weight: 700; font-size: .95em; line-height: 1.65; margin-bottom: 12px; }}
.parsed-table, .original-table {{ margin-top: 14px; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px;
  line-height: 1.55; font-variant-numeric: tabular-nums; }}
th, td {{ border: 1px solid #c9d1d9; padding: 7px 9px; text-align: left; vertical-align: top; }}
th {{ background: #eef2f5; color: #183153; }}
.align-left {{ text-align: left; }}
.align-center {{ text-align: center; }}
.align-right {{ text-align: right; }}
img {{ display: block; max-width: 100%; width: auto; height: auto;
  box-sizing: border-box; border: 1px solid #c9d1d9; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f5f7f9; padding: 12px; }}
.references {{ margin-top: 40px; border-top: 2px solid #183153; padding-top: 12px; }}
.references h2 {{ margin-top: 0; border-bottom: 0; }}
.references ol {{ padding-left: 24px; }}
.references li {{ margin: 0 0 10px; padding-left: 3px; font-size: 14px; line-height: 1.65;
  overflow-wrap: anywhere; break-inside: avoid; scroll-margin-top: 24px; }}
.references li:target {{ background: #eef2f5; }}
.filename {{ color: #66717c; }}
.reading-coverage {{ display: inline-block; margin-left: 10px;
  white-space: nowrap; color: #66717c; }}
.reading-summary {{ margin: 0 0 18px; padding: 12px 14px; border-left: 3px solid #315779;
  background: #f5f7f9; color: #344454; font-size: 14px; line-height: 1.65; }}
.reading-summary p {{ margin: 4px 0 0; }}
.reading-summary-note {{ color: #66717c; font-size: 12px; }}
.table-quality-note, .table-warning {{ font-size: 13px; color: #7a341b; }}
@media screen and (max-width: 600px) {{
  body {{ padding: 24px 18px 40px; }}
  h1 {{ font-size: 27px; }}
  h2 {{ font-size: 20px; }}
}}
@media print {{
  body {{ max-width: none; padding: 0; font-size: 11pt; line-height: 1.74; }}
  header {{ padding-bottom: 5mm; margin-bottom: 7mm; break-inside: avoid; }}
  h1 {{ font-size: 23pt; line-height: 1.4; }}
  .subtitle {{ font-size: 10pt; margin-top: 3mm; }}
  h2 {{ font-size: 15pt; margin-top: 8mm; margin-bottom: 3.5mm; padding-bottom: 2mm; }}
  p {{ margin-bottom: 3.5mm; }}
  .table-evidence {{ margin: 5mm 0 6mm; }}
  figcaption {{ font-size: 10pt; margin-bottom: 2.5mm; }}
  .parsed-table {{ display: none; }}
  .original-table {{ margin-top: 0; overflow: visible; }}
  .original-table h3 {{ display: none; }}
  .original-table img {{ max-height: 210mm; object-fit: contain; }}
  .references {{ margin-top: 9mm; padding-top: 4mm; }}
  .references li {{ font-size: 9.5pt; margin-bottom: 2.5mm; }}
  .table-quality-note, .table-warning {{ font-size: 9pt; }}
}}
</style>
</head>
<body>
<header><h1>{title}</h1>{subtitle_html}</header>
<main>{section_html}</main>
<section class="references"><h2>{labels["references"]}</h2>
{reading_summary()}<ol>{reference_html}</ol></section>
{quality_note}
</body>
</html>
"""
