"""Convert multi-search-engine JSON (on stdin) into a BibTeX file.

Each search result becomes one ``@misc{}`` entry with the URL preserved
as ``howpublished``. When the URL pattern reveals a stronger identifier
the entry gains an extra structured field so downstream gates can audit
citation provenance:

* arxiv (abs / pdf URLs)               → ``eprint = {YYMM.NNNNN}``
* DOI metadata or URLs                  → ``doi = {10.xxxx/xxxxx}``
* OpenReview / ACL Anthology / ACM DL  → kept as ``howpublished`` only
  (no canonical IDs) but tagged via ``note = {source: <domain>}``

``note`` always records the source domain so the strict citation_map +
citation_integrity_gate steps in meta-paper-write can classify each
entry as STRONG / OK / WEAK without re-parsing the URL.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TEX_SPECIAL_RE = re.compile(r"[$&%#_~^]")
_TEX_SPECIALS = {
    "$": r"\$",
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_EMBEDDED_ENTRY_RE = re.compile(
    r"@\s*(?:article|book|booklet|conference|inbook|incollection|inproceedings|"
    r"manual|mastersthesis|misc|phdthesis|proceedings|techreport|unpublished)\b",
    re.IGNORECASE,
)

# arxiv abs/pdf — both with and without version suffix
_ARXIV_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(?P<id>\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?",
    re.IGNORECASE,
)
# arxiv legacy taxonomy (cs.LG/0312001)
_ARXIV_LEGACY_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(?P<id>[a-z.\-]+/\d{7})", re.IGNORECASE,
)
_DOI_VALUE_RE = re.compile(r"(?P<doi>10\.\d{4,9}/[^\s?#\"<>]+)", re.IGNORECASE)
_YEAR_RE = re.compile(r"^[12]\d{3}$")


def _field_text(value: object, *, max_chars: int | None = None) -> str:
    """Return plain, balanced text safe inside one braced BibTeX field.

    Search metadata is untrusted prose, not LaTeX. In particular, snippets can
    contain copied ``@InProceedings{...}`` fragments and may be truncated in
    the middle of a brace group. Convert structural characters to plain text
    before length limiting, then escape only the small set of TeX specials we
    intentionally retain. Unicode is preserved.
    """

    text = _CONTROL_CHARS_RE.sub(" ", str(value or ""))
    text = text.replace("`", "'")
    text = text.replace("\\", "/")
    text = text.replace("{", "(").replace("}", ")")
    text = _EMBEDDED_ENTRY_RE.sub(lambda match: f"at {match.group(0).lstrip('@ ').lower()}", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return _TEX_SPECIAL_RE.sub(lambda match: _TEX_SPECIALS[match.group(0)], text)


def _field_url(value: object, *, max_chars: int = 4096) -> str:
    """Keep the locator intact while percent-encoding BibTeX delimiters."""

    text = str(value or "").strip()
    encoded: list[str] = []
    encoded_length = 0
    for char in text:
        code = ord(char)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            part = f"%{code:02X}"
        elif char in {"{", "}", "\\", "`"}:
            part = f"%{code:02X}"
        else:
            part = char
        if encoded_length + len(part) > max_chars:
            break
        encoded.append(part)
        encoded_length += len(part)
        if encoded_length == max_chars:
            break
    return "".join(encoded)


def _source_domain(url: str) -> str:
    """Return a normalised host string used by the citation gates."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _normalise_doi(value: object) -> str | None:
    raw = unquote(str(value or "")).strip()
    raw = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^doi:\s*", "", raw, flags=re.IGNORECASE)
    match = _DOI_VALUE_RE.search(raw)
    if match is None:
        return None
    doi = match.group("doi").rstrip(".,;{}[]")
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    doi = _CONTROL_CHARS_RE.sub("", doi)
    doi = doi.replace("\\", "").replace("{", "").replace("}", "").replace("`", "")
    return doi.lower() or None


