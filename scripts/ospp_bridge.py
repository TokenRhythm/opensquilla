"""Dedicated event-loop thread bridge for the optional Rust turn kernel.

A direct bridge from Rust to a Python async generator deadlocks:
advancing the generator depends on the event loop, while a synchronous
Rust call holds the main thread's loop. This module instead drives
`provider.chat` on a dedicated thread running its own asyncio loop,
pumping events through a thread-safe queue.Queue. Rust consumes with
q.get() (blocks with the GIL released), so the dedicated loop thread
keeps advancing the generator.

Usage:
    from scripts.ospp_bridge import get_loop
    loop = get_loop()
    q = loop.start_chat(provider, messages)
    while True:
        ev = q.get()
        if ev is None: break
        ...consume...
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
from typing import Any

_singleton: BridgeLoop | None = None
_singleton_lock = threading.Lock()


class BridgeLoop:
    """专用 asyncio loop 线程，用于驱动 Python async generator 流。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="ospp-bridge-loop"
        )
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start_chat(self, provider: Any, messages: list[dict]) -> queue.Queue:
        """在独立 loop 上跑 provider.chat，事件泵进线程安全 queue。

        返回 queue，事件逐个入队，流结束后放入 None 哨兵。
        """
        q: queue.Queue = queue.Queue()

        async def _pump() -> None:
            try:
                async for ev in provider.chat(messages):
                    q.put(ev)
            finally:
                q.put(None)

        asyncio.run_coroutine_threadsafe(_pump(), self.loop)
        return q

    def start_coro(self, coro: Any) -> queue.Queue:
        """在独立 loop 上跑任意 coroutine，yield 的事件泵进 queue。"""
        q: queue.Queue = queue.Queue()

        async def _pump() -> None:
            try:
                async for ev in coro:
                    q.put(ev)
            finally:
                q.put(None)

        asyncio.run_coroutine_threadsafe(_pump(), self.loop)
        return q

    def submit_coro(self, coro: Any) -> concurrent.futures.Future:
        """在独立 loop 上跑 coroutine，返回 concurrent.futures.Future。

        Rust 侧并行等待多个 future（result() 阻塞时释放 GIL，独立 loop
        线程可推进 coroutine，不死锁）。用于 5c 工具并行调度。
        """
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


def get_loop() -> BridgeLoop:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = BridgeLoop()
        return _singleton
