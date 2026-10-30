"""Deterministic numeric88 extraction with no model-runtime dependency."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence

import numpy as np

from .registry import NUMERIC88_REGISTRY
from .text import canonicalize_text, extract_text_segments, sample_input


_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_JSON_RE = re.compile(r"\{[\s\S]*?[\"'][\w]+[\"']\s*:")
_YAML_RE = re.compile(r"^[\w_]+:\s+\S", re.MULTILINE)
_CSV_RE = re.compile(r"^[^,\n]+,[^,\n]+,[^,\n]+", re.MULTILINE)
_TABLE_RE = re.compile(r"\|.*\|.*\|")
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s\"'`(])([a-zA-Z_][\w.-]*/[\w./-]+\.[\w]+)", re.MULTILINE
)
_URL_RE = re.compile(r"https?://\S+")
_LOG_RE = re.compile(
    r"(\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}.*\n){3,}"
    r"|(^\[?(INFO|WARN|ERROR|DEBUG)\]?\s.*\n){3,}",
    re.MULTILINE,
)
_SHELL_RE = re.compile(r"^\$\s+\w|^>\s+\w|```(?:bash|sh|shell)", re.MULTILINE)
_TRACEBACK_RE = re.compile(r"Traceback \(most recent|stderr:|\.py\", line \d+")
_BULLET_RE = re.compile(r"^[\s]*[-*]\s", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^[\s]*\d+[.)、]\s", re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`]{4,}`")
_STEPS_RE = re.compile(r"^\s*\d+[.、]\s", re.MULTILINE)

_KEYWORDS = {
    "debug": ("error", "bug", "exception", "traceback", "failed", "root cause", "报错", "根因", "修复", "stack trace", "debug"),
    "research": ("调研", "research", "对比", "compare", "survey", "分析报告", "competitive analysis", "综述"),
    "architecture": ("architecture", "架构", "重构", "refactor", "monorepo", "codebase", "module", "dependency"),
    "compare": ("对比", "compare", "audit", "审计", "review", "评估"),
    "planning": ("plan", "规划", "roadmap", "设计方案", "workflow", "pipeline", "步骤", "step by step"),
    "strict": ("json", "yaml", "csv", "schema", "只返回", "不要解释", "按格式", "only return", "no explanation"),
    "risk": ("deploy", "rollback", "migration", "delete", "overwrite", "production", "生产", "部署", "删除", "客户", "法务", "财务"),
    "production": ("production", "生产", "prod", "线上", "正式环境"),
    "customer": ("customer", "客户", "用户邮件", "client"),
    "delete": ("delete", "remove", "drop", "truncate", "删除", "清空", "覆盖", "overwrite"),
    "formal": ("formal", "正式", "official", "公文", "合同", "法律"),
    "constraint": ("必须", "不能", "不要", "只能", "must", "shall", "required", "forbidden", "不允许", "至少", "最多"),
    "teaching": ("how does", "explain", "what is", "why does", "how to", "教我", "解释", "为什么", "怎么", "是什么", "how can", "tell me about", "walk me through", "介绍", "说明"),
    "implementation": ("implement", "write function", "write a", "create a", "写个", "实现", "用法", "帮我写", "生成代码", "add a", "build a", "make a", "写一个", "编写"),
}

_CLARIFY_RE = re.compile(r"(?:能否|请\s*提供|需要(?:更多|具体).{0,8}信息|could you (?:clarify|provide)|please (?:specify|provide)|clarify which)", re.I)
_REFUSAL_RE = re.compile(r"(?:I cannot|I can't help|对不起.{0,5}无法|抱歉.{0,5}不能|作为(?:AI|大语言模型))", re.I)
_SELF_DOUBT_RE = re.compile(r"(?:我不(?:确定|清楚)|可能(?:不太|不一定)|not sure|might not be|I'm not entirely)", re.I)
_CONTINUE_RE = re.compile(r"(?:请继续|继续|接着|续写|展开一下|再说|more|continue|go on|carry on|next)", re.I)
_REASONING_RE = re.compile(r"(?:why|compare|trade[ -]?off|analy[sz]e|architecture|reasoning|design|解释|原因|对比|分析|架构|设计|权衡)", re.I)

_TIER_INDEX = {"C0": 0, "C1": 1, "C2": 2, "C3": 3, "R0": 0, "R1": 1, "R2": 2, "R3": 3}
_TRAJECTORIES = (
    "cold_start",
    "stable_low",
    "stable_high",
    "escalating",
    "descalating",
    "oscillating",
    "unclear",
    "mixed",
)


def _keyword_count(text: str, name: str) -> float:
    lowered = text.lower()
    return float(sum(keyword.lower() in lowered for keyword in _KEYWORDS[name]))


def _char_ratios(text: str) -> tuple[float, float, float]:
    if not text:
        return 0.0, 0.0, 0.0
    size = len(text)
    zh = sum("\u4e00" <= char <= "\u9fff" for char in text)
    latin = sum(char.isascii() and char.isalpha() for char in text)
    code = sum(char in "{}[]();=<>|&!@#$%^*~`\\" for char in text)
    return zh / size, latin / size, code / size


def _as_nonnegative(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _log_usage(usage: Mapping[str, object], key: str) -> float:
    return math.log1p(_as_nonnegative(usage.get(key))) / 10.0


def extract_rules48(current_request: str) -> np.ndarray:
    text = canonicalize_text(current_request)
    words = text.split()
    lines = text.split("\n")
    zh, latin, code = _char_ratios(text)
    code_blocks = _CODE_BLOCK_RE.findall(text)
    file_refs = set(_FILE_PATH_RE.findall(text))
    keyword_values = {name: _keyword_count(text, name) for name in _KEYWORDS}
    quoted = re.findall(r"[\"'`](.*?)[\"'`]", text)
    diversity = len({word.lower() for word in words}) / max(len(words), 1)
    total_core_keywords = sum(keyword_values[name] for name in ("debug", "research", "architecture", "compare", "planning", "strict", "risk"))
    values = (
        len(text),
        len(words),
        len(lines),
        len(text) / max(len(lines), 1),
        zh,
        latin,
        code,
        float(zh > 0.1 and latin > 0.1),
        float(bool(code_blocks)),
        len(code_blocks),
        sum(len(block) for block in code_blocks),
        float(bool(_JSON_RE.search(text))),
        float(bool(_YAML_RE.search(text))),
        float(bool(_CSV_RE.search(text))),
        float(bool(_TABLE_RE.search(text))),
        text.count("?") + text.count("？"),
        text.count("!") + text.count("！"),
        len(_BULLET_RE.findall(text)),
        len(_NUMBERED_RE.findall(text)),
        keyword_values["debug"],
        keyword_values["research"],
        keyword_values["architecture"],
        keyword_values["compare"],
        keyword_values["planning"],
        keyword_values["strict"],
        keyword_values["risk"],
        keyword_values["production"],
        keyword_values["customer"],
        keyword_values["delete"],
        keyword_values["formal"],
        float(bool(file_refs)),
        float(bool(_URL_RE.search(text))),
        float(bool(_LOG_RE.search(text))),
        float(bool(_SHELL_RE.search(text))),
        float(bool(_TRACEBACK_RE.search(text))),
        keyword_values["constraint"],
        sum(len(value) for value in quoted) / max(len(text), 1),
        diversity,
        keyword_values["teaching"],
        keyword_values["implementation"],
        float(len(file_refs) == 0),
        float(1 <= len(file_refs) <= 2),
        float(len(file_refs) >= 3),
        float(bool(code_blocks) and keyword_values["debug"] == 0),
        float(len(text) < 200),
        float(200 <= len(text) <= 1000),
        float(len(text) > 1000),
        float(total_core_keywords < 2),
    )
    result = np.asarray(values, dtype=np.float32)
    if result.shape != (48,):
        raise AssertionError(f"rules48 shape drifted to {result.shape}")
    return result


def _route_index(entry: Mapping[str, object]) -> int:
    value = entry.get("tier_id") or entry.get("tier") or entry.get("route_class")
    normalized = str(value).upper()
    if normalized not in _TIER_INDEX:
        raise ValueError(f"route history entry has invalid tier: {value!r}")
    return _TIER_INDEX[normalized]


def _finite_number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _trajectory(indices: list[int], difficulties: list[float]) -> str:
    if not indices:
        return "cold_start"
    if len(indices) < 2:
        return "unclear"
    if all(index <= 1 for index in indices):
        return "stable_low"
    if all(index >= 2 for index in indices):
        return "stable_high"
    signs = []
    for before, after in zip(difficulties, difficulties[1:]):
        delta = after - before
        signs.append(1 if delta > 0.3 else (-1 if delta < -0.3 else 0))
    nonzero = [sign for sign in signs if sign]
    if len(nonzero) >= 2 and all(sign == 1 for sign in nonzero):
        return "escalating"
    if len(nonzero) >= 2 and all(sign == -1 for sign in nonzero):
        return "descalating"
    changes = sum(left != right for left, right in zip(nonzero, nonzero[1:]))
    return "oscillating" if changes >= 2 else "mixed"


def extract_route_history16(route_history: object) -> np.ndarray:
    if route_history is None:
        entries: list[Mapping[str, object]] = []
    else:
        if isinstance(route_history, (str, bytes)) or not isinstance(route_history, Sequence):
            raise TypeError("input.route_history must be a sequence")
        entries = []
        for value in route_history[-5:]:
            if not isinstance(value, Mapping):
                raise TypeError("each route_history entry must be a mapping")
            entries.append(value)
    indices = [_route_index(entry) for entry in entries]
    difficulties = [_finite_number(entry.get("difficulty", entry.get("difficulty_score", 0.0))) for entry in entries]
    margins = [_finite_number(entry.get("margin", 0.0)) for entry in entries]
    result = np.zeros(16, dtype=np.float32)
    if entries:
        result[0] = indices[-1]
        result[1] = difficulties[-1]
        result[2] = margins[-1]
        result[3] = max(indices)
        result[4] = len(entries)
        result[5] = len(entries)
        counts = Counter(indices)
        top_count = max(counts.values())
        result[6] = max(index for index, count in counts.items() if count == top_count)
        result[7] = sum(left != right for left, right in zip(indices, indices[1:]))
    else:
        result[[0, 3, 6]] = -1.0
    result[8 + _TRAJECTORIES.index(_trajectory(indices, difficulties))] = 1.0
    return result


def extract_previous_signals19(
    current_request: str,
    previous_answer: object,
    previous_usage: object,
) -> np.ndarray:
    current = canonicalize_text(current_request)
    previous = canonicalize_text(previous_answer)
    if previous_usage is None:
        usage: Mapping[str, object] = {}
    elif isinstance(previous_usage, Mapping):
        usage = previous_usage
    else:
        raise TypeError("input.previous_usage must be a mapping or null")

    assistant = np.zeros(12, dtype=np.float32)
    if previous:
        input_tokens = max(_as_nonnegative(usage.get("input_tokens")), 1.0)
        assistant[:] = (
            1.0,
            float(bool(_CLARIFY_RE.search(previous))),
            float(bool(_REFUSAL_RE.search(previous))),
            float(bool(_SELF_DOUBT_RE.search(previous))),
            float("```" in previous or bool(_INLINE_CODE_RE.search(previous))),
            float(bool(_STEPS_RE.search(previous))),
            _log_usage(usage, "output_tokens"),
            _log_usage(usage, "reasoning_tokens"),
            _log_usage(usage, "duration_ms"),
            min(len(previous) / max(len(current), 1), 5.0) / 5.0,
            _char_ratios(previous)[0],
            min(_as_nonnegative(usage.get("cached_tokens")) / input_tokens, 1.0),
        )
    stripped = current.strip()
    continuation = np.asarray(
        (
            float(bool(stripped) and len(stripped) <= 24 and bool(_CONTINUE_RE.search(stripped))),
            _log_usage(usage, "output_tokens"),
        ),
        dtype=np.float32,
    )
    qmarks = current.count("?") + current.count("？")
    reasoning = np.asarray(
        (
            float(bool(_REASONING_RE.search(current))),
            min(qmarks / max(len(current), 1) * 20.0, 1.0),
            math.log1p(len(current)) / 10.0,
            _log_usage(usage, "reasoning_tokens"),
            _log_usage(usage, "duration_ms"),
        ),
        dtype=np.float32,
    )
    result = np.concatenate((assistant, continuation, reasoning)).astype(np.float32)
    if result.shape != (19,):
        raise AssertionError(f"previous_signals19 shape drifted to {result.shape}")
    return result


def extract_context5(row: Mapping[str, object]) -> np.ndarray:
    payload = sample_input(row)
    segments = extract_text_segments(row)
    route_history = payload.get("route_history") or []
    if isinstance(route_history, (str, bytes)) or not isinstance(route_history, Sequence):
        raise TypeError("input.route_history must be a sequence")
    turn_index = min(len(route_history[-5:]), 20)
    raw_context = payload.get("context") or {}
    if not isinstance(raw_context, Mapping):
        raise TypeError("input.context must be a mapping or null")
    estimated = raw_context.get("context_tokens_est")
    if estimated is None:
        history_chars = sum(len(canonicalize_text(value)) for value in (payload.get("history_user") or []))
        estimated_tokens = (
            len(segments.current_request) + history_chars + len(segments.previous_answer)
        ) // 4
    else:
        estimated_tokens = int(max(_finite_number(estimated), 0.0))
    result = np.asarray(
        (
            turn_index / 20.0,
            min(estimated_tokens, 20_000) / 20_000.0,
            float("```" in segments.current_request),
            float(turn_index >= 4),
            float(estimated_tokens > 2_000),
        ),
        dtype=np.float32,
    )
    return result


def extract_numeric88(row: Mapping[str, object]) -> np.ndarray:
    payload = sample_input(row)
    segments = extract_text_segments(row)
    result = np.concatenate(
        (
            extract_rules48(segments.current_request),
            extract_route_history16(payload.get("route_history")),
            extract_previous_signals19(
                segments.current_request,
                payload.get("previous_answer"),
                payload.get("previous_usage"),
            ),
            extract_context5(row),
        )
    ).astype(np.float32)
    if result.shape != (len(NUMERIC88_REGISTRY),):
        raise AssertionError(f"numeric88 shape drifted to {result.shape}")
    if not np.isfinite(result).all():
        raise ValueError("numeric88 contains a non-finite value")
    return result
