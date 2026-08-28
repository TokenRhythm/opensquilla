"""Rule-based routing flag computation.

Computes five boolean flags from raw text using keyword matching
and pattern detection. All rules are config-driven via router.yaml.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.router.features import ContextMetadata


@dataclass
class RoutingFlags:
    high_risk: bool
    long_context: bool
    debug: bool
    repo_arch: bool
    strict_format: bool


_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_LOG_BLOCK_RE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}.*\n){3,}"
    r"|"
    r"(^\[?(INFO|WARN|ERROR|DEBUG)\]?\s.*\n){3,}",
    re.MULTILINE,
)
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s\"'`(])([a-zA-Z_][\w.-]*/[\w./-]+\.[\w]+)",
    re.MULTILINE,
)


def _has_keyword(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _has_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def _code_block_total_len(text: str) -> int:
    return sum(len(m.group()) for m in _CODE_BLOCK_RE.finditer(text))


def _log_block_total_len(text: str) -> int:
    return sum(len(m.group()) for m in _LOG_BLOCK_RE.finditer(text))


def _file_ref_count(text: str) -> int:
    return len(_FILE_PATH_RE.findall(text))


def compute_flags(text: str, config: dict, context: ContextMetadata | None = None) -> RoutingFlags:
    """Compute routing flags from text using rules in config['flag_rules'].

    Optional context metadata can enhance flag detection — e.g. heavy accumulated
    context tokens trigger long_context even if the current message is short.
    """
    rules = config.get("flag_rules", {})

    hr = rules.get("high_risk", {})
    high_risk = (
        _has_keyword(text, hr.get("keywords_zh", []))
        or _has_keyword(text, hr.get("keywords_en", []))
    )

    dbg = rules.get("debug", {})
    debug = (
        _has_keyword(text, dbg.get("keywords", []))
        or _has_pattern(text, dbg.get("patterns", []))
    )

    ra = rules.get("repo_arch", {})
    repo_arch = _has_keyword(text, ra.get("keywords", []))

    sf = rules.get("strict_format", {})
    strict_format = _has_keyword(text, sf.get("keywords", []))

    lc = rules.get("long_context", {})
    char_thresh = lc.get("char_threshold", 6000)
    code_thresh = lc.get("code_block_threshold", 1500)
    log_thresh = lc.get("log_block_threshold", 1500)
    file_thresh = lc.get("file_ref_threshold", 2)

    long_context = (
        len(text) >= char_thresh
        or _code_block_total_len(text) >= code_thresh
        or _log_block_total_len(text) >= log_thresh
        or _file_ref_count(text) >= file_thresh
    )

    # Enhance long_context flag with context metadata
    if context is not None:
        ctx_rules = config.get("context_rules", {})
        heavy_tokens = ctx_rules.get("heavy_context_tokens", 2000)
        if context.context_tokens_est > heavy_tokens:
            long_context = True

    return RoutingFlags(
        high_risk=high_risk,
        long_context=long_context,
        debug=debug,
        repo_arch=repo_arch,
        strict_format=strict_format,
    )
