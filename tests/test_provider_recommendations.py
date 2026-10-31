"""Offline contracts for scoped, non-destructive recommendation updates."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.provider import recommendations as rec


@pytest.fixture(autouse=True)
def isolated_instances(monkeypatch):
    monkeypatch.setattr(rec, "_INSTANCES", {})
    yield
    for instance in rec._INSTANCES.values():
        instance.stop.set()
        if instance.thread is not None:
            instance.thread.join(timeout=2)
            assert not instance.thread.is_alive()


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(
        state_dir=str(tmp_path / "instance-a"),
        llm=SimpleNamespace(provider="tokenrhythm"),
        privacy=SimpleNamespace(disable_network_observability=False),
    )


@pytest.fixture
def allow_refresh(monkeypatch):
    # Explicitly opt these isolated tests into the mocked network boundary.
    for name in (
        "PYTEST_CURRENT_TEST", "CI", "GITHUB_ACTIONS", "OPENSQUILLA_TESTING",
        "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
        "OPENSQUILLA_TELEMETRY_DISABLED", "OPENSQUILLA_UPDATE_CHECK_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def _documents(suffix="a"):
    return {
        "router": {
            provider: {
                **{
                    role: {"model": f"route-{role}-{suffix}", "thinking_level": "high"}
                    for role in ("c0", "c1", "c2", "c3")
                },
                "image_model": {"model": "ignored-image", "thinking_level": "invalid"},
            }
            for provider in ("openrouter", "tokenrhythm")
        },
        "ensemble": {
            provider: {
                "proposers": [
                    {"model": f"proposer-{i}-{suffix}", "thinking_level": "low"}
                    for i in range(4)
                ],
                "aggregator": {"model": f"aggregator-{suffix}", "thinking_level": None},
            }
            for provider in ("openrouter", "tokenrhythm")
        },
    }


def _mock_fetch(monkeypatch, documents):
    # pytest reinstates this variable at the call phase, after fixtures ran.
    # Clear it only after installing an explicit, offline fetch replacement.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    calls = []

    def fetch(url):
        calls.append(url)
        kind = next(key for key, value in rec.RECOMMENDATION_URLS.items() if value == url)
        value = documents[kind]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    monkeypatch.setattr(rec, "_fetch_document", fetch)
    return calls


def test_bundled_reads_do_not_fetch_write_or_mutate_user_config(config, monkeypatch):
    def unexpected_fetch(_url):
        pytest.fail("a recommendation read must remain offline")

    monkeypatch.setattr(rec, "_fetch_document", unexpected_fetch)
    before = copy.deepcopy(vars(config))
    for provider in ("openrouter", "tokenrhythm"):
        router = rec.get_recommendation(config, provider, "router")
        ensemble = rec.get_recommendation(config, provider, "ensemble")
        assert router["source"] == ensemble["source"] == "bundled"
        assert [row["role"] for row in router["rows"]] == ["c0", "c1", "c2", "c3"]
        assert [row["role"] for row in ensemble["rows"]] == [
            "proposer_1", "proposer_2", "proposer_3", "proposer_4", "aggregator",
        ]
        router["rows"][0]["model"] = "caller-edit"
        assert rec.get_recommendation(config, provider, "router")["rows"][0]["model"] != (
            "caller-edit"
        )
    assert vars(config) == before
    assert not Path(config.state_dir).exists()


@pytest.mark.parametrize("provider,kind", [("openai", "router"), ("tokenrhythm", "direct")])
def test_out_of_scope_pair_is_rejected(config, provider, kind):
    with pytest.raises(ValueError):
        rec.get_recommendation(config, provider, kind)
    assert not rec._INSTANCES


def test_only_allowed_fields_enter_cache_and_revisions_ignore_metadata(
    config, monkeypatch, allow_refresh,
):
    documents = _documents()
    documents["router"]["openai"] = documents["router"]["openrouter"]
    documents["router"]["openrouter"]["c0"].update(
        base_url="https://unused.invalid", api_key="public-dummy", description="unused",
    )
    calls = _mock_fetch(monkeypatch, documents)
    before = copy.deepcopy(vars(config))
    assert rec.refresh_recommendations(config=config)
    result = rec.get_recommendation(config, "openrouter", "router")
    assert result["source"] == "remote"
    assert set(result) == {"providerId", "kind", "revision", "source", "rows"}
    assert result["rows"][0] == {
        "role": "c0", "provider": "openrouter", "model": "route-c0-a", "thinkingLevel": "high",
    }
    documents["router"]["openrouter"]["image_model"]["model"] = "other-image"
    documents["router"]["openrouter"]["c0"]["description"] = "different metadata"
    assert not rec.refresh_recommendations(config=config, force=True)
    assert rec.get_recommendation(config, "openrouter", "router")["revision"] == result["revision"]
    documents["router"]["openrouter"]["c0"]["thinking_level"] = "low"
    assert rec.refresh_recommendations(config=config, force=True)
    assert rec.get_recommendation(config, "openrouter", "router")["revision"] != result["revision"]
    assert set(calls) == set(rec.RECOMMENDATION_URLS.values())
    assert vars(config) == before
    assert "public-dummy" not in (Path(config.state_dir) / rec.STATE_FILENAME).read_text()


def test_attempt_ttl_survives_restart_and_expires_after_24h(config, monkeypatch, allow_refresh):
    now = [10_000.0]
    monkeypatch.setattr(rec.time, "time", lambda: now[0])
    calls = _mock_fetch(monkeypatch, _documents())
    assert rec.refresh_recommendations(config=config)
    rec._INSTANCES.clear()
    now[0] += rec.TTL_SECONDS - 1
    assert not rec.refresh_recommendations(config=config)
    assert len(calls) == 2
    now[0] += 1
    assert not rec.refresh_recommendations(config=config)
    assert len(calls) == 4


def test_background_worker_waits_for_ttl_and_refreshes_again(config, monkeypatch, allow_refresh):
    now = [10_000.0]
    monkeypatch.setattr(rec.time, "time", lambda: now[0])
    calls = _mock_fetch(monkeypatch, _documents())
    delays = []

    class SimulatedClockEvent:
        def is_set(self):
            return len(calls) >= 4

        def wait(self, seconds):
            delays.append(seconds)
            now[0] += seconds
            return False

        def set(self):
            pass

    instance = rec._instance(config)
    instance.config = config
    instance.stop = SimulatedClockEvent()
    rec._background_worker(instance)
    assert len(calls) == 4
    assert delays == [0.0, 1.0, rec.TTL_SECONDS - 1.0, 1.0]


def test_same_bundled_and_remote_rows_have_same_revision(config, monkeypatch, allow_refresh):
    bundled = rec.get_recommendation(config, "openrouter", "router")
    documents = _documents()
    documents["router"]["openrouter"] = {
        row["role"]: {"model": row["model"], "thinking_level": row["thinkingLevel"]}
        for row in bundled["rows"]
    }
    _mock_fetch(monkeypatch, documents)
    rec.refresh_recommendations(config=config)
    remote = rec.get_recommendation(config, "openrouter", "router")
    assert remote["source"] == "remote"
    assert remote["revision"] == bundled["revision"]


def test_failed_attempts_keep_last_good_and_are_throttled(config, monkeypatch, allow_refresh):
    _mock_fetch(monkeypatch, _documents())
    rec.refresh_recommendations(config=config)
    before = rec.get_recommendation(config, "tokenrhythm", "ensemble")
    calls = _mock_fetch(monkeypatch, {
        "router": httpx.ReadTimeout("unavailable"), "ensemble": ValueError("invalid JSON"),
    })
    assert not rec.refresh_recommendations(config=config, force=True)
    rec._INSTANCES.clear()
    assert rec.get_recommendation(config, "tokenrhythm", "ensemble") == before
    assert not rec.refresh_recommendations(config=config)
    assert len(calls) == 2


def test_invalid_provider_unit_does_not_block_other_updates(config, monkeypatch, allow_refresh):
    _mock_fetch(monkeypatch, _documents())
    rec.refresh_recommendations(config=config)
    previous = rec.get_recommendation(config, "tokenrhythm", "router")
    documents = _documents("b")
    documents["router"]["tokenrhythm"]["c2"]["provider"] = "openai"
    documents["ensemble"]["openrouter"]["proposers"] = []
    _mock_fetch(monkeypatch, documents)
    assert rec.refresh_recommendations(config=config, force=True)
    assert rec.get_recommendation(config, "tokenrhythm", "router") == previous
    assert rec.get_recommendation(config, "openrouter", "router")["rows"][0]["model"] == (
        "route-c0-b"
    )
    assert rec.get_recommendation(config, "tokenrhythm", "ensemble")["rows"][0]["model"] == (
        "proposer-0-b"
    )
    assert rec.get_recommendation(config, "openrouter", "ensemble")["rows"][0]["model"] == (
        "proposer-0-a"
    )


@pytest.mark.parametrize("bad_row", [
    {"model": ""}, {"model": "bad\nmodel"}, {"model": 12},
    {"model": "model", "thinking_level": "future-unknown"},
    {"model": "model", "thinking_level": []},
])
def test_invalid_model_or_thinking_retains_bundled_unit(
    config, monkeypatch, allow_refresh, bad_row,
):
    documents = _documents()
    documents["router"]["openrouter"]["c0"] = bad_row
    _mock_fetch(monkeypatch, documents)
    rec.refresh_recommendations(config=config)
    assert rec.get_recommendation(config, "openrouter", "router")["source"] == "bundled"
    assert rec.get_recommendation(config, "tokenrhythm", "router")["source"] == "remote"


@pytest.mark.parametrize("count", [0, 1, 7])
def test_unsupported_ensemble_sizes_preserve_last_good(
    config, monkeypatch, allow_refresh, count,
):
    documents = _documents()
    _mock_fetch(monkeypatch, documents)
    rec.refresh_recommendations(config=config)
    previous = rec.get_recommendation(config, "openrouter", "ensemble")
    documents["ensemble"]["openrouter"]["proposers"] = [{"model": "synthetic"}] * count
    assert not rec.refresh_recommendations(config=config, force=True)
    assert rec.get_recommendation(config, "openrouter", "ensemble") == previous


def test_duplicate_proposers_retain_last_good_but_aggregator_may_repeat(
    config, monkeypatch, allow_refresh,
):
    documents = _documents()
    documents["ensemble"]["openrouter"]["aggregator"]["model"] = "proposer-0-a"
    _mock_fetch(monkeypatch, documents)
    rec.refresh_recommendations(config=config)
    previous = rec.get_recommendation(config, "openrouter", "ensemble")
    assert previous["source"] == "remote"
    assert previous["rows"][-1]["model"] == previous["rows"][0]["model"]
    documents["ensemble"]["openrouter"]["proposers"][1]["model"] = " proposer-0-a "
    assert not rec.refresh_recommendations(config=config, force=True)
    assert rec.get_recommendation(config, "openrouter", "ensemble") == previous


def test_duplicate_proposers_in_persisted_cache_are_rejected(
    config, monkeypatch, allow_refresh,
):
    _mock_fetch(monkeypatch, _documents())
    rec.refresh_recommendations(config=config)
    path = Path(config.state_dir) / rec.STATE_FILENAME
    state = json.loads(path.read_text())
    rows = state["entries"]["openrouter:ensemble"]["rows"]
    rows[1]["model"] = rows[0]["model"]
    # Even a correctly hashed document from an older writer must pass today's
    # semantic checks before it can seed a first-run configuration.
    state["entries"]["openrouter:ensemble"] = rec._envelope(
        "openrouter", "ensemble", rows, source="remote",
    )
    path.write_text(json.dumps(state))
    rec._INSTANCES.clear()
    assert rec.get_recommendation(config, "openrouter", "ensemble")["source"] == "bundled"
    assert rec.get_recommendation(config, "tokenrhythm", "ensemble")["source"] == "remote"


def test_unrelated_provider_does_not_start_background_fetch(config, monkeypatch, allow_refresh):
    calls = _mock_fetch(monkeypatch, _documents())
    config.llm.provider = "openai"
    # A dormant default ensemble profile is not actual use of OpenRouter.
    config.llm_ensemble = SimpleNamespace(enabled=False, selection_mode="static_openrouter_b5")
    assert rec.start_background_recommendation_refresh(config=config) is None
    assert calls == []
    assert not rec._INSTANCES


@pytest.mark.parametrize("use", ["profile", "named_profile", "ensemble"])
def test_configured_secondary_provider_enables_background_refresh(
    config, monkeypatch, allow_refresh, use,
):
    _mock_fetch(monkeypatch, _documents())
    config.llm.provider = "openai"
    if use == "ensemble":
        config.llm_ensemble = SimpleNamespace(enabled=True, selection_mode="static_tokenrhythm_b5")
    else:
        key = "openrouter:work" if use == "named_profile" else "tokenrhythm"
        config.llm_profiles = {key: SimpleNamespace()}
    thread = rec.start_background_recommendation_refresh(config=config)
    assert thread is not None


def test_instance_isolation_return_copy_and_notification_persistence(
    config, monkeypatch, allow_refresh, tmp_path,
):
    other = SimpleNamespace(state_dir=str(tmp_path / "instance-b"))
    _mock_fetch(monkeypatch, _documents())
    rec.refresh_recommendations(config=config)
    result = rec.get_recommendation(config, "openrouter", "router")
    revision = result["revision"]
    result["rows"][0]["model"] = "caller-edit"
    assert rec.get_recommendation(config, "openrouter", "router")["rows"][0]["model"] == (
        "route-c0-a"
    )
    rec.mark_recommendation_notified(config, "openrouter", "router", revision)
    rec._INSTANCES.clear()
    assert rec.recommendation_notified(config, "openrouter", "router", revision)
    assert not rec.recommendation_notified(config, "openrouter", "ensemble", revision)
    assert not rec.recommendation_notified(other, "openrouter", "router", revision)
    assert rec.get_recommendation(other, "openrouter", "router")["source"] == "bundled"
    assert not Path(other.state_dir).exists()


def test_corrupt_cached_unit_does_not_poison_other_units(config, monkeypatch, allow_refresh):
    _mock_fetch(monkeypatch, _documents())
    rec.refresh_recommendations(config=config)
    path = Path(config.state_dir) / rec.STATE_FILENAME
    state = json.loads(path.read_text())
    state["entries"]["openrouter:router"]["rows"][0]["model"] = "edited-without-revision"
    path.write_text(json.dumps(state))
    rec._INSTANCES.clear()
    assert rec.get_recommendation(config, "openrouter", "router")["source"] == "bundled"
    assert rec.get_recommendation(config, "tokenrhythm", "router")["source"] == "remote"
    path.write_text("not json")
    rec._INSTANCES.clear()
    assert rec.get_recommendation(config, "tokenrhythm", "router")["source"] == "bundled"


@pytest.mark.parametrize("flag", [
    "PYTEST_CURRENT_TEST", "CI", "GITHUB_ACTIONS", "OPENSQUILLA_TESTING",
    "OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY",
    "OPENSQUILLA_TELEMETRY_DISABLED", "OPENSQUILLA_UPDATE_CHECK_DISABLED",
])
def test_privacy_and_automated_environments_disable_even_forced_refresh(
    config, monkeypatch, allow_refresh, flag,
):
    calls = _mock_fetch(monkeypatch, _documents())
    monkeypatch.setenv(flag, "1")
    assert not rec.refresh_recommendations(config=config, force=True)
    assert rec.start_background_recommendation_refresh(config=config) is None
    assert calls == []
    assert not Path(config.state_dir).exists()


def test_privacy_opt_out_keeps_cached_recommendation_readable(config, monkeypatch, allow_refresh):
    calls = _mock_fetch(monkeypatch, _documents())
    rec.refresh_recommendations(config=config)
    config.privacy.disable_network_observability = True
    assert not rec.refresh_recommendations(config=config, force=True)
    assert rec.start_background_recommendation_refresh(config=config) is None
    assert rec.get_recommendation(config, "tokenrhythm", "router")["source"] == "remote"
    assert len(calls) == 2


def test_background_singleflight_does_not_block_reads(config, monkeypatch, allow_refresh):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    documents = _documents()

    def fetch(url):
        calls.append(url)
        if url == rec.RECOMMENDATION_URLS["router"]:
            entered.set()
            assert release.wait(2)
            return documents["router"]
        completed.set()
        return documents["ensemble"]

    monkeypatch.setattr(rec, "_fetch_document", fetch)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    thread = rec.start_background_recommendation_refresh(config=config)
    try:
        assert thread is not None and entered.wait(2)
        assert rec.start_background_recommendation_refresh(config=config) is thread
        assert rec.get_recommendation(config, "openrouter", "router")["source"] == "bundled"
        assert not rec.refresh_recommendations(config=config, force=True)
        release.set()
        assert completed.wait(2)
    finally:
        release.set()
        rec._instance(config).stop.set()
        if thread is not None:
            thread.join(timeout=2)
    assert len(calls) == 2
    assert rec.get_recommendation(config, "openrouter", "router")["source"] == "remote"


def test_atomic_write_failure_preserves_previous_disk_cache(config, monkeypatch, allow_refresh):
    _mock_fetch(monkeypatch, _documents())
    rec.refresh_recommendations(config=config)
    path = Path(config.state_dir) / rec.STATE_FILENAME
    previous = path.read_bytes()
    _mock_fetch(monkeypatch, _documents("b"))

    def fail_replace(*_args):
        raise PermissionError("synthetic file sharing violation")

    monkeypatch.setattr(rec.os, "replace", fail_replace)
    assert rec.refresh_recommendations(config=config, force=True)
    assert path.read_bytes() == previous
    assert rec.get_recommendation(config, "openrouter", "router")["rows"][0]["model"] == (
        "route-c0-b"
    )
    assert list(path.parent.glob("*.tmp")) == []


def test_failed_acknowledgement_is_retryable(config, monkeypatch):
    revision = rec.get_recommendation(config, "openrouter", "router")["revision"]
    save = rec._save
    monkeypatch.setattr(rec, "_save", lambda _instance: False)
    with pytest.raises(OSError, match="acknowledgement"):
        rec.mark_recommendation_notified(config, "openrouter", "router", revision)
    assert not rec.recommendation_notified(config, "openrouter", "router", revision)
    monkeypatch.setattr(rec, "_save", save)
    rec.mark_recommendation_notified(config, "openrouter", "router", revision)
    assert rec.recommendation_notified(config, "openrouter", "router", revision)


def test_fetch_has_timeout_no_redirects_and_bounded_body(monkeypatch):
    original_client = httpx.Client
    requests = []
    response = [httpx.Response(200, json={"ok": True})]

    def handler(request):
        requests.append(request)
        return response[0]

    def client(**kwargs):
        assert kwargs["timeout"] == 3.0
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(rec.httpx, "Client", client)
    url = rec.RECOMMENDATION_URLS["router"]
    assert rec._fetch_document(url) == {"ok": True}
    assert "authorization" not in requests[0].headers
    response[0] = httpx.Response(302, headers={"Location": "https://unused.invalid/"})
    with pytest.raises(httpx.HTTPStatusError):
        rec._fetch_document(url)
    assert len(requests) == 2
    response[0] = httpx.Response(200, content=b" " * (rec.MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(ValueError, match="too large"):
        rec._fetch_document(url)
    # A missing/incorrect content-length must not bypass the streamed cap.
    response[0] = httpx.Response(
        200, content=b" " * (rec.MAX_PAYLOAD_BYTES + 1), headers={"Content-Length": "0"},
    )
    with pytest.raises(ValueError, match="too large"):
        rec._fetch_document(url)
