"""Turn-level capture into private agent-state Markdown files."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opensquilla.engine.steps.inject_time_prefix import TIME_PREFIX_RE

_SESSION_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TRUNCATION_SUFFIX = "\n... (truncated)"


def _session_slug(session_key: str) -> str:
    normalized = _SESSION_SLUG_RE.sub("-", session_key.lower()).strip("-")
    return normalized[:80] or "session"


def _strip_time_prefix(text: str) -> str:
    return TIME_PREFIX_RE.sub("", text, count=1).strip()


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = max(0, max_chars - len(_TRUNCATION_SUFFIX))
    return text[:head].rstrip() + _TRUNCATION_SUFFIX


class TurnCaptureService:
    """Persist turn deltas into private state files.

    Raw turns are audit/debug state, not curated memory. They deliberately do
    not index into the ordinary memory store.
    """

    def __init__(
        self,
        *,
        workspace_dir: str | Path,
        turns_dir: str | Path | None = None,
        memory_config: Any | None = None,
    ) -> None:
        self._workspace_dir = Path(workspace_dir).expanduser().resolve()
        self._turns_dir = (
            Path(turns_dir).expanduser().resolve()
            if turns_dir is not None
            else (self._workspace_dir / ".opensquilla" / "turns").resolve()
        )
        self._turns_parent = self._turns_dir.parent
        self._memory_config = memory_config

    def _enabled(self) -> bool:
        if self._memory_config is None:
            return True
        if not bool(getattr(self._memory_config, "auto_capture_enabled", True)):
            return False
        return getattr(self._memory_config, "capture_mode", "turn_pair") != "off"

    def _capture_max_chars(self) -> int:
        if self._memory_config is None:
            return 2000
        value = int(getattr(self._memory_config, "capture_max_chars", 2000) or 0)
        return max(0, value)

    def _capture_user(self) -> bool:
        if self._memory_config is None:
            return True
        return bool(getattr(self._memory_config, "capture_user", True))

    def _capture_assistant(self) -> bool:
        if self._memory_config is None:
            return False
        return bool(getattr(self._memory_config, "capture_assistant", False))

    def _turn_roll_max_chars(self) -> int:
        if self._memory_config is None:
            return 50_000
        value = int(getattr(self._memory_config, "capture_roll_max_chars", 50_000) or 0)
        return max(0, value)

    def _turn_rel_path(self, session_key: str, captured_at: datetime) -> str:
        date_part = captured_at.strftime("%Y-%m-%d")
        return f"{self._turns_dir.name}/{_session_slug(session_key)}/{date_part}.md"

    def _turn_part_rel_path(
        self,
        session_key: str,
        captured_at: datetime,
        part: int,
    ) -> str:
        date_part = captured_at.strftime("%Y-%m-%d")
        return f"{self._turns_dir.name}/{_session_slug(session_key)}/{date_part}-part{part:03d}.md"

    def _file_header(self, session_key: str) -> str:
        return "\n".join(
            [
                "# Turn Capture",
                "",
                "- source_kind: turn_capture",
                f"- session_key: {session_key}",
                "- schema: turn-capture-v1",
            ]
        )

    @staticmethod
    def _source_lines(source: dict[str, Any] | None) -> list[str]:
        if not source:
            return []
        lines: list[str] = []
        for key in (
            "caller_kind",
            "channel_kind",
            "channel_id",
            "sender_id",
            "source_kind",
            "source_name",
            "run_kind",
            "input_provenance_kind",
        ):
            value = source.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        provenance = source.get("input_provenance")
        if (
            "input_provenance_kind" not in source
            and isinstance(provenance, dict)
            and provenance.get("kind")
        ):
            lines.append(f"- input_provenance_kind: {provenance['kind']}")
        return lines

    def _render_entry(
        self,
        *,
        session_id: str,
        captured_at: datetime,
        user_text: str,
        assistant_text: str,
        source: dict[str, Any] | None,
    ) -> str:
        lines = [
            f"## Turn {captured_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"- session_id: {session_id}",
            f"- captured_at: {captured_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        ]
        lines.extend(self._source_lines(source))
        if user_text:
            lines.extend(["", "### User", user_text])
        if assistant_text:
            lines.extend(["", "### Assistant", assistant_text])
        return "\n".join(lines)

    def _select_turn_target(
        self,
        *,
        session_key: str,
        captured_at: datetime,
        entry: str,
    ) -> tuple[str, Path, str | None, str]:
        roll_max_chars = self._turn_roll_max_chars()

        for part in range(1, 1000):
            rel_path = (
                self._turn_rel_path(session_key, captured_at)
                if part == 1
                else self._turn_part_rel_path(session_key, captured_at, part)
            )
            abs_path = (self._turns_parent / rel_path).resolve()
            try:
                abs_path.relative_to(self._turns_parent)
            except ValueError as exc:  # pragma: no cover - defensive
                raise RuntimeError("turn capture path escaped state root") from exc

            previous_content = abs_path.read_text(encoding="utf-8") if abs_path.exists() else None
            existing = (
                previous_content
                if previous_content is not None
                else self._file_header(session_key)
            )
            new_content = existing.rstrip() + "\n\n" + entry + "\n"
            if (
                roll_max_chars <= 0
                or len(new_content) <= roll_max_chars
                or previous_content is None
            ):
                return rel_path, abs_path, previous_content, new_content

        raise RuntimeError("turn capture part limit exceeded")

    async def capture_turn(
        self,
        *,
        session_key: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        source: dict[str, Any] | None = None,
        captured_at: datetime | None = None,
        no_memory_capture: bool = False,
    ) -> str | None:
        if no_memory_capture:
            return None
        if not self._enabled():
            return None

        captured_at = captured_at or datetime.now(tz=UTC)
        max_chars = self._capture_max_chars()
        cleaned_user = (
            _truncate(_strip_time_prefix(user_text.strip()), max_chars)
            if self._capture_user()
            else ""
        )
        cleaned_assistant = (
            _truncate(assistant_text.strip(), max_chars)
            if self._capture_assistant()
            else ""
        )
        if not cleaned_user and not cleaned_assistant:
            return None

        entry = self._render_entry(
            session_id=session_id,
            captured_at=captured_at,
            user_text=cleaned_user,
            assistant_text=cleaned_assistant,
            source=source,
        )
        rel_path, abs_path, previous_content, new_content = self._select_turn_target(
            session_key=session_key,
            captured_at=captured_at,
            entry=entry,
        )
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        had_existing = previous_content is not None
        try:
            abs_path.write_text(new_content, encoding="utf-8")
        except Exception:
            if had_existing and previous_content is not None:
                abs_path.write_text(previous_content, encoding="utf-8")
            elif abs_path.exists():
                abs_path.unlink()
            raise
        return rel_path
