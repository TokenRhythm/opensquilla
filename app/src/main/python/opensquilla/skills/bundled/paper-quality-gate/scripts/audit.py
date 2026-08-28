"""Deterministic publication-safety checks for generated paper packages."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

_RESULT_SECTION_RE = re.compile(
    r"(?:\\begin\{abstract\}|\\section\*?\{(?:"
    r"Experiments?|Experimental\s+Evaluation|Evaluations?|Results?|Findings?|"
    r"Discussion|Conclusion|实验|评估|结果|讨论|结论)[^}]*\})"
    r"[\s\S]*?(?=\\end\{abstract\}|\\section\*?\{|\\end\{document\}|\Z)",
    re.IGNORECASE,
)
_UNSUPPORTED_RESULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "reported significance statistic",
        re.compile(r"\b(?:p\s*[<=>]\s*0?\.\d+|cohen(?:'s)?\s+d\s*[=:]?\s*\d)", re.I),
    ),
    (
        "completed finding claim",
        re.compile(
            r"\b(?:we|our\s+(?:study|evaluation|experiment))\s+"
            r"(?:found|observed|demonstrated|showed|achieved|outperformed|confirmed)\b",
            re.I,
        ),
    ),
    (
        "results presented as observed",
        re.compile(
            r"\b(?:results?|experiments?|evaluation)\s+"
            r"(?:show|shows|showed|demonstrate|demonstrates|demonstrated|"
            r"confirm|confirms|confirmed|indicate|indicates|indicated|reveal|reveals|revealed)\b",
            re.I,
        ),
    ),
    (
        "numeric improvement claim",
        re.compile(
            r"\b(?:improv(?:e|ed|ement)|outperform(?:s|ed)?)\b[^.\n]{0,50}"
            r"\b\d+(?:\.\d+)?\s*(?:\\?%|points?|x)(?=\s|[,.;:)])",
            re.I,
        ),
    ),
    (
        "predicted numeric result without evidence",
        re.compile(
            r"\b(?:expect(?:ed)?|predict(?:ed)?|project(?:ed)?|anticipat(?:e|ed)|"
            r"estimat(?:e|ed)|hypothesi[sz](?:e|ed)?|hypothesis|likely|may|"
            r"might|could|would|should|will)\b"
            r"[^.\n]{0,120}?\b(?:improv\w*|outperform\w*|increase\w*|decrease\w*|"
            r"reduce\w*|drop\w*|rise\w*|fall\w*|gain\w*)\b[^.\n]{0,60}?"
            r"\b\d+(?:\.\d+)?\s*(?:\\?%|percentage\s+points?|points?|x|ms)"
            r"(?=\s|[,.;:)])",
            re.I,
        ),
    ),
    (
        "completed sample claim",
        re.compile(
            r"\b(?:we\s+(?:collected|recruited|analyzed|sampled|extracted)|"
            r"dataset\s+(?:contains|contained|comprised|included))\s+"
            r"\d[\d,]*\b",
            re.I,
        ),
    ),
    (
        "unsupported novelty claim",
        re.compile(r"\bfirst\s+experimental\s+evidence\b", re.I),
    ),
    (
        "将实验结果表述为既成事实",
        re.compile(
            r"(?:结果|实验|评估)(?:表明|显示|证明|证实)|"
            r"我们(?:发现|观察到|证明|证实)|首次实验证据|显著(?:提高|改善|优于)"
        ),
    ),
    (
        "无证据的具体预测结果数字",
        re.compile(
            r"(?:预期|预计|预测|估计|有望|可能|将会|假设|目标|可使|可以|能够|"
            r"(?<!功)(?<!性)能(?=将|使|把|让|在|于))"
            r"[^。！？\n]{0,120}?"
            r"(?:提高|提升|改善|增加|上升|降低|减少|下降|优于|高出|"
            r"达到|保持|维持|降至|升至)"
            r"[^。！？\n]{0,50}?"
            r"\d+(?:\.\d+)?\s*(?:\\?%|个百分点|倍|毫秒|ms|秒)"
        ),
    ),
)
_NO_RESULTS_DISCLOSURE_RE = re.compile(
    r"\b(?:no empirical results? (?:were )?(?:provided|supplied|available)|"
    r"empirical results? (?:are )?not yet available|planned evaluation|"
    r"will be evaluated|awaiting (?:experimental )?data)\b|"
    r"(?:未提供|尚无|没有)(?:真实)?实验(?:数据|结果)|计划(?:进行)?评估|"
    r"待(?:补充|提供)实验(?:数据|结果)|待实验确定|结果值待定",
    re.I,
)
_LITERAL_GREEK_RE = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]")
_UNICODE_EN_DASH_RE = re.compile(r"\u2013")
_UNICODE_EM_DASH_RUN_RE = re.compile(r"\u2014+")
_CONDITIONAL_CLAIM_PREFIX_RE = re.compile(
    r"(?:\b(?:if|whether|hypothesis(?:\s+is|\s+that)?|"
    r"we\s+hypothesi[sz]e(?:\s+that)?|to\s+test\s+whether)\b|"
    r"若(?:经)?|如果(?:经)?|假如(?:经)?|能否|是否|假设|"
    r"待(?:实验)?(?:验证|检验)|尚待(?:验证|检验)?|有待(?:验证|检验)?|"
    r"计划(?:检验|验证)|预期|预计|有望|可能)"
    r"[^.!?。！？；;\n]{0,80}$",
    re.I,
)
_PLANNED_SIGNIFICANCE_THRESHOLD_RE = re.compile(
    r"(?:\b(?:significance\s+(?:level|threshold)|decision\s+threshold|"
    r"planned\s+(?:analysis|test)|will\s+use)\b|"
    r"显著性(?:水平|阈值)|判定(?:标准|阈值)|计划(?:分析|检验))",
    re.I,
)
_QUALITATIVE_TIME_TO_THRESHOLD_RE = re.compile(
    r"(?:\b(?:fewer|less|an?\s+unknown\s+number\s+of|TBD)\s+"
    r"(?:rounds?|epochs?|steps?)\b|"
    r"(?:较少|更少|最少|若干|未知|待定)(?:个)?(?:轮次|轮|步))",
    re.I,
)
_METRIC_THRESHOLD_RE = re.compile(
    r"(?:\b(?:reach|reaching|converge(?:s|d|nce)?\s+to)\s+"
    r"\d+(?:\.\d+)?\s*\\?%\s*(?:accuracy|completion\s+rate)\b|"
    r"(?:达到|收敛至|收敛到)\s*\d+(?:\.\d+)?\s*\\?%\s*"
    r"(?:的)?(?:准确率|精度|任务完成率))",
    re.I,
)
_CONCRETE_TIME_BUDGET_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:rounds?|epochs?|steps?)\b|"
    r"\d+(?:\.\d+)?\s*(?:个)?(?:轮次|轮|步))",
    re.I,
)
_METRIC_PERCENT_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*\\?%\s*(?:的)?"
    r"(?P<metric>accuracy|completion\s+rate|准确率|精度|任务完成率)",
    re.I,
)
_DECLARED_METRIC_TARGET_CUE_RE = re.compile(
    r"(?:\b(?:define|defined|set|use|predefined|target|threshold|criterion)\b|"
    r"定义|设定|预设|预定义|目标|阈值|判定标准)",
    re.I,
)
_CAPTION_SENTENCE_RE = re.compile(r"(?:\d\.\d|[^.!?。！？；;\n])+")
_CAPTION_HYPOTHESIS_FRAME_RE = re.compile(
    r"(?:\b(?:hypothesis|hypothesi[sz](?:e|ed|es|ing)|whether|"
    r"will|would|may|might|could|expected|anticipated|to\s+be\s+tested)\b|"
    r"假设|能否|是否|将|预期|预计|有望|可能|待(?:实验)?(?:验证|检验))",
    re.I,
)
_ENGLISH_CAPTION_RESULT_CLAIM_RE = re.compile(
    r"\b(?:ours|(?:the\s+)?(?:proposed|presented|our|this)\s+"
    r"(?:method|approach|model|system)|(?:the\s+)?"
    r"(?:method|approach|model|system|variant|ablation|baseline))\b"
    r"[^.!?;\n]{0,120}?"
    r"(?:\b(?:outperform(?:s|ed|ing)?|exceed(?:s|ed|ing)?|beat(?:s|en|ing)?|"
    r"achiev(?:e|es|ed|ing)|attain(?:s|ed|ing)?|obtain(?:s|ed|ing)?|"
    r"deliver(?:s|ed|ing)?|yield(?:s|ed|ing)?|"
    r"retain(?:s|ed|ing)?|maintain(?:s|ed|ing)?|"
    r"improv(?:e|es|ed|ing)|reduc(?:e|es|ed|ing)|"
    r"lower(?:s|ed|ing)?|increas(?:e|es|ed|ing)|decreas(?:e|es|ed|ing))\b|"
    r"\bconverg(?:e|es|ed|ing)\b[^.!?;\n]{0,30}\b(?:faster|slower)\b|"
    r"\b(?:is|was|remains?|retains?|has|shows?)\b[^.!?;\n]{0,30}"
    r"\b(?:higher|lower|better|worse|faster|slower|best|highest|lowest)\b)",
    re.I,
)
_CHINESE_CAPTION_RESULT_CLAIM_RE = re.compile(
    r"(?:所提方法|所提出的?方法|本文(?:的)?(?:方法|模型|方案)|"
    r"本研究(?:的)?(?:方法|模型|方案)|我们(?:的)?(?:方法|模型|方案)|"
    r"Ours|完整(?:方法|模型)|消融(?:变体|方法|模型)?|"
    r"基线(?:方法|模型)?|各方法)"
    r"[^。！？；\n]{0,120}?"
    r"(?:达到|取得|实现|保持|维持|优于|超过|胜过|高于|低于|快于|慢于|"
    r"领先于|落后于|减少|降低|提高|提升|改善|"
    r"收敛(?:速度)?(?:显著|明显|更)?(?:快于|慢于|更快|更慢)|"
    r"(?:表现|性能)(?:最佳|最好)|最低|最高)",
    re.I,
)
_CONDITIONAL_RESULT_LABELS = {
    "completed finding claim",
    "results presented as observed",
    "将实验结果表述为既成事实",
}
_OWN_METHOD_RE = re.compile(
    r"\b(?:ours|our\s+(?:method|approach|framework|model|system|algorithm)|"
    r"the\s+(?:proposed|presented)\s+(?:method|approach|framework|model|system)|"
    r"this\s+(?:method|approach|framework|model|system|algorithm))\b|"
    r"本文(?:方法|模型|框架|方案|算法)?|本研究|所提(?:方法|模型|框架|方案|算法)|"
    r"我们(?:的)?(?:方法|模型|框架|方案|算法)|该(?:方法|机制|模型|框架|方案|算法)",
    re.I,
)
_OWN_METHOD_OUTCOME_RE = re.compile(
    r"\b(?:accuracy|precision|recall|f1|score|latency|cost|overhead|"
    r"throughput|utility|performance|efficiency|convergence|communication)\b|"
    r"精度|准确率|召回率|分数|延迟|开销|吞吐|效用|性能|效率|收敛|通信",
    re.I,
)
_OWN_METHOD_FORECAST_MAGNITUDE_RE = re.compile(
    r"\b(?:expect(?:ed)?|predict(?:ed)?|project(?:ed)?|anticipat(?:e|ed)|"
    r"estimat(?:e|ed)|hypothesi[sz](?:e|ed)?|hypothesis|likely|may|might|"
    r"could|would|should|will)\b[^.!?。！？]{0,100}?"
    r"(?:less\s+than|more\s+than|at\s+least|at\s+most|below|under|over|"
    r"improv\w*|reduc\w*|increase\w*|decrease\w*)[^.!?。！？]{0,30}?"
    r"\d+(?:\.\d+)?\s*\\?%|"
    r"(?:预期|预计|预测|估计|有望|可能|将会|假设|目标|可使|可以|能够|"
    r"(?<!功)(?<!性)能(?=将|使|把|让|在|于))[^。！？]{0,100}?"
    r"(?:不超过|不低于|小于|低于|高于|超过|至少|至多|提升|提高|改善|"
    r"增加|上升|降低|减少|下降|优于|高出|达到|保持|维持|降至|升至|恶化)"
    r"[^。！？]{0,30}?\d+(?:\.\d+)?\s*\\?%",
    re.I,
)
_OWN_METHOD_COMPARATIVE_MAGNITUDE_RE = re.compile(
    r"(?:\b(?:ours|our\s+(?:method|approach|framework|model|system)|"
    r"the\s+proposed\s+(?:method|approach|framework|model|system))\b|"
    r"本文(?:方法|模型|框架|方案|算法)?|所提(?:方法|模型|框架|方案|算法)|"
    r"我们(?:的)?(?:方法|模型|框架|方案|算法)|该(?:方法|机制|模型|框架|方案|算法))"
    r"[^.!?。！？]{0,140}?"
    r"(?:improv\w*|outperform\w*|increase\w*|decrease\w*|reduc\w*|"
    r"提升|提高|改善|增加|上升|降低|减少|下降|优于|高出|降至|升至|恶化)"
    r"[^.!?。！？]{0,40}?\d+(?:\.\d+)?\s*\\?%",
    re.I,
)


def _fail(reasons: list[str]) -> int:
    print("QUALITY_GATE: block")
    print("BLOCKERS:")
    for reason in reasons:
        print(f"- {reason}")
    return 2


def _verdict(text: str, marker: str) -> str | None:
    match = re.search(rf"\b{re.escape(marker)}\s*:\s*(pass|warn|block)\b", text, re.I)
    return match.group(1).lower() if match else None


def _manuscript_text(package: str) -> str:
    path_match = re.search(r"^MANUSCRIPT_PATH:\s*(.+)$", package, re.MULTILINE)
    if path_match:
        path = Path(path_match.group(1).strip()).expanduser()
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")

    block = re.search(
        r"MANUSCRIPT_TEX:\s*([\s\S]+?)(?:REFERENCES_BIB:|COMPILE_NOTES:|\Z)",
        package,
    )
    if block:
        return block.group(1).strip()

    raw = re.search(r"(\\documentclass[\s\S]+?\\end\{document\})", package)
    return raw.group(1).strip() if raw else ""


def _without_tex_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%[^\n]*", "", text)


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = max(
        text.rfind("\n", 0, start),
        text.rfind(".", 0, start),
        text.rfind("!", 0, start),
        text.rfind("?", 0, start),
        text.rfind("。", 0, start),
        text.rfind("！", 0, start),
        text.rfind("？", 0, start),
        text.rfind(";", 0, start),
        text.rfind("；", 0, start),
    )
    right_candidates = [
        position
        for separator in ("\n", ".", "!", "?", "。", "！", "？", ";", "；")
        if (position := text.find(separator, end)) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(text)
    return left + 1, right


def _sentence_containing(text: str, start: int, end: int) -> str:
    left, right = _sentence_bounds(text, start, end)
    return text[left:right]


def _is_explicitly_conditional_claim(text: str, match: re.Match[str]) -> bool:
    sentence_start, _ = _sentence_bounds(text, match.start(), match.end())
    prefix = text[sentence_start : match.start()]
    return bool(_CONDITIONAL_CLAIM_PREFIX_RE.search(prefix))


def _is_planned_significance_threshold(text: str, match: re.Match[str]) -> bool:
    sentence = _sentence_containing(text, match.start(), match.end())
    return bool(_PLANNED_SIGNIFICANCE_THRESHOLD_RE.search(sentence))


def _is_qualitative_time_to_metric_threshold(text: str, match: re.Match[str]) -> bool:
    """Allow a predefined target metric when the unknown is a nonnumeric time-to-target.

    For example, ``may reach 50% accuracy in fewer rounds`` names the convergence
    criterion (50%) but does not invent the unknown round count. ``within 20 rounds``
    remains a concrete forecast and is not exempted.
    """

    sentence = _sentence_containing(text, match.start(), match.end())
    qualitative_time_to_target = bool(
        _METRIC_THRESHOLD_RE.search(sentence)
        and _QUALITATIVE_TIME_TO_THRESHOLD_RE.search(sentence)
        and not _CONCRETE_TIME_BUDGET_RE.search(sentence)
    )
    if qualitative_time_to_target:
        return True

    candidate_metrics = {
        (candidate.group("value"), candidate.group("metric").casefold())
        for candidate in _METRIC_PERCENT_RE.finditer(sentence)
    }
    if not candidate_metrics or _CONCRETE_TIME_BUDGET_RE.search(sentence):
        return False
    declared_metrics: set[tuple[str, str]] = set()
    for declared_sentence in _CAPTION_SENTENCE_RE.findall(text):
        if not _DECLARED_METRIC_TARGET_CUE_RE.search(declared_sentence):
            continue
        declared_metrics.update(
            (candidate.group("value"), candidate.group("metric").casefold())
            for candidate in _METRIC_PERCENT_RE.finditer(declared_sentence)
        )
    return bool(candidate_metrics & declared_metrics)


def _latex_command_arguments(text: str, command: str) -> list[str]:
    """Extract balanced braced arguments for a LaTeX command.

    Captions can contain nested formatting commands, so a flat regular
    expression is not sufficient. Malformed commands are ignored here and
    remain the compiler's responsibility.
    """

    arguments: list[str] = []
    needle = f"\\{command}"
    cursor = 0
    while (start := text.find(needle, cursor)) >= 0:
        cursor = start + len(needle)
        if cursor < len(text) and text[cursor].isalpha():
            continue
        if cursor < len(text) and text[cursor] == "*":
            cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor < len(text) and text[cursor] == "[":
            bracket_depth = 1
            cursor += 1
            while cursor < len(text) and bracket_depth:
                if text[cursor] == "\\" and cursor + 1 < len(text):
                    cursor += 2
                    continue
                if text[cursor] == "[":
                    bracket_depth += 1
                elif text[cursor] == "]":
                    bracket_depth -= 1
                cursor += 1
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
        if cursor >= len(text) or text[cursor] != "{":
            continue
        body_start = cursor + 1
        depth = 1
        cursor += 1
        while cursor < len(text) and depth:
            if text[cursor] == "\\" and cursor + 1 < len(text):
                cursor += 2
                continue
            if text[cursor] == "{":
                depth += 1
            elif text[cursor] == "}":
                depth -= 1
                if depth == 0:
                    arguments.append(text[body_start:cursor])
                    cursor += 1
                    break
            cursor += 1
    return arguments


def _caption_plain_text(caption: str) -> str:
    text = caption.replace(r"\%", "%")
    for _ in range(3):
        text = re.sub(
            r"\\(?:textbf|textit|emph|mathrm|mathbf|operatorname)\s*\{([^{}]*)\}",
            r"\1",
            text,
        )
    text = re.sub(r"\\(?:cite|ref|label)\s*\{[^{}]*\}", " ", text)
    text = re.sub(r"\\[A-Za-z@]+\*?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    return re.sub(r"\s+", " ", text).strip()


def _caption_claim_blockers(manuscript: str) -> list[str]:
    blockers: list[str] = []
    for raw_caption in _latex_command_arguments(manuscript, "caption"):
        caption = _caption_plain_text(raw_caption)
        for sentence_match in _CAPTION_SENTENCE_RE.finditer(caption):
            sentence = sentence_match.group(0).strip()
            if not sentence:
                continue
            claim = _ENGLISH_CAPTION_RESULT_CLAIM_RE.search(sentence)
            if claim is None:
                claim = _CHINESE_CAPTION_RESULT_CLAIM_RE.search(sentence)
            if claim is None or _CAPTION_HYPOTHESIS_FRAME_RE.search(sentence):
                continue
            excerpt = re.sub(r"\s+", " ", raw_caption).strip()[:160]
            blockers.append(
                "categorical observed result claim in evidence-free caption: " + excerpt
            )
            break
    return blockers


def audit(payload: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    length_gate = str(payload.get("length_gate") or "")
    citation_gate = str(payload.get("citation_gate") or "")
    length_verdict = _verdict(length_gate, "LENGTH_GATE")
    citation_verdict = _verdict(citation_gate, "INTEGRITY")

    if length_verdict is None:
        blockers.append("length gate did not emit LENGTH_GATE: pass|warn|block")
    elif length_verdict == "block":
        blockers.append("length gate blocked compilation")
    if citation_verdict is None:
        blockers.append("citation gate did not emit INTEGRITY: pass|warn|block")
    elif citation_verdict == "block":
        blockers.append("citation integrity gate blocked compilation")

    paper_contract = str(payload.get("paper_contract") or "")
    evidence_match = re.search(
        r"\bEVIDENCE_STATUS\s*:\s*(supplied|not_supplied)\b",
        paper_contract,
        re.I,
    )
    evidence_status = evidence_match.group(1).lower() if evidence_match else "not_supplied"
    manuscript = _manuscript_text(str(payload.get("manuscript_package") or ""))
    if not manuscript:
        blockers.append("manuscript package contains no readable MANUSCRIPT_TEX or MANUSCRIPT_PATH")
        return blockers

    visible_manuscript = _without_tex_comments(manuscript)
    literal_greek = sorted(set(_LITERAL_GREEK_RE.findall(visible_manuscript)))
    if literal_greek:
        rendered = ", ".join(f"{glyph} (U+{ord(glyph):04X})" for glyph in literal_greek)
        blockers.append(
            "literal Unicode Greek math glyphs must use named LaTeX macros in math mode: "
            + rendered
        )
    unicode_dashes = sorted(
        set(_UNICODE_EN_DASH_RE.findall(visible_manuscript))
        | {
            "\u2014"
            for run in _UNICODE_EM_DASH_RUN_RE.finditer(visible_manuscript)
            if len(run.group(0)) != 2
        }
    )
    if unicode_dashes:
        rendered = ", ".join(f"{glyph} (U+{ord(glyph):04X})" for glyph in unicode_dashes)
        blockers.append(
            "Latin-style Unicode en/em dashes must use LaTeX -- / ---; "
            "paired Chinese em dashes remain valid native-language punctuation: "
            + rendered
        )

    if evidence_status == "not_supplied":
        result_regions = "\n".join(_RESULT_SECTION_RE.findall(manuscript))
        own_method_sentences = "\n".join(
            sentence.group(0)
            for sentence in re.finditer(
                r"[^.!?。！？]+(?:[.!?。！？]+|\Z)", visible_manuscript
            )
            if _OWN_METHOD_RE.search(sentence.group(0))
        )
        result_regions = f"{result_regions}\n{own_method_sentences}"
        if not _NO_RESULTS_DISCLOSURE_RE.search(manuscript):
            blockers.append(
                "no empirical evidence was supplied, but the manuscript does not disclose "
                "that results are planned or unavailable"
            )
        blockers.extend(_caption_claim_blockers(visible_manuscript))
        for label, pattern in _UNSUPPORTED_RESULT_PATTERNS:
            for match in pattern.finditer(result_regions):
                if label in _CONDITIONAL_RESULT_LABELS and _is_explicitly_conditional_claim(
                    result_regions, match
                ):
                    continue
                if label == "reported significance statistic" and (
                    _is_planned_significance_threshold(result_regions, match)
                ):
                    continue
                if label in {
                    "predicted numeric result without evidence",
                    "无证据的具体预测结果数字",
                } and _is_qualitative_time_to_metric_threshold(result_regions, match):
                    continue
                excerpt = re.sub(r"\s+", " ", match.group(0)).strip()[:120]
                blockers.append(f"{label}: {excerpt}")
                break
        for sentence in re.finditer(r"[^.!?。！？]+(?:[.!?。！？]+|\Z)", own_method_sentences):
            sentence_text = sentence.group(0)
            match = _OWN_METHOD_FORECAST_MAGNITUDE_RE.search(sentence_text)
            comparative_match = _OWN_METHOD_COMPARATIVE_MAGNITUDE_RE.search(sentence_text)
            if (
                (match is None and comparative_match is None)
                or not _OWN_METHOD_OUTCOME_RE.search(sentence_text)
            ):
                continue
            effective_match = match or comparative_match
            assert effective_match is not None
            if _is_qualitative_time_to_metric_threshold(sentence_text, effective_match):
                continue
            excerpt = re.sub(r"\s+", " ", effective_match.group(0)).strip()[:120]
            blocker = f"own-method numeric forecast without evidence: {excerpt}"
            if blocker not in blockers:
                blockers.append(blocker)
            break

    return blockers


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        return _fail([f"invalid JSON input: {exc}"])
    if not isinstance(payload, dict):
        return _fail(["input must be a JSON object"])

    blockers = audit(payload)
    if blockers:
        return _fail(blockers)
    print("QUALITY_GATE: pass")
    print("BLOCKERS:")
    print("- none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
