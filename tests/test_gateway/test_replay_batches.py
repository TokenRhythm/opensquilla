from dataclasses import dataclass

import pytest

from opensquilla.gateway.replay_batches import (
    REPLAY_BATCH_FRAMES,
    ReplayBatchError,
    plan_replay_batches,
)


@dataclass(frozen=True)
class Event:
    stream_seq: int


@pytest.mark.parametrize("count", [8, 9, 17])
def test_replay_batches_advance_contiguous_waterline(count: int) -> None:
    events = [Event(index) for index in range(1, count + 1)]
    batches = plan_replay_batches(events, start_seq=0, current_seq=count)

    assert [event.stream_seq for batch in batches for event in batch] == list(
        range(1, count + 1)
    )
    assert all(len(batch) <= REPLAY_BATCH_FRAMES for batch in batches)
    assert [batch[-1].stream_seq for batch in batches] == [
        min(index, count)
        for index in range(
            REPLAY_BATCH_FRAMES, count + REPLAY_BATCH_FRAMES, REPLAY_BATCH_FRAMES
        )
    ]


def test_replay_batches_reject_gap() -> None:
    with pytest.raises(ReplayBatchError):
        plan_replay_batches([Event(1), Event(3)], start_seq=0, current_seq=3)


@pytest.mark.parametrize("sequences", [[1, 1], [2, 1], [2, 3]])
def test_replay_batches_reject_wrong_sequence_with_matching_count(
    sequences: list[int],
) -> None:
    with pytest.raises(ReplayBatchError, match="not contiguous"):
        plan_replay_batches(
            [Event(sequence) for sequence in sequences], start_seq=0, current_seq=2
        )


def test_replay_batches_reject_huge_missing_tail_without_allocating_it() -> None:
    with pytest.raises(ReplayBatchError, match="not contiguous"):
        plan_replay_batches([], start_seq=0, current_seq=10**30)


def test_replay_batches_accept_empty_tail_at_current_waterline() -> None:
    assert plan_replay_batches([], start_seq=5, current_seq=5) == ()
