from __future__ import annotations

from opensquilla.gateway.token_store import AuthFailureLimiter


def test_failure_limiter_allows_five_then_applies_bounded_backoff() -> None:
    now = 1_000.0
    limiter = AuthFailureLimiter(clock=lambda: now)

    delays = [
        limiter.record_failure("192.168.1.7", "laptop")
        for _ in range(12)
    ]

    assert delays[:5] == [0.0] * 5
    assert delays[5:] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_failure_limiter_tracks_peer_and_public_id_independently() -> None:
    limiter = AuthFailureLimiter(clock=lambda: 1_000.0)

    for index in range(5):
        assert limiter.record_failure(f"192.168.1.{index}", "shared") == 0.0

    assert limiter.record_failure("192.168.1.99", "shared") == 1.0
    assert limiter.record_failure("192.168.1.99", "different") == 0.0


def test_failure_window_expires() -> None:
    now = [1_000.0]
    limiter = AuthFailureLimiter(clock=lambda: now[0])
    for _ in range(6):
        limiter.record_failure("192.168.1.7", "laptop")

    now[0] += 61.0

    assert limiter.record_failure("192.168.1.7", "laptop") == 0.0
