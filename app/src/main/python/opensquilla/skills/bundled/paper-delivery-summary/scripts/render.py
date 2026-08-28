"""Render a paper delivery note from deterministic workflow outputs only."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

_COMPILE_NUMBER_FIELDS = ("PDF_PAGES", "PDF_TARGET_PAGES")
_SUMMARY_FIELDS = (
    "total_cite_keys",
    "strong",
    "ok",
    "weak",
    "invalid",
    "unused",
)
_SUMMARY_RE = re.compile(r"^\s*SUMMARY\s*:\s*([^\r\n]+?)\s*$", re.MULTILINE)
_CONTRACT_LANGUAGE_RE = re.compile(
    r"^\s*LANGUAGE\s*:\s*([a-z][a-z0-9_-]*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class DeliveryDataError(ValueError):
    """Raised when upstream deterministic evidence is missing or ambiguous."""


def _single_marker(text: str, name: str) -> str:
    matches = re.findall(
        rf"^\s*{re.escape(name)}\s*:\s*([^\r\n]+?)\s*$",
        text,
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise DeliveryDataError(f"compile_pdf must contain exactly one {name} marker")
    value = matches[0].strip()
    if not value:
        raise DeliveryDataError(f"compile_pdf marker {name} must not be empty")
    return value


def _non_negative_integer(value: str, *, field: str) -> int:
    if not re.fullmatch(r"0|[1-9]\d*", value):
        raise DeliveryDataError(f"{field} must be a non-negative integer")
    return int(value)


def _compile_fields(text: str) -> dict[str, str | int]:
    path_value = _single_marker(text, "PDF_PATH")
    if any(ord(character) < 32 for character in path_value):
        raise DeliveryDataError("PDF_PATH contains a control character")
    if not (Path(path_value).is_absolute() or PureWindowsPath(path_value).is_absolute()):
        raise DeliveryDataError("PDF_PATH must be absolute")

    parsed: dict[str, str | int] = {"path": path_value}
    for marker in _COMPILE_NUMBER_FIELDS:
        parsed[marker] = _non_negative_integer(_single_marker(text, marker), field=marker)
    pages = int(parsed["PDF_PAGES"])
    target = int(parsed["PDF_TARGET_PAGES"])
    if pages < 1:
        raise DeliveryDataError("PDF_PAGES must be at least 1")
    if not 1 <= target <= 50:
        raise DeliveryDataError("PDF_TARGET_PAGES must be between 1 and 50")
    if pages < target:
        raise DeliveryDataError("PDF_PAGES is below PDF_TARGET_PAGES")
    return parsed


def _citation_summary(text: str) -> dict[str, int]:
    matches = _SUMMARY_RE.findall(text)
    if len(matches) != 1:
        raise DeliveryDataError(
            "citation_map must contain exactly one machine-readable SUMMARY line"
        )

    fields: dict[str, int] = {}
    segments = [segment.strip() for segment in matches[0].split(",")]
    if not segments or any(not segment for segment in segments):
        raise DeliveryDataError("citation_map SUMMARY contains an empty field")
    for segment in segments:
        match = re.fullmatch(r"([a-z_]+)\s*=\s*(0|[1-9]\d*)", segment, re.IGNORECASE)
        if not match:
            raise DeliveryDataError("citation_map SUMMARY contains a malformed field")
        name = match.group(1).lower()
        if name not in _SUMMARY_FIELDS:
            raise DeliveryDataError(f"citation_map SUMMARY contains unknown field {name}")
        if name in fields:
            raise DeliveryDataError(f"citation_map SUMMARY repeats field {name}")
        fields[name] = int(match.group(2))

    missing = [name for name in _SUMMARY_FIELDS if name not in fields]
    if missing:
        raise DeliveryDataError(
            "citation_map SUMMARY is missing " + ", ".join(missing)
        )
    classified = fields["strong"] + fields["ok"] + fields["weak"] + fields["invalid"]
    if classified != fields["total_cite_keys"]:
        raise DeliveryDataError(
            "citation_map SUMMARY classifications do not equal total_cite_keys"
        )
    return fields


def _instruction_language(instruction: str) -> str | None:
    lowered = instruction.casefold()
    candidates: set[str] = set()
    if "simplified chinese" in lowered or "简体中文" in instruction:
        candidates.add("zh")
    if "english only" in lowered or "in english" in lowered:
        candidates.add("en")
    if len(candidates) > 1:
        raise DeliveryDataError("language_instruction is ambiguous")
    return next(iter(candidates), None)


def _language(contract: str, instruction: str) -> str:
    matches = _CONTRACT_LANGUAGE_RE.findall(contract)
    if len(matches) > 1:
        raise DeliveryDataError("paper_contract must not repeat the LANGUAGE marker")
    aliases = {"zh-cn": "zh", "zh_hans": "zh", "en-us": "en", "en-gb": "en"}
    contract_language = matches[0].lower() if matches else ""
    contract_language = aliases.get(contract_language, contract_language)

    # The paper contract is produced after structured clarification and is the
    # authoritative record of a user's language choice. The runtime language
    # instruction may still reflect the original slash-command text, so it
    # must never override or block a later confirmed zh/en selection.
    if contract_language in {"zh", "en"}:
        return contract_language

    instruction_language = _instruction_language(instruction)
    if instruction_language:
        return instruction_language
    if contract_language:
        return contract_language
    raise DeliveryDataError(
        "delivery language is missing from paper_contract and language_instruction"
    )


def _render_zh(compiled: dict[str, str | int], citations: dict[str, int]) -> str:
    lines = [
        "📄 论文已生成",
        "",
        f"- PDF: {compiled['path']}",
        f"- 页数: {compiled['PDF_PAGES']}（目标至少 {compiled['PDF_TARGET_PAGES']} 页）",
        (
            "- 引用: "
            f"正文引用键 {citations['total_cite_keys']}；"
            f"强来源 {citations['strong']}；一般来源 {citations['ok']}；"
            f"弱来源 {citations['weak']}；无效 {citations['invalid']}；"
            f"未使用条目 {citations['unused']}"
        ),
    ]
    warnings: list[str] = []
    if citations["weak"]:
        warnings.append(f"{citations['weak']} 个正文引用使用弱来源")
    if citations["invalid"]:
        warnings.append(f"{citations['invalid']} 个正文引用键未在参考文献库中找到")
    if citations["unused"]:
        warnings.append(f"参考文献库中有 {citations['unused']} 条未在正文引用")
    lines.append(f"- 警告: {'；'.join(warnings) if warnings else '无'}")
    return "\n".join(lines)


def _render_en(compiled: dict[str, str | int], citations: dict[str, int]) -> str:
    lines = [
        "📄 Paper compiled",
        "",
        f"- PDF: {compiled['path']}",
        f"- Pages: {compiled['PDF_PAGES']} (target: at least {compiled['PDF_TARGET_PAGES']})",
        (
            "- Citations: "
            f"cited keys {citations['total_cite_keys']}; "
            f"strong {citations['strong']}; acceptable {citations['ok']}; "
            f"weak {citations['weak']}; invalid {citations['invalid']}; "
            f"unused entries {citations['unused']}"
        ),
    ]
    warnings: list[str] = []
    if citations["weak"]:
        warnings.append(f"{citations['weak']} cited keys use weak sources")
    if citations["invalid"]:
        warnings.append(
            f"{citations['invalid']} cited keys are missing from the bibliography"
        )
    if citations["unused"]:
        warnings.append(f"{citations['unused']} bibliography entries are not cited")
    lines.append(f"- Warnings: {'; '.join(warnings) if warnings else 'none'}")
    return "\n".join(lines)


def _render_neutral(compiled: dict[str, str | int], citations: dict[str, int]) -> str:
    """Keep unsupported language codes deterministic without asserting English prose."""

    return "\n".join(
        [
            "📄",
            f"- PDF: {compiled['path']}",
            f"- 📑: {compiled['PDF_PAGES']} / ≥ {compiled['PDF_TARGET_PAGES']}",
            (
                "- 🔗: "
                f"Σ={citations['total_cite_keys']}; "
                f"✓={citations['strong']}; ○={citations['ok']}; "
                f"⚠={citations['weak']}; ✗={citations['invalid']}; "
                f"∅={citations['unused']}"
            ),
        ]
    )


def render(payload: dict[str, Any]) -> str:
    contract = str(payload.get("paper_contract") or "")
    instruction = str(payload.get("language_instruction") or "")
    compiled = _compile_fields(str(payload.get("compile_pdf") or ""))
    citations = _citation_summary(str(payload.get("citation_map") or ""))
    language = _language(contract, instruction)
    if language == "zh":
        return _render_zh(compiled, citations)
    if language == "en":
        return _render_en(compiled, citations)
    return _render_neutral(compiled, citations)


def _failure(message: str) -> int:
    rendered = f"DELIVERY_SUMMARY: blocked\nERROR: {message}"
    print(rendered)
    print(rendered, file=sys.stderr)
    return 2


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        return _failure(f"invalid JSON input: {exc}")
    if not isinstance(payload, dict):
        return _failure("input must be a JSON object")
    try:
        rendered = render(payload)
    except DeliveryDataError as exc:
        return _failure(str(exc))
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
