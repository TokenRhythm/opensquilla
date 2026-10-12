"""Post-processing controllers: derive thinking mode and prompt policy.

Pure functions — no I/O and no model runtime dependency. The bundled v4
router usually returns T0-T3 / P0-P2 directly; synthetic probability vectors
are retained only for safe fallback/default handling.
"""

from __future__ import annotations

from opensquilla.router_tiers import TEXT_TIERS

TIER_ORDER: list[str] = list(TEXT_TIERS)

_SYNTHETIC_PEAK = 0.85


def synthetic_one_hot(tier: str, dominant: float = _SYNTHETIC_PEAK) -> list[float]:
    """Return a synthetic 4-class probability vector peaking on *tier*."""
    n = len(TIER_ORDER)
    residual = (1.0 - dominant) / max(n - 1, 1)
    idx = TIER_ORDER.index(tier) if tier in TIER_ORDER else 1
    probs = [residual] * n
    probs[idx] = dominant
    return probs

DIFFICULTY_WEIGHTS: list[float] = [0.0, 1.0, 2.0, 3.0]

_THINKING_MODE_LEVEL: dict[str, str | None] = {
    "T0": None,
    "T1": "low",
    "T2": "medium",
    "T3": "high",
}

_P0_HINT_EN = "Answer directly, keep thinking short, avoid irrelevant expansion."
_P0_HINT_ZH = "直接作答，缩短思考长度，避免无关展开。"
_P0_HINT = _P0_HINT_EN

_PROMPT_HINTS: dict[str, dict[str, str]] = {
    "P0": {
        "hint_zh": _P0_HINT_ZH,
        "hint_en": _P0_HINT_EN,
    },
}

_CJK_RANGES = (
    ("\u4e00", "\u9fff"),
    ("\u3400", "\u4dbf"),
    ("\uf900", "\ufaff"),
)

_DEEP_FLAGS = frozenset({"high_risk", "debug", "long_context"})
_FULL_PROMPT_FLAGS = frozenset({"high_risk", "long_context", "debug", "strict_format"})
_COMPRESS_BLOCK_FLAGS = frozenset({"high_risk", "strict_format", "debug"})


def compute_difficulty(probs: list[float]) -> float:
    return sum(w * p for w, p in zip(DIFFICULTY_WEIGHTS, probs))


def compute_margin(probs: list[float]) -> float:
    if len(probs) < 2:
        return probs[0] if probs else 0.0
    ordered = sorted(probs, reverse=True)
    return max(0.0, ordered[0] - ordered[1])


def _has_any_flag(flags: dict | object | None, flag_names: frozenset[str]) -> bool:
    if flags is None:
        return False
    if isinstance(flags, dict):
        return any(flags.get(f) for f in flag_names)
    return any(getattr(flags, f, False) for f in flag_names)


def derive_thinking_mode(
    probs: list[float],
    flags: dict | object | None = None,
    *,
    t3_min_idx: int = 2,
    t0_max_idx: int = 0,
    t0_min_margin: float = 0.5,
    t1_max_idx: int = 1,
    t1_min_margin: float = 0.4,
) -> str:
    top1_idx = int(max(range(len(probs)), key=lambda i: probs[i]))
    margin = compute_margin(probs)

    if top1_idx >= len(TIER_ORDER) - 1:
        return "T3"
    if top1_idx >= t3_min_idx and _has_any_flag(flags, _DEEP_FLAGS):
        return "T3"
    if top1_idx <= t0_max_idx and margin >= t0_min_margin:
        return "T0"
    if top1_idx <= t1_max_idx and margin >= t1_min_margin:
        return "T1"
    return "T2"


def derive_prompt_policy(
    probs: list[float],
    flags: dict | object | None = None,
    *,
    max_difficulty: float = 0.8,
    min_margin: float = 0.4,
) -> str:
    if _has_any_flag(flags, _FULL_PROMPT_FLAGS):
        return "P2"
    difficulty = compute_difficulty(probs)
    margin = compute_margin(probs)
    if (
        difficulty <= max_difficulty
        and margin >= min_margin
        and not _has_any_flag(flags, _COMPRESS_BLOCK_FLAGS)
    ):
        return "P0"
    return "P1"


def normalize_decisions(thinking_mode: str, prompt_policy: str) -> tuple[str, str]:
    """Forbid THINK_DEEP + P0 (compress) — contradictory."""
    if thinking_mode in ("T2", "T3") and prompt_policy == "P0":
        return thinking_mode, "P1"
    return thinking_mode, prompt_policy


def thinking_mode_to_level(mode: str | None) -> str | None:
    if mode is None:
        return None
    return _THINKING_MODE_LEVEL.get(mode)


def prompt_hint_locale(text: str | None) -> str:
    """Return ``zh`` when the prompt is substantially CJK, else ``en``."""
    if not text:
        return "en"
    cjk_count = 0
    latin_count = 0
    for char in text:
        if any(start <= char <= end for start, end in _CJK_RANGES):
            cjk_count += 1
        elif char.isascii() and char.isalpha():
            latin_count += 1
    if cjk_count >= 2:
        return "zh"
    return "en"


def select_localized_prompt_hint(policy_cfg: dict, text: str | None) -> str | None:
    """Select hint_zh or hint_en from a prompt policy using current input language."""
    if prompt_hint_locale(text) == "zh":
        return policy_cfg.get("hint_zh") or policy_cfg.get("hint_en") or None
    return policy_cfg.get("hint_en") or policy_cfg.get("hint_zh") or None


def get_prompt_hint(policy: str | None, text: str | None = None) -> str | None:
    if policy is None:
        return None
    hint_cfg = _PROMPT_HINTS.get(policy)
    if not hint_cfg:
        return None
    if text is None:
        return hint_cfg.get("hint_en") or hint_cfg.get("hint_zh")
    return select_localized_prompt_hint(hint_cfg, text)
