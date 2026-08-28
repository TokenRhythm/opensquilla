"""Mutable state carried by an interactive chat session."""

from __future__ import annotations

from dataclasses import dataclass, field

from opensquilla.cli.chat.turn import UsageCounter

__all__ = [
    "ChatSessionState",
    "ChatTranscript",
    "PromptState",
    "ReplTranscript",
    "TranscriptTurn",
    "_model_alias",
    "messages_to_markdown",
]


def _model_alias(full: str | None) -> str:
    """Return a short display alias for a model identifier."""
    if not full:
        return "…"
    seg = full.rsplit("/", 1)[-1]
    if len(seg) > 28:
        return seg[:12] + "…" + seg[-12:]
    return seg


@dataclass
class PromptState:
    model: str | None = None
    elevated: str | None = None

    @property
    def label(self) -> str:
        mode = self.elevated or "normal"
        return f"[{_model_alias(self.model)} {mode}] you ▸ "


@dataclass
class TranscriptTurn:
    role: str
    content: str


@dataclass
class ChatTranscript:
    turns: list[TranscriptTurn] = field(default_factory=list)

    def add(self, role: str, content: str) -> None:
        if content:
            self.turns.append(TranscriptTurn(role=role, content=content))

    def clear(self) -> None:
        self.turns.clear()

    def to_markdown(self) -> str:
        chunks: list[str] = []
        for turn in self.turns:
            heading = "You" if turn.role == "user" else "Assistant"
            chunks.append(f"## {heading}\n\n{turn.content.strip()}\n")
        return "\n".join(chunks)


ReplTranscript = ChatTranscript


def messages_to_markdown(messages: list[dict]) -> str:
    chunks: list[str] = []
    for message in messages:
        role = str(message.get("role") or "message")
        if role == "user":
            heading = "You"
        elif role == "assistant":
            heading = "Assistant"
        else:
            heading = role.title()
        text = str(message.get("text") or message.get("content") or "").strip()
        if text:
            chunks.append(f"## {heading}\n\n{text}\n")
    return "\n".join(chunks)


@dataclass
class ChatSessionState:
    session_key: str
    model: str | None = None
    elevated: str | None = None
    transcript: ChatTranscript = field(default_factory=ChatTranscript)
    usage: UsageCounter = field(default_factory=UsageCounter)

    def prompt_state(self) -> PromptState:
        return PromptState(model=self.model, elevated=self.elevated)
