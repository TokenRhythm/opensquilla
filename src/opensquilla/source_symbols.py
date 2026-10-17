"""Lightweight line-local symbol matching shared by source tools and parser guards."""

from __future__ import annotations

import re
from pathlib import Path

_SOURCE_C_FAMILY_EXTENSIONS = frozenset(
    {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".m", ".mm"}
)

_SOURCE_C_RETURN_TYPE = re.compile(r"[A-Za-z_][\w:<>,~*&\s]+")

_SOURCE_C_FUNCTION_TAIL = re.compile(r"\([^;{}]*\)\s*(?:const\s*)?(?:\{|$)")

_SOURCE_SYMBOL_REGEXES: tuple[tuple[frozenset[str], str, re.Pattern[str]], ...] = (
    (
        frozenset({".py", ".pyi"}),
        "class",
        re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    ),
    (
        frozenset({".py", ".pyi"}),
        "function",
        re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ),
    (
        frozenset({".js", ".jsx", ".ts", ".tsx"}),
        "class",
        re.compile(r"^\s*(?:export\s+default\s+|export\s+)?class\s+([A-Za-z_$][\w$]*)\b"),
    ),
    (
        frozenset({".js", ".jsx", ".ts", ".tsx"}),
        "function",
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\("),
    ),
    (
        frozenset({".js", ".jsx", ".ts", ".tsx"}),
        "function",
        re.compile(
            r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
            r"\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"
        ),
    ),
    (
        frozenset({".java", ".cs", ".kt", ".kts", ".scala"}),
        "class",
        re.compile(
            r"^\s*(?:public|private|protected|internal|abstract|final|sealed|open|"
            r"static|\s)*(?:class|interface|enum|record|object)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
        ),
    ),
    (
        frozenset({".java", ".cs", ".kt", ".kts", ".scala"}),
        "function",
        re.compile(
            r"^\s*(?:public|private|protected|internal|static|final|abstract|open|"
            r"override|async|suspend|\s)+[A-Za-z_<>\[\].?,\s]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
        ),
    ),
    (
        frozenset({".go"}),
        "function",
        re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ),
    (
        frozenset({".rs"}),
        "function",
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    ),
    (
        frozenset({".rs"}),
        "class",
        re.compile(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
        ),
    ),
    (
        frozenset({".rb"}),
        "class",
        re.compile(r"^\s*(?:class|module)\s+([A-Za-z_][A-Za-z0-9_:]*)\b"),
    ),
    (
        frozenset({".rb"}),
        "function",
        re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_][A-Za-z0-9_!?=]*)\b"),
    ),
    (
        frozenset({".php"}),
        "class",
        re.compile(
            r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
        ),
    ),
    (
        frozenset({".php"}),
        "function",
        re.compile(
            r"^\s*(?:public|private|protected|static|final|abstract|\s)*function\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*\("
        ),
    ),
    (
        _SOURCE_C_FAMILY_EXTENSIONS,
        "class",
        re.compile(r"^\s*(?:class|struct|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    ),
)

_SOURCE_SYMBOL_IGNORED_NAMES = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "sizeof",
        "function",
    }
)

def _source_c_function_name(line: str) -> str | None:
    # Split the return type from the name before validating either. A repeated
    # regex group containing whitespace can backtrack exponentially on even a
    # short non-function line. Each scan here is bounded by the line length.
    prefix, opening, tail = line.lstrip().partition("(")
    if not opening:
        return None
    prefix = prefix.rstrip()
    name_start = len(prefix)
    while name_start and (
        prefix[name_start - 1].isascii()
        and (prefix[name_start - 1].isalnum() or prefix[name_start - 1] == "_")
    ):
        name_start -= 1
    name = prefix[name_start:]
    if not name or not (name[0].isalpha() or name[0] == "_"):
        return None
    return_type = prefix[:name_start]
    if not return_type or not return_type[-1].isspace():
        return None
    # Reserve the separator, preserving the previous return-type grammar.
    if _SOURCE_C_RETURN_TYPE.fullmatch(return_type[:-1]) is None:
        return None
    if _SOURCE_C_FUNCTION_TAIL.match(opening + tail) is None:
        return None
    return name

def _source_symbol_matches_line(path: Path, line: str) -> list[tuple[str, str]]:
    extension = path.suffix.casefold()
    matches: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for extensions, kind, regex in _SOURCE_SYMBOL_REGEXES:
        if extension not in extensions:
            continue
        match = regex.search(line)
        if match is None:
            continue
        name = match.group(1)
        if name in _SOURCE_SYMBOL_IGNORED_NAMES:
            continue
        key = (kind, name)
        if key in seen:
            continue
        matches.append(key)
        seen.add(key)
    if extension in _SOURCE_C_FAMILY_EXTENSIONS:
        name = _source_c_function_name(line)
        if name is not None and name not in _SOURCE_SYMBOL_IGNORED_NAMES:
            matches.append(("function", name))
    return matches
