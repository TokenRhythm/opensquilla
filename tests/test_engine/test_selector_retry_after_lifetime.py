from __future__ import annotations

import asyncio
import time

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
from opensquilla.provider.types import ChatConfig, DoneEvent, ErrorEvent, Message, TextDeltaEvent


def _done():
    return [TextDeltaEvent(text="done"), DoneEvent(stop_reason="stop")]


class _Provider:
    provider_name = "openai"
    retry_failed_call_safe = True

    def __init__(self, model, streams, calls):
        self.model = model
        self.streams = streams
        self.calls = calls
        self.count = 0

    async def chat(self, messages, tools=None, config=None):
        self.calls.append(self.model)
        events = self.streams[min(self.count, len(self.streams) - 1)]
        self.count += 1
        for event in events:
            yield event


def _setup(monkeypatch, *, with_other_authority=True):
    calls = []
    configs = {
        "a": ProviderConfig("openai", "a", api_key="dummy-a"),
        "a2": ProviderConfig(
            "openai", "a2", api_key="dummy-a", base_url="HTTPS://API.OPENAI.COM:443/v1/"
        ),
        "b": ProviderConfig("openai", "b", api_key="dummy-b", base_url="https://b.test/v1"),
        "c": ProviderConfig("openai", "c", api_key="dummy-c", base_url="https://c.test/v1"),
    }
    providers = {
        "a": _Provider(
            "a", [[ErrorEvent(message="rate limit", code="429", retry_after_s=60)]], calls
        ),
        "b": _Provider("b", [[ErrorEvent(message="unavailable", code="503")]], calls),
        "a2": _Provider("a2", [_done()], calls),
        "c": _Provider("c", [_done()], calls),
    }

    class Plugin:
        count = 0

        def failover_hook(self, failure):
            self.count += 1
            # Every returned chain excludes original primary a, as required.
            tail = [configs["a2"], *([configs["c"]] if with_other_authority else [])]
            return [configs["b"], *tail] if self.count == 1 else tail

    plugin = Plugin()
    monkeypatch.setattr(
        "opensquilla.provider.selector._build_provider", lambda config: providers[config.model]
    )
    selector = ModelSelector(
        SelectorConfig(primary=configs["a"], fallbacks=[configs["b"], configs["a2"], configs["c"]]),
        plugin=plugin,
    )
    wrapper = _SelectorFallbackProvider(selector.resolve(), selector)
    return wrapper, selector, providers, configs, plugin, calls


async def _run(wrapper, **overrides):
    config = {"timeout": 2, "max_provider_retries": 0}
    config.update(overrides)
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(retry_base_backoff_ms=0, retry_max_backoff_ms=0, **config),
    )
    return [event async for event in agent.run_turn("Complete the synthetic task.")]


async def _chat(wrapper):
    return [
        event async for event in wrapper.chat(
            [Message(role="user", content="Synthetic request")],
            config=ChatConfig(agent_managed_recovery=True),
        )
    ]


@pytest.mark.parametrize("with_other_authority", [False, True])
async def test_dynamic_hook_cannot_reintroduce_cooling_authority(monkeypatch, with_other_authority):
    wrapper, _, providers, configs, plugin, calls = _setup(
        monkeypatch, with_other_authority=with_other_authority
    )
    events = await _run(wrapper)

    assert calls == (["a", "b", "c"] if with_other_authority else ["a", "b"])
    assert providers["a2"].count == 0
    assert plugin.count == 2
    assert wrapper._retry_after_remaining(configs["a2"]) > 0
    assert any(event.kind == ("done" if with_other_authority else "error") for event in events)


async def test_expired_authority_can_rejoin_dynamic_hook(monkeypatch):
    wrapper, _, _, configs, _, calls = _setup(monkeypatch)
    await _run(wrapper)
    expires_after = time.monotonic() + 61

    # Advance only the synchronous selection operation, not asyncio's clock.
    with monkeypatch.context() as clock:
        clock.setattr("opensquilla.engine.runtime.time.monotonic", lambda: expires_after)
        assert wrapper.fallback_after_invalid_response("synthetic invalid response") is True
        assert wrapper._retry_after_remaining(configs["a2"]) == 0
    assert any(isinstance(event, DoneEvent) for event in await _chat(wrapper))
    assert calls == ["a", "b", "c", "a2"]


async def test_turn_clone_does_not_share_authority_cooldown_or_rewrite_plugin(monkeypatch):
    wrapper, selector, _, configs, plugin, calls = _setup(monkeypatch)
    await _run(wrapper)
    cloned = selector.clone()
    other_turn = _SelectorFallbackProvider(cloned.resolve(), cloned)

    assert other_turn._retry_after_remaining(configs["a2"]) == 0
    assert other_turn.fallback_after_invalid_response("synthetic invalid response") is True
    assert any(isinstance(event, DoneEvent) for event in await _chat(other_turn))
    assert calls == ["a", "b", "c", "a2"]
    assert wrapper._retry_after_remaining(configs["a2"]) > 0
    assert selector._plugin is cloned._plugin is plugin


