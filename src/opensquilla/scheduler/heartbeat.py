"""Heartbeat runner — event coalescing, priority bands, active-hours mask.

The heartbeat runner turns a burst of in-process events into a small number of
emitted ticks. It is deliberately structured as a poll-driven coalescer rather
than a background task so callers (timer loop, unit tests) can drive it on
their own cadence.

Three knobs shape the emitted-tick stream:

- ``coalesce_window_ms``: events received within this window of the first
  buffered event for a given priority band are rolled into a single tick. The
  AC pins ``5 events in 250ms -> 1 tick`` against this default.
- ``priority_bands``: a dict mapping band name to *minimum seconds between
  ticks*. A band that last emitted within that cooldown is suppressed on the
  current poll even if events are buffered (they stay buffered for the next
  eligible poll).
- ``active_hours``: an optional ``(start_hour_inclusive, end_hour_exclusive)``
  window in 24-hour local time. When supplied, ``poll`` at a moment outside
  the window drains no events — the buffer stays intact so ticks resume when
  the window re-opens.

The persistence layer is intentionally thin: :class:`HeartbeatStore` writes
each emitted tick into ``heartbeat_ticks`` (via yoyo migration ``V003``). The
table carries ``schema_version`` per S-MIGRATE discipline so future shape
changes go through a migration rather than an ALTER in product code.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from opensquilla.compat import aiosqlite

__all__ = [
    "HEARTBEAT_TEMPLATE_PATH",
    "HeartbeatConfig",
    "HeartbeatConfigWatcher",
    "HeartbeatEvent",
    "HeartbeatLoopOverrides",
    "HeartbeatRunner",
    "HeartbeatStore",
    "HeartbeatTick",
    "is_heartbeat_content_effectively_empty",
    "parse_heartbeat_md",
    "parse_loop_overrides",
]

HEARTBEAT_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "HEARTBEAT.md"


_DEFAULT_PRIORITY_BANDS: dict[str, float] = {
    "high": 1.0,
    "medium": 5.0,
    "low": 30.0,
}


@dataclass
class HeartbeatEvent:
    """Single event ingested into the runner's buffer."""

    kind: str = ""
    priority: str = "medium"
    emitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    payload: dict = field(default_factory=dict)