def _detect_identifiers(url: str, explicit_doi: object = None) -> dict[str, str]:
    """Pull eprint / doi out of common paper URL shapes."""
    found: dict[str, str] = {}
    m = _ARXIV_RE.search(url)
    if m:
        found["eprint"] = m.group("id")
        found["archivePrefix"] = "arXiv"
    else:
        m = _ARXIV_LEGACY_RE.search(url)
    if m and "eprint" not in found:
        found["eprint"] = m.group("id")
        found["archivePrefix"] = "arXiv"
    doi = _normalise_doi(explicit_doi)
    if doi is None:
        doi = _normalise_doi(url)
    if doi:
        # BibTeX doesn't escape DOIs the same way — keep the raw string.
        found["doi"] = doi
    return found


def _author_name(value: object) -> tuple[str, bool]:
    if isinstance(value, dict):
        literal = str(value.get("literal") or "").strip()
        if literal:
            return literal, True
        given = str(value.get("given") or "").strip()
        family = str(value.get("family") or "").strip()
        return " ".join(part for part in (given, family) if part), False
    return str(value or "").strip(), False


def _authors(item: dict[str, object]) -> str | None:
    raw = item.get("authors", item.get("author"))
    raw_corporate = item.get("corporate_authors")
    corporate: set[str] = set()
    if isinstance(raw_corporate, list):
        corporate = {str(value).strip() for value in raw_corporate if str(value).strip()}
    if isinstance(raw, list):
        parsed = [_author_name(value) for value in raw]
    else:
        parsed = [_author_name(raw)]
    names: list[str] = []
    for name, is_literal in parsed:
        if not name:
            continue
        escaped = _field_text(name, max_chars=500)
        names.append(f"{{{{{escaped}}}}}" if is_literal or name in corporate else escaped)
    return " and ".join(names) if names else None


def _year(item: dict[str, object]) -> str | None:
    raw = item.get("year")
    if isinstance(raw, bool):
        return None
    text = str(raw or "").strip()
    return text if _YEAR_RE.fullmatch(text) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"error: stdin is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        print("error: payload.results missing or not a list", file=sys.stderr)
        sys.exit(2)

    entries: list[str] = []
    seen_dois: set[str] = set()
    for idx, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue
        title = _field_text(item.get("title") or f"Untitled {idx}", max_chars=1000)
        url = _field_url(item.get("url", ""))
        snippet = _field_text(item.get("snippet", ""), max_chars=300)
        identifiers = _detect_identifiers(url, item.get("doi", item.get("DOI")))
        doi = identifiers.get("doi")
        if doi and doi in seen_dois:
            continue
        if doi:
            seen_dois.add(doi)
            if not url:
                url = f"https://doi.org/{doi}"
        domain = _source_domain(url)
        author = _authors(item)
        year = _year(item)

        # note field carries machine-readable provenance markers so the
        # downstream citation_map / citation_integrity_gate prompts can
        # classify each entry without re-fetching the URL.
        note_bits: list[str] = []
        if domain:
            note_bits.append(f"source: {_field_text(domain, max_chars=255)}")
        if snippet:
            note_bits.append(snippet)
        note_field = "; ".join(note_bits)

        lines: list[str] = [
            f"@misc{{ref{len(entries) + 1},",
            f"  title = {{{title}}},",
        ]
        if url:
            lines.append(f"  howpublished = {{\\url{{{url}}}}},")
        if author:
            lines.append(f"  author = {{{author}}},")
        if "doi" in identifiers:
            lines.append(f"  doi = {{{identifiers['doi']}}},")
        if "eprint" in identifiers:
            lines.append(f"  eprint = {{{identifiers['eprint']}}},")
            lines.append(
                f"  archivePrefix = {{{identifiers['archivePrefix']}}},"
            )
        if note_field:
            lines.append(f"  note = {{{note_field}}},")
        if year:
            lines.append(f"  year = {{{year}}},")
        lines.append("}")
        entries.append("\n".join(lines) + "\n")

    bib_text = "\n".join(entries)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(bib_text, encoding="utf-8")
    sys.stdout.write(bib_text)


if __name__ == "__main__":
    main()
