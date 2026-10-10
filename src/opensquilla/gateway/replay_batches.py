"""Bounded replay planning for snapshot recovery.

The recovery reader may observe more events while a snapshot is being read
than fit in one transport window.  Planning is kept pure so both the Gateway
and deterministic tests can prove contiguous waterline advancement without
touching the session store.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class _Sequenced(Protocol):
    stream_seq: int


REPLAY_BATCH_FRAMES = 8


class ReplayBatchError(ValueError):
    """The replay tail is not contiguous or cannot be bounded."""


def plan_replay_batches[T: _Sequenced](
    events: Sequence[T],
    *,
    start_seq: int,
    current_seq: int,
    batch_frames: int = REPLAY_BATCH_FRAMES,
) -> tuple[tuple[T, ...], ...]:
    """Split one contiguous tail into finite batches and preserve its waterline.

    The returned batches cover every sequence in ``(start_seq, current_seq]``
    exactly once.  An empty tail is represented by ``()``.  Callers may
    publish one batch at a time and advance the consumer cursor to the last
    sequence in that batch before planning the next one.
    """

    if type(start_seq) is not int or type(current_seq) is not int:
        raise ReplayBatchError("replay sequence numbers must be integers")
    if start_seq < 0 or current_seq < start_seq:
        raise ReplayBatchError("replay sequence range is invalid")
    if type(batch_frames) is not int or batch_frames <= 0:
        raise ReplayBatchError("replay batch size must be positive")
    if len(events) != current_seq - start_seq or any(
        getattr(event, "stream_seq", None) != start_seq + index
        for index, event in enumerate(events, 1)
    ):
        raise ReplayBatchError("replay tail is not contiguous")
    return tuple(
        tuple(events[offset : offset + batch_frames])
        for offset in range(0, len(events), batch_frames)
    )


__all__ = ["REPLAY_BATCH_FRAMES", "ReplayBatchError", "plan_replay_batches"]