@dataclass
class HeartbeatTick:
    """Coalesced tick emitted for a priority band."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    emitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    priority_band: str = "medium"
    event_count: int = 0
    schema_version: int = 1


@dataclass
class HeartbeatConfig:
    """Runtime knobs for :class:`HeartbeatRunner`."""

    coalesce_window_ms: int = 250
    priority_bands: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_PRIORITY_BANDS))
    active_hours: tuple[int, int] | None = None

    def band_cooldown(self, band: str) -> float:
        return float(self.priority_bands.get(band, self.priority_bands.get("medium", 5.0)))

    def is_within_active_hours(self, moment: datetime) -> bool:
        if self.active_hours is None:
            return True
        start, end = self.active_hours
        hour = moment.hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end


@dataclass
class HeartbeatLoopOverrides:
    """Loop-side overrides parsed from HEARTBEAT.md frontmatter.

    Each field is ``None`` when absent from the frontmatter; the loop falls
    back to its bootstrap (gateway) config for unset values. Lets HEARTBEAT.md
    be the single live-edit surface for both Runner and Loop without merging
    the two config classes.
    """

    enabled: bool | None = None
    interval_ms: int | None = None
    target: str | None = None
    prompt: str | None = None
    ack_max_chars: int | None = None
    light_context: bool | None = None
    active_hours: tuple[int, int] | None = None  # also gates the loop tick

    def is_empty(self) -> bool:
        return all(
            getattr(self, f) is None
            for f in (
                "enabled",
                "interval_ms",
                "target",
                "prompt",
                "ack_max_chars",
                "light_context",
                "active_hours",
            )
        )


class HeartbeatRunner:
    """Poll-driven coalescing runner.

    The runner is single-threaded by design: callers must not share a runner
    across event loops without an external lock. The buffer is keyed by
    priority band so each band coalesces independently and observes its own
    cooldown.
    """

    def __init__(self, config: HeartbeatConfig | None = None) -> None:
        self._config = config or HeartbeatConfig()
        self._buffers: dict[str, list[HeartbeatEvent]] = defaultdict(list)
        self._last_tick: dict[str, datetime] = {}

    @property
    def config(self) -> HeartbeatConfig:
        return self._config

    def replace_config(self, config: HeartbeatConfig) -> None:
        """Swap the active config — called by HeartbeatConfigWatcher on live edits."""
        self._config = config

    def ingest(self, event: HeartbeatEvent) -> None:
        self._buffers[event.priority].append(event)

    def pending_counts(self) -> dict[str, int]:
        return {band: len(events) for band, events in self._buffers.items() if events}

    def poll(self, now: datetime | None = None) -> list[HeartbeatTick]:
        moment = now or datetime.now(UTC)
        if not self._config.is_within_active_hours(moment):
            return []

        emitted: list[HeartbeatTick] = []
        window_ms = self._config.coalesce_window_ms

        for band, events in list(self._buffers.items()):
            if not events:
                continue

            first = events[0].emitted_at
            if (moment - first).total_seconds() * 1000 < window_ms:
                continue

            last = self._last_tick.get(band)
            if last is not None:
                cooldown = self._config.band_cooldown(band)
                if (moment - last).total_seconds() < cooldown:
                    continue

            tick = HeartbeatTick(
                emitted_at=moment,
                priority_band=band,
                event_count=len(events),
            )
            emitted.append(tick)
            self._last_tick[band] = moment
            self._buffers[band] = []

        return emitted


_CREATE_TICKS_TABLE = """
CREATE TABLE IF NOT EXISTS heartbeat_ticks (
    id TEXT PRIMARY KEY,
    emitted_at TEXT NOT NULL,
    priority_band TEXT NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL DEFAULT 1,
    payload TEXT NOT NULL DEFAULT '{}'
)
"""


class HeartbeatStore:
    """Async SQLite store for emitted :class:`HeartbeatTick` records."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(_CREATE_TICKS_TABLE)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> HeartbeatStore:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("HeartbeatStore not opened")
        return self._conn

    async def save_tick(self, tick: HeartbeatTick, payload: dict | None = None) -> None:
        await self._db().execute(
            """
            INSERT INTO heartbeat_ticks
                (id, emitted_at, priority_band, event_count, schema_version, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                tick.id,
                tick.emitted_at.isoformat(),
                tick.priority_band,
                tick.event_count,
                tick.schema_version,
                json.dumps(payload or {}),
            ),
        )
        await self._db().commit()

    async def list_ticks(self, limit: int = 50) -> list[HeartbeatTick]:
        async with self._db().execute(
            "SELECT * FROM heartbeat_ticks ORDER BY emitted_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            HeartbeatTick(
                id=row["id"],
                emitted_at=datetime.fromisoformat(row["emitted_at"]),
                priority_band=row["priority_band"],
                event_count=row["event_count"],
                schema_version=row["schema_version"],
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# HEARTBEAT.md parser + live-reload watcher (S9)
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _extract_frontmatter(text: str) -> str | None:
    match = _FRONTMATTER_RE.match(text)
    return match.group(1) if match else None


def _coerce_active_hours(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _coerce_priority_bands(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for band, cooldown in value.items():
        try:
            out[str(band)] = float(cooldown)
        except (TypeError, ValueError):
            continue
    return out or None


def parse_heartbeat_md(source: str | Path) -> HeartbeatConfig:
    """Parse a HEARTBEAT.md frontmatter into a HeartbeatConfig.

    Pass a :class:`~pathlib.Path` to read from disk, or a raw markdown string
    to parse in memory. A missing file, absent frontmatter, or malformed YAML
    all fall back to default :class:`HeartbeatConfig` so a typo during editing
    does not brick cadence.
    """
    if isinstance(source, Path):
        if not source.is_file():
            return HeartbeatConfig()
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = str(source)

    frontmatter = _extract_frontmatter(text)
    if frontmatter is None:
        return HeartbeatConfig()

    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return HeartbeatConfig()
    if not isinstance(data, dict):
        return HeartbeatConfig()

    config = HeartbeatConfig()
    coalesce = data.get("coalesce_window_ms")
    if isinstance(coalesce, (int, float)) and coalesce >= 0:
        config.coalesce_window_ms = int(coalesce)

    bands = _coerce_priority_bands(data.get("priority_bands"))
    if bands is not None:
        config.priority_bands = bands

    if "active_hours" in data:
        config.active_hours = _coerce_active_hours(data["active_hours"])

    return config


def _load_frontmatter_data(source: str | Path) -> dict | None:
    """Shared frontmatter extraction. Returns parsed dict or None on any failure."""
    if isinstance(source, Path):
        if not source.is_file():
            return None
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = str(source)
    frontmatter = _extract_frontmatter(text)
    if frontmatter is None:
        return None
    try:
        data = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _heartbeat_body_text(source: str | Path) -> str | None:
    if isinstance(source, Path):
        if not source.is_file():
            return None
        text = source.read_text(encoding="utf-8", errors="replace")
    else:
        text = str(source)
    return _FRONTMATTER_RE.sub("", text, count=1)


def is_heartbeat_content_effectively_empty(source: str | Path) -> bool:
    """Return True for blank/header-only/empty-list heartbeat scaffolds."""
    text = _heartbeat_body_text(source)
    if text is None:
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"#{1,6}\s+.*", line):
            continue
        if re.fullmatch(r"[-*+]\s*(?:\[(?:\s|x|X)?\])?\s*", line):
            continue
        return False
    return True


def parse_loop_overrides(source: str | Path) -> HeartbeatLoopOverrides:
    """Parse HEARTBEAT.md frontmatter for HeartbeatLoop overrides.

    Recognized keys (all optional):
    ``enabled``, ``interval_ms``, ``target``, ``prompt``, ``ack_max_chars``,
    ``active_hours``. Any malformed value is silently dropped (fail-open):
    the resulting field stays ``None`` and the loop keeps its bootstrap
    config — a typo in the markdown must not brick cadence.
    """
    overrides = HeartbeatLoopOverrides()
    data = _load_frontmatter_data(source)
    if data is None:
        return overrides

    enabled = data.get("enabled")
    if isinstance(enabled, bool):
        overrides.enabled = enabled

    # ``bool`` is a subclass of ``int`` in Python, so ``isinstance(True, int)``
    # is True. Reject bools explicitly — otherwise ``interval_ms: true``
    # parses as 1, giving a 1 ms heartbeat. Same logic for ``ack_max_chars``.
    interval = data.get("interval_ms")
    if (
        not isinstance(interval, bool)
        and isinstance(interval, (int, float))
        and interval >= 1
    ):
        overrides.interval_ms = int(interval)

    target = data.get("target")
    if isinstance(target, str) and target.strip():
        overrides.target = target.strip()

    prompt = data.get("prompt")
    if isinstance(prompt, str):
        overrides.prompt = prompt

    ack = data.get("ack_max_chars")
    if not isinstance(ack, bool) and isinstance(ack, (int, float)) and ack >= 0:
        overrides.ack_max_chars = int(ack)

    light_context = data.get("light_context", data.get("lightContext"))
    if isinstance(light_context, bool):
        overrides.light_context = light_context

    if "active_hours" in data:
        overrides.active_hours = _coerce_active_hours(data["active_hours"])

    return overrides


class HeartbeatConfigWatcher:
    """Poll HEARTBEAT.md for mtime changes and swap runner config live.

    Tolerant by construction: a missing file reverts to :class:`HeartbeatConfig`
    defaults; a malformed frontmatter leaves the previous config intact (via
    :func:`parse_heartbeat_md`'s default-on-error contract) so a typo during
    editing does not brick cadence.
    """

    def __init__(
        self,
        runner: HeartbeatRunner,
        heartbeat_md_path: str | Path,
        poll_interval: float = 2.0,
        *,
        loop_listener: Callable[[HeartbeatLoopOverrides], None] | None = None,
    ) -> None:
        self._runner = runner
        self._path = Path(heartbeat_md_path).expanduser()
        self._poll_interval = poll_interval
        self._loop_listener = loop_listener
        self._last_mtime: float | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def path(self) -> Path:
        return self._path

    def reload_now(self) -> HeartbeatConfig:
        """Parse the file (or fall back to defaults) and apply to the runner.

        If a ``loop_listener`` was registered, also parse Loop overrides and
        push them; listener exceptions are swallowed to keep cadence alive.
        """
        config = parse_heartbeat_md(self._path)
        self._runner.replace_config(config)
        if self._loop_listener is not None:
            try:
                overrides = parse_loop_overrides(self._path)
                self._loop_listener(overrides)
            except Exception:  # noqa: BLE001 — fail-open by contract
                pass
        try:
            self._last_mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._last_mtime = None
        return config

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.reload_now()
        # Use create_background_task so a stubbed asyncio.create_task in tests
        # closes the spawned coroutine (asyncio_utils contract).
        from opensquilla.asyncio_utils import create_background_task

        self._task = create_background_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            if not self._running:
                break
            current = self._path.stat().st_mtime if self._path.is_file() else None
            if current != self._last_mtime:
                self.reload_now()
