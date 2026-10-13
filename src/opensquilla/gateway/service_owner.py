"""Lifecycle fencing for optional Gateway services.

Optional integrations are deliberately outside the durable core readiness
boundary, but they still need one owner for start/stop races.  This small
adapter gives each integration a single-flight ``start_once`` operation and a
generation fence.  A completion from an older owner generation may finish for
cleanup purposes, but it can never publish readiness for the current owner.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, MutableMapping
from typing import Any

LifecycleCallback = Callable[..., Any]


def _invoke_callback(
    callback: LifecycleCallback | None,
    *,
    generation: int,
    config_revision: str | int,
) -> Any:
    """Invoke a lifecycle callback, passing fencing metadata when accepted."""
    if callback is None:
        return None
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(callback).parameters
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if accepts_kwargs or "generation" in parameters:
            kwargs["generation"] = generation
        if accepts_kwargs or "config_revision" in parameters:
            kwargs["config_revision"] = config_revision
    except (TypeError, ValueError):
        # Some extension callables do not expose a signature.  Preserve the
        # legacy zero-argument callback shape instead of guessing.
        kwargs = {}
    return callback(**kwargs)


class OptionalServiceOwner:
    """Own one optional service and fence late lifecycle completions.

    ``starter`` and the optional lifecycle callbacks may be synchronous or
    asynchronous.  Concurrent ``start_once`` calls share the active attempt;
    an explicit call after completion starts a new attempt and generation.
    ``quiesce`` invalidates the current generation
    and cancels an in-flight starter without waiting for an uncooperative
    external operation.  Its eventual completion is consumed and ignored.
    """

    def __init__(
        self,
        name: str,
        starter: LifecycleCallback,
        *,
        config_revision: str | int = 0,
        descriptor: MutableMapping[str, Any] | None = None,
        quiescer: LifecycleCallback | None = None,
        drainer: LifecycleCallback | None = None,
        closer: LifecycleCallback | None = None,
    ) -> None:
        if not name:
            raise ValueError("optional service owner name must not be empty")
        self.name = name
        self.starter = starter
        self.quiescer = quiescer
        self.drainer = drainer
        self.closer = closer
        self.config_revision = config_revision
        self.descriptor = descriptor
        self.owner_generation = 0
        self.state = "disabled"
        self._start_task: asyncio.Task[Any] | None = None
        self._closed = False
        self._quiesced = False
        self._publish()

    @property
    def start_task(self) -> asyncio.Task[Any] | None:
        return self._start_task

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.state,
            "state": self.state,
            "config_revision": self.config_revision,
            "owner_generation": self.owner_generation,
        }

    def _publish(self) -> None:
        if self.descriptor is None:
            return
        self.descriptor.update(self.snapshot())

    def _is_current(self, generation: int) -> bool:
        return (
            not self._closed
            and not self._quiesced
            and generation == self.owner_generation
        )

    async def _run_start(self, generation: int) -> Any:
        try:
            result = _invoke_callback(
                self.starter,
                generation=generation,
                config_revision=self.config_revision,
            )
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            if self._is_current(generation):
                self.state = "stopping"
                self._publish()
            raise
        except Exception:
            if self._is_current(generation):
                self.state = "degraded"
                self._publish()
            raise
        if not self._is_current(generation):
            # The operation can finish after cancellation (for example, an
            # SDK call running in a thread).  Its result is deliberately not
            # published into a newer owner generation.  Preserve cancellation
            # to the caller even when the external callback swallowed the
            # task cancellation and returned a late result.
            raise asyncio.CancelledError
        if isinstance(result, str) and result in {
            "disabled", "starting", "ready", "degraded", "stopping", "stopped"
        }:
            self.state = result
        else:
            self.state = "ready"
        self._publish()
        return result

    @staticmethod
    def _consume(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            # A fenced late completion is expected during shutdown.  Consume
            # it so the event loop does not report an unhandled task warning.
            return

    async def start_once(self) -> Any:
        """Share an active start, or begin a new attempt after completion."""
        if self._closed:
            raise RuntimeError(f"optional service {self.name!r} is closed")
        task = self._start_task
        if task is not None and not task.done():
            return await task
        if task is not None and task.done():
            # Surface a previous failure to its caller, but permit a later
            # explicit retry after a completed start attempt.
            self._start_task = None
        self._quiesced = False
        self.owner_generation += 1
        generation = self.owner_generation
        self.state = "starting"
        self._publish()
        task = asyncio.create_task(self._run_start(generation))
        self._start_task = task
        task.add_done_callback(self._consume)
        try:
            return await task
        finally:
            if self._start_task is task and task.done():
                self._start_task = None

    async def quiesce(self) -> None:
        """Fence new work and cancel startup without waiting indefinitely."""
        if self._closed:
            return
        self._quiesced = True
        self.owner_generation += 1
        self.state = "stopping"
        self._publish()
        task = self._start_task
        self._start_task = None
        if task is not None and not task.done():
            task.cancel()
        result = _invoke_callback(
            self.quiescer,
            generation=self.owner_generation,
            config_revision=self.config_revision,
        )
        if inspect.isawaitable(result):
            await result

    async def drain(self) -> None:
        """Drain accepted optional work through the owner callback."""
        result = _invoke_callback(
            self.drainer,
            generation=self.owner_generation,
            config_revision=self.config_revision,
        )
        if inspect.isawaitable(result):
            await result

    async def close(self) -> None:
        """Quiesce, drain, and close while keeping the generation fenced."""
        if self._closed:
            return
        await self.quiesce()
        await self.drain()
        self._closed = True
        self.owner_generation += 1
        result = _invoke_callback(
            self.closer,
            generation=self.owner_generation,
            config_revision=self.config_revision,
        )
        if inspect.isawaitable(result):
            await result
        self.state = "stopped"
        self._publish()


__all__ = ["OptionalServiceOwner"]
