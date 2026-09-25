"""Synthetic idle replay benchmark; run in each checkout with its own dependencies.

Prints cache accounting separately from whole-process RSS. No provider, Gateway
profile, user transcript, or real attachment is read. This is not a startup or
native sleep/wake benchmark.
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import tracemalloc

from opensquilla.gateway.session_streams import SessionStreamRegistry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=500)
    parser.add_argument("--text-bytes", type=int, default=256 * 1024)
    args = parser.parse_args()
    if args.sessions < 1 or args.text_bytes < 32:
        parser.error("sessions must be positive and text-bytes at least 32")

    registry = SessionStreamRegistry()
    tracemalloc.start()
    initial_cpu = time.process_time()
    started = time.perf_counter()
    # The benchmark models the terminal storage transaction succeeding. On
    # older baselines there is no cache admission notification to invoke.
    persisted = getattr(registry, "mark_terminal_persisted", lambda *_: None)
    for index in range(args.sessions):
        key, task = f"synthetic-{index}", f"synthetic-task-{index}"
        text = f"{index:032x}" + "x" * (args.text_bytes - 32)
        registry.record(key, "session.event.text_delta", {"task_id": task, "text": text})
        registry.record(key, "session.event.done", {"task_id": task})
        registry.take_terminal_activity_snapshot(key, task, turn_id=task)
        if hasattr(registry, "mark_terminal_persisted"):
            persisted(key, task, reconstructible=True)
    gc.collect()
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    try:
        import resource

        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            max_rss *= 1024
    except ImportError:
        max_rss = None  # Windows needs an external process-tree sampler.
    usage = getattr(registry, "replay_cache_usage", lambda: None)()
    print(json.dumps({
        "sessions": args.sessions,
        "text_bytes_per_session": args.text_bytes,
        "wall_ms": round((time.perf_counter() - started) * 1000, 2),
        "process_cpu_ms": round((time.process_time() - initial_cpu) * 1000, 2),
        "process_peak_rss_bytes": max_rss,
        "python_retained_bytes": retained,
        "python_peak_bytes": peak,
        "child_processes_started": 0,
        "cache_accounting": usage,
        "oldest_replay_complete": registry.replay("synthetic-0", 0).replay_complete,
        "newest_replay_complete": registry.replay(
            f"synthetic-{args.sessions - 1}", 0,
        ).replay_complete,
    }, indent=2))


if __name__ == "__main__":
    main()