async def test_main_entry_blocks_early_same_authority_before_usage_start(monkeypatch):
    wrapper, selector, providers, configs, _, calls = _setup(monkeypatch)
    await _chat(wrapper)
    original_deadlines = dict(wrapper._retry_after_until)
    selector.override_provider_config(configs["a2"], preserve_existing_tail=False)
    wrapper._provider = providers["a2"]

    def forbidden_accounting(*args, **kwargs):
        raise AssertionError("a held authority must not start a physical usage envelope")

    monkeypatch.setattr("opensquilla.engine.runtime.account_provider_stream", forbidden_accounting)
    events = await _chat(wrapper)
    assert calls == ["a"]
    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "429"
    assert 0 < events[0].retry_after_s <= 60
    assert wrapper._retry_after_until == original_deadlines


async def test_new_fallback_entry_blocks_legacy_selection_before_usage_start(monkeypatch):
    import opensquilla.engine.runtime as runtime

    wrapper, selector, providers, configs, plugin, calls = _setup(monkeypatch)
    await _chat(wrapper)
    original_deadlines = dict(wrapper._retry_after_until)
    selector.override_provider_config(configs["b"])
    wrapper._provider = providers["b"]
    plugin.count = 1
    # Exercise the existing legacy-selector seam, which cannot filter a
    # candidate atomically. The final dispatch guard must still prevent I/O.
    monkeypatch.setattr(selector, "next_fallback_after_failure_matching", None)
    started_models = []
    original_accounting = runtime.account_provider_stream

    def accounting(*args, **kwargs):
        started_models.append(kwargs["model"])
        return original_accounting(*args, **kwargs)

    monkeypatch.setattr(runtime, "account_provider_stream", accounting)
    events = await _chat(wrapper)
    assert calls == ["a", "b"]
    assert started_models == ["b"]
    assert providers["a2"].count == 0
    assert any(isinstance(event, ErrorEvent) and event.code == "429" for event in events)
    assert wrapper._retry_after_until == original_deadlines


async def test_real_agent_wait_releases_same_leg_at_retry_after(monkeypatch):
    wrapper, _, providers, _, _, calls = _setup(monkeypatch)
    providers["a"].streams = [
        [ErrorEvent(message="rate limit", code="429", retry_after_s=0.01)], _done()
    ]
    events = await _run(wrapper, max_provider_retries=1)
    assert calls == ["a", "a"]
    assert any(event.kind == "done" for event in events)


@pytest.mark.parametrize("timeout", [0, 2])
async def test_early_timer_wakeup_does_not_consume_retry_or_switch_provider(monkeypatch, timeout):
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    original_sleep = asyncio.sleep
    delays = []

    async def early_sleep(delay):
        delays.append(delay)
        now[0] += delay / 2 if len(delays) <= 2 else delay
        await original_sleep(0)

    monkeypatch.setattr(loop, "time", lambda: now[0])
    monkeypatch.setattr("opensquilla.engine.runtime.time.monotonic", lambda: now[0])
    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", early_sleep)
    wrapper, _, providers, _, _, calls = _setup(monkeypatch)
    providers["a"].streams = [
        [ErrorEvent(message="rate limit", code="429", retry_after_s=0.01)], _done()
    ]

    events = await _run(wrapper, timeout=timeout, max_provider_retries=1)

    assert delays == pytest.approx([0.01, 0.005, 0.0025])
    assert calls == ["a", "a"]
    assert [
        event.retry_attempt for event in events
        if event.kind == "provider_activity" and event.phase == "retrying"
    ] == [1]
    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" for event in events)


async def test_rate_limit_from_fallback_leg_records_its_authority(monkeypatch):
    wrapper, _, providers, configs, _, calls = _setup(monkeypatch)
    providers["a"].streams = [[ErrorEvent(message="unavailable", code="503")]]
    providers["b"].streams = [[ErrorEvent(message="rate limit", code="429", retry_after_s=60)]]
    events = await _chat(wrapper)
    assert calls == ["a", "b"]
    assert any(isinstance(event, ErrorEvent) and event.code == "429" for event in events)
    assert wrapper._retry_after_remaining(configs["b"]) > 0
    assert wrapper._retry_after_remaining(configs["a"]) == 0


@pytest.mark.parametrize(
    "error", [ErrorEvent(message="invalid api key", code="401", retry_after_s=60),
              ErrorEvent(message="insufficient_quota", code="429", retry_after_s=60)]
)
async def test_auth_and_billing_errors_do_not_install_retry_after_hold(monkeypatch, error):
    wrapper, _, providers, configs, _, _ = _setup(monkeypatch)
    providers["a"].streams = [[error]]
    await _chat(wrapper)
    assert wrapper._retry_after_remaining(configs["a2"]) == 0
