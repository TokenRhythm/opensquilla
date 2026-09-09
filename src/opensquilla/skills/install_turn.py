"""Trusted, turn-local installation receipts and conservative request scope."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from opensquilla.skills.install_source import resolve_install_source

_INSTALL = re.compile(r"\binstall(?:ation)?\b|安装|装上|装一下", re.I)
_SEARCH = re.compile(r"\b(?:find|search|browse|locate)\b|搜索|查找|找.*技能", re.I)
_PREFIX = re.compile(
    r"^(?:(?:please|can you|could you|help me|I want you to)\s+|请|帮我|麻烦你|帮忙)*"
    r"(?:install\b|安装|装上|装一下)\s*", re.I,
)
_URL = re.compile(r"(?:https?://|github\.com/)[^\s<>\[\](),，、；;。]+", re.I)
_MIXED = re.compile(
    r"\b(?:then|use|using|run|write|create|verify|check|explain|test|update|remove)\b"
    r"|然后|并且|使用|写|运行|验证|检查|解释|测试|更新|删除", re.I,
)
_SCAFFOLD = re.compile(
    r"\b(?:the|these|this|following|skills?|and|from|for me)\b"
    r"|以下|这些|这个|这两个|这几个|技能|和|以及|并", re.I,
)


def _key(identifier: str, source: str | None = None) -> tuple[str, str]:
    return resolve_install_source(identifier, source), identifier.strip().rstrip("/")


def install_targets(request: str) -> tuple[tuple[str, str], ...]:
    """Accept only an install imperative followed by an exact list of targets."""
    text = request.strip()
    if "```" in text or re.search(r"(?m)^\s*>|<[^>]+>", text):
        return ()
    prefix = _PREFIX.match(text)
    if prefix is None:
        return ()
    text = text[prefix.end():]
    # Markdown labels are presentation; the link target is the requested reference.
    text = re.sub(r"\[[^\]\n]+\]\(([^)\n]+)\)", r"\1", text)
    urls = _URL.findall(text)
    remaining = _URL.sub(" ", text)
    if _MIXED.search(remaining):
        return ()
    remaining = _SCAFFOLD.sub(" ", remaining)
    tokens = [part for part in re.split(r"[\s,，、;；:：.!！?？。`*]+", remaining) if part]
    tokens = [part for part in tokens if part != "-"]
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./@-]*", part) for part in tokens):
        return ()
    if any(resolve_install_source(url) != "github" for url in urls):
        return ()
    targets = [_key(value) for value in [*urls, *tokens]]
    return tuple(dict.fromkeys(targets)) if 0 < len(targets) <= 100 else ()


@dataclass
class SkillInstallTurn:
    request: str
    receipts: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    targets: tuple[tuple[str, str], ...] = field(init=False)
    finalization_allowed: bool = True

    def __post_init__(self) -> None:
        self.targets = install_targets(self.request)

    def surface_tools(self) -> set[str]:
        result: set[str] = set()
        if _INSTALL.search(self.request):
            result.add("skill_install_community")
        if _SEARCH.search(self.request) and re.search(r"skills?|技能", self.request, re.I):
            result.add("skill_search_community")
        return result

    def previous(self, identifier: str, source: str) -> dict[str, Any] | None:
        result = self.receipts.get(_key(identifier, source))
        if result is not None:
            return copy.deepcopy(result)
        # A directory-choice response grants no authority to choose a directory.
        # A new user turn starts with a fresh receipt set and can choose explicitly.
        for receipt in self.receipts.values():
            for diagnostic in receipt.get("diagnostics", []):
                if diagnostic.get("code") != "SOURCE_TREE_AMBIGUOUS":
                    continue
                for candidate in diagnostic.get("details", {}).get("candidates", []):
                    if candidate.get("identifier") == identifier:
                        return copy.deepcopy(receipt)
        return None

    def record(self, identifier: str, source: str, result: dict[str, Any]) -> None:
        self.receipts[_key(identifier, source)] = copy.deepcopy(result)

    @property
    def complete(self) -> bool:
        return bool(self.finalization_allowed and self.targets) and all(
            self.receipts.get(target, {}).get("success") is True for target in self.targets
        )

    def final_text(self) -> str:
        chinese = bool(re.search(r"[\u3400-\u9fff]", self.request))
        lines = []
        for target in self.targets:
            receipt = self.receipts[target]
            name = str(receipt.get("name") or target[1])
            usable = receipt.get("instruction_usable") is True
            lifecycle = receipt.get("lifecycle") or {}
            if chinese:
                status = "已安装，下一回合可用" if usable else "已安装，当前尚不可用"
            else:
                status = "Installed; available next turn" if usable else "Installed; not yet usable"
            if not usable:
                states = [
                    str(lifecycle.get(key, ""))
                    for key in ("load_state", "selection_state", "readiness_state")
                ]
                states = [value for value in states if value]
                if states:
                    status += " (" + ", ".join(states) + ")"
            lines.append(f"{name}: {status}.")
        return "\n".join(lines)
