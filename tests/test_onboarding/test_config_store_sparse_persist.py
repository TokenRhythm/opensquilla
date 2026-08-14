"""Regression tests for sparse, diff-based config persistence.

Covers the onboarding persistence audit findings:

* env-derived values (secrets and overrides) must never be baked into the
  TOML by an unrelated save;
* explicit mutations must always be written;
* pre-existing TOML values and extra raw keys must survive a save;
* a small config must stay small after an unrelated save;
* saves must write through symlinks, fsync before the atomic rename, and
  merge (not clobber) non-conflicting concurrent on-disk edits.
"""

from __future__ import annotations

import os
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.onboarding.mutations import upsert_llm_provider


def _write_small_config(target) -> None:
    target.write_text(
        "\n".join(
            [
                "[llm]",
                'provider = "openrouter"',
                'model = "deepseek/deepseek-v4-flash"',
                "",
            ]
        )
    )


# ---------------------------------------------------------------------------
# A1: env-derived values must not be baked into the file
# ---------------------------------------------------------------------------


def test_persist_does_not_bake_env_auth_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_AUTH_TOKEN", "tok-synthetic-123")
    target = tmp_path / "config.toml"
    _write_small_config(target)

    cfg = load_config(target)
    assert cfg.auth.token == "tok-synthetic-123"  # env reached the model
    cfg.port = 18795  # unrelated explicit change

    persist_config(cfg, path=target)

    text = target.read_text()
    assert "tok-synthetic-123" not in text
    data = tomllib.loads(text)
    assert "auth" not in data
    assert data["port"] == 18795


def test_persist_missing_file_does_not_bake_env_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_AUTH_TOKEN", "tok-synthetic-456")
    target = tmp_path / "config.toml"

    cfg = load_config(target)  # first run: no config on disk yet
    cfg.llm.model = "deepseek/deepseek-v4-flash"  # differs from the default
    persist_config(cfg, path=target)

    text = target.read_text()
    assert "tok-synthetic-456" not in text
    data = tomllib.loads(text)
    assert "auth" not in data
    assert data["llm"]["model"] == "deepseek/deepseek-v4-flash"


def test_persist_fresh_model_does_not_bake_env_secret(tmp_path, monkeypatch):
    """Even a config never routed through load_config must not leak env."""
    monkeypatch.setenv("OPENSQUILLA_AUTH_TOKEN", "tok-synthetic-789")
    target = tmp_path / "config.toml"

    cfg = GatewayConfig()
    cfg.llm.model = "deepseek/deepseek-v4-flash"  # differs from the default
    persist_config(cfg, path=target)

    text = target.read_text()
    assert "tok-synthetic-789" not in text
    data = tomllib.loads(text)
    assert "auth" not in data
    assert data["llm"]["model"] == "deepseek/deepseek-v4-flash"


def test_persist_does_not_freeze_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_MEMORY_FLUSH_ENABLED", "true")
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    cfg = load_config(target)
    assert cfg.memory.flush_enabled is True
    cfg.port = 18795
    persist_config(cfg, path=target)

    assert "flush_enabled" not in target.read_text()

    # Removing the env override must restore the built-in default: the save
    # above must not have frozen the env value into the file.
    monkeypatch.delenv("OPENSQUILLA_MEMORY_FLUSH_ENABLED")
    assert load_config(target).memory.flush_enabled is False


def test_persist_writes_explicit_mutation(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    cfg = load_config(target)
    cfg.memory.flush_enabled = True
    persist_config(cfg, path=target)

    data = tomllib.loads(target.read_text())
    assert data["memory"]["flush_enabled"] is True
    assert load_config(target).memory.flush_enabled is True


def test_repersist_same_object_writes_revert(tmp_path):
    """A second save of the same object diffs against what was just written."""
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    cfg = load_config(target)
    cfg.memory.flush_enabled = True
    persist_config(cfg, path=target)
    assert tomllib.loads(target.read_text())["memory"]["flush_enabled"] is True

    cfg.memory.flush_enabled = False
    persist_config(cfg, path=target)
    data = tomllib.loads(target.read_text())
    assert data.get("memory", {}).get("flush_enabled") is False


def test_persist_preserves_existing_toml_values(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        "\n".join(
            [
                "[llm]",
                'provider = "openrouter"',
                'model = "deepseek/deepseek-v4-flash"',
                "",
                "[memory]",
                "flush_enabled = true",
                "",
            ]
        )
    )

    cfg = load_config(target)
    cfg.port = 18795
    persist_config(cfg, path=target)

    data = tomllib.loads(target.read_text())
    assert data["llm"]["provider"] == "openrouter"
    assert data["memory"]["flush_enabled"] is True
    assert data["port"] == 18795


def test_persist_preserves_extra_raw_keys(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        "\n".join(
            [
                "[llm]",
                'provider = "openrouter"',
                'model = "deepseek/deepseek-v4-flash"',
                "",
                "[channels.demo_extra]",
                'note = "keep-me"',
                "",
            ]
        )
    )

    cfg = load_config(target)
    cfg.port = 18795
    persist_config(cfg, path=target)

    data = tomllib.loads(target.read_text())
    assert data["channels"]["demo_extra"]["note"] == "keep-me"
    assert data["port"] == 18795


# ---------------------------------------------------------------------------
# A5: no full-default dump on an unrelated save
# ---------------------------------------------------------------------------


def test_persist_keeps_small_config_small(tmp_path):
    target = tmp_path / "config.toml"
    _write_small_config(target)

    cfg = load_config(target)
    cfg.port = 18795
    persist_config(cfg, path=target)

    text = target.read_text()
    data = tomllib.loads(text)
    assert set(data) <= {"llm", "port", "config_version"}
    assert len(text.splitlines()) < 12


# ---------------------------------------------------------------------------
# A2: writes go through symlinks instead of replacing them
# ---------------------------------------------------------------------------


def test_persist_updates_symlink_target_in_place(tmp_path):
    real = tmp_path / "real-config.toml"
    real.write_text("port = 18791\n")
    link = tmp_path / "config.toml"
    link.symlink_to(real)

    cfg = load_config(link)
    cfg.port = 18795
    persist_config(cfg, path=link)

    assert link.is_symlink()
    assert tomllib.loads(real.read_text())["port"] == 18795
    assert tomllib.loads(link.read_text())["port"] == 18795


# ---------------------------------------------------------------------------
# A3: fsync the temp file before the atomic rename
# ---------------------------------------------------------------------------


def test_persist_fsyncs_tempfile_before_replace(tmp_path, monkeypatch):
    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def tracking_fsync(fd):
        calls.append("fsync")
        return real_fsync(fd)

    def tracking_replace(src, dst):
        calls.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(os, "replace", tracking_replace)

    target = tmp_path / "config.toml"
    persist_config(GatewayConfig(), path=target)

    assert "fsync" in calls
    assert calls.index("fsync") < calls.index("replace")


def test_replace_is_commit_point_with_no_target_chmod_afterward(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")
    cfg = load_config(target)
    cfg.port = 18795
    real_chmod = os.chmod

    def reject_target_chmod(path, mode, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            raise PermissionError("target chmod must not run after replace")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", reject_target_chmod)

    persist_config(cfg, path=target, backup=False)

    assert tomllib.loads(target.read_text())["port"] == 18795
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# A4: non-conflicting concurrent edits survive a save
# ---------------------------------------------------------------------------


def test_persist_merges_concurrent_disk_edits(tmp_path):
    target = tmp_path / "config.toml"
    _write_small_config(target)

    cfg = load_config(target)
    cfg.port = 18793  # field X: mutated in memory

    # Another writer (e.g. a Web-UI save) adds field Y while the wizard
    # is still sitting at a prompt.
    target.write_text(
        "\n".join(
            [
                "[llm]",
                'provider = "openrouter"',
                'model = "deepseek/deepseek-v4-flash"',
                "",
                "[memory]",
                "flush_enabled = true",
                "",
            ]
        )
    )

    persist_config(cfg, path=target)

    data = tomllib.loads(target.read_text())
    assert data["port"] == 18793  # X survives
    assert data["memory"]["flush_enabled"] is True  # Y survives
    assert data["llm"]["provider"] == "openrouter"


def test_persist_serializes_overlapping_nonconflicting_writers(tmp_path, monkeypatch):
    import opensquilla.onboarding.config_store as config_store

    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")
    first_cfg = load_config(target)
    second_cfg = load_config(target)
    first_cfg.port = 18795
    second_cfg.memory.flush_enabled = True

    first_planned = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_planned = threading.Event()
    call_guard = threading.Lock()
    call_count = 0
    original_plan = config_store._persist_plan

    def gated_plan(*args, **kwargs):
        nonlocal call_count
        result = original_plan(*args, **kwargs)
        with call_guard:
            call_count += 1
            position = call_count
        if position == 1:
            first_planned.set()
            assert release_first.wait(timeout=5)
        else:
            second_planned.set()
        return result

    def persist_second() -> None:
        second_started.set()
        persist_config(second_cfg, path=target, backup=False)

    monkeypatch.setattr(config_store, "_persist_plan", gated_plan)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(persist_config, first_cfg, path=target, backup=False)
        assert first_planned.wait(timeout=5)
        second = pool.submit(persist_second)
        assert second_started.wait(timeout=5)
        overlapped_plan = second_planned.wait(timeout=0.5)
        release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert overlapped_plan is False
    data = tomllib.loads(target.read_text())
    assert data["port"] == 18795
    assert data["memory"]["flush_enabled"] is True


def test_force_persist_paths_are_consumed_after_successful_commit(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")
    cfg = load_config(target)
    cfg.llm_ensemble.enabled = False
    cfg.mark_force_persist("llm_ensemble.enabled")

    persist_config(cfg, path=target, backup=False)

    assert cfg.force_persist_paths() == set()
    target.write_text(target.read_text().replace("enabled = false", "enabled = true"))
    cfg.port = 18795
    persist_config(cfg, path=target, backup=False)

    data = tomllib.loads(target.read_text())
    assert data["llm_ensemble"]["enabled"] is True
    assert data["port"] == 18795


def test_force_persist_none_removes_a_drifted_disk_value(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")
    cfg = load_config(target)
    target.write_text(
        'port = 18791\n[memory.embedding.remote]\nbase_url = "https://drifted.test/v1"\n'
    )
    cfg.mark_force_persist("memory.embedding.remote.base_url")

    persist_config(cfg, path=target, backup=False)

    data = tomllib.loads(target.read_text())
    assert "base_url" not in data.get("memory", {}).get("embedding", {}).get("remote", {})
    assert cfg.force_persist_paths() == set()


# ---------------------------------------------------------------------------
# A6: baselines are instance-scoped — two live objects for the same path
# must not contaminate each other's diffs (R4).
# ---------------------------------------------------------------------------


def test_second_object_for_same_path_does_not_revert_first_writers_save(tmp_path):
    """Two load_config objects for one file: object B's save must not diff
    against object A's post-persist snapshot and silently revert A's write."""
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    a = load_config(target)
    b = load_config(target)

    a.port = 1111
    persist_config(a, path=target)
    assert tomllib.loads(target.read_text())["port"] == 1111

    b.memory.flush_enabled = True
    persist_config(b, path=target)

    data = tomllib.loads(target.read_text())
    assert data["memory"]["flush_enabled"] is True
    # B never touched port, so A's persisted 1111 must survive B's save.
    assert data["port"] == 1111


def test_disk_fallback_model_save_does_not_revert_other_writers_change(tmp_path):
    """A config that outlived another writer's save (no fresher baseline than
    its own load) must still not revert the on-disk change it never touched."""
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    b = load_config(target)

    # Another PROCESS (no shared state at all) changes port on disk.
    target.write_text("port = 2222\n")

    b.memory.flush_enabled = True
    persist_config(b, path=target)

    data = tomllib.loads(target.read_text())
    assert data["memory"]["flush_enabled"] is True
    assert data["port"] == 2222


def test_save_as_to_different_path_carries_loaded_values(tmp_path):
    """The instance baseline only describes the file the config was loaded
    from: persisting to a DIFFERENT path (save-as/copy) must not diff the
    copy against the instance's own load snapshot — that would erase every
    loaded value and write a near-empty file."""
    source = tmp_path / "config.toml"
    source.write_text('port = 18795\n[llm]\nprovider = "openrouter"\nmodel = "custom/model-x"\n')
    other = tmp_path / "copy.toml"

    cfg = load_config(source)
    persist_config(cfg, path=other, backup=False)

    data = tomllib.loads(other.read_text())
    assert data["port"] == 18795
    assert data["llm"]["model"] == "custom/model-x"
    # Values equal to the built-in default (provider = "openrouter") may be
    # omitted from the sparse copy; the copy must still LOAD equivalently.
    reloaded = load_config(other)
    assert reloaded.port == 18795
    assert reloaded.llm.provider == "openrouter"
    assert reloaded.llm.model == "custom/model-x"

    # The save-as must not disturb the instance's association with its own
    # file: a later save back to the source still diffs against the load
    # snapshot (only the mutated field lands, nothing is erased).
    cfg.memory.flush_enabled = True
    persist_config(cfg, path=source, backup=False)
    source_data = tomllib.loads(source.read_text())
    assert source_data["memory"]["flush_enabled"] is True
    assert source_data["port"] == 18795
    assert source_data["llm"]["model"] == "custom/model-x"


# ---------------------------------------------------------------------------
# A7: explicit search_api_key must persist even when it equals the
# env-absorbed value (R5).
# ---------------------------------------------------------------------------


def test_explicit_search_key_persists_when_equal_to_env_value(tmp_path, monkeypatch):
    from opensquilla.onboarding.mutations import upsert_search_provider

    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SEARCH_API_KEY", "tvly-synthetic-abc")
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    cfg = load_config(target)
    res = upsert_search_provider(
        cfg, provider_id="tavily", api_key="tvly-synthetic-abc"
    )
    assert res.public_payload["api_key_source"] == "explicit"
    persist_config(res.config, path=target)

    data = tomllib.loads(target.read_text())
    assert data["search_provider"] == "tavily"
    # The operator explicitly typed the key: it must be stored so search
    # keeps working after the env var disappears.
    assert data["search_api_key"] == "tvly-synthetic-abc"

    monkeypatch.delenv("OPENSQUILLA_GATEWAY_SEARCH_API_KEY")
    assert load_config(target).search_api_key == "tvly-synthetic-abc"


def test_env_only_search_key_still_never_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_SEARCH_API_KEY", "tvly-synthetic-env")
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    cfg = load_config(target)
    cfg.port = 18795  # unrelated explicit change
    persist_config(cfg, path=target)

    assert "tvly-synthetic-env" not in target.read_text()


def test_explicit_audio_key_persists_when_equal_to_env_value(tmp_path, monkeypatch):
    """Same env-coincidence class as search_api_key: the audio provider key
    section is pydantic-settings-bound, so an explicit entry equal to the
    env-absorbed value used to diff as unchanged and vanish from the file —
    leaving audio enabled with no stored credential."""
    from opensquilla.onboarding.mutations import upsert_audio_provider

    monkeypatch.setenv(
        "OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY", "el-synthetic-abc"
    )
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    cfg = load_config(target)
    res = upsert_audio_provider(
        cfg, provider_id="elevenlabs", api_key="el-synthetic-abc", enabled=True
    )
    assert res.public_payload["api_key_source"] == "explicit"
    persist_config(res.config, path=target)

    data = tomllib.loads(target.read_text())
    assert data["audio"]["enabled"] is True
    assert data["audio"]["providers"]["elevenlabs"]["api_key"] == "el-synthetic-abc"

    monkeypatch.delenv("OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY")
    assert load_config(target).audio.providers.elevenlabs.api_key == "el-synthetic-abc"


def test_env_only_audio_key_still_never_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "OPENSQUILLA_AUDIO_PROVIDERS__ELEVENLABS__API_KEY", "el-synthetic-env"
    )
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    cfg = load_config(target)
    cfg.port = 18795  # unrelated explicit change
    persist_config(cfg, path=target)

    assert "el-synthetic-env" not in target.read_text()


def test_explicit_image_key_persists_when_equal_to_env_value(tmp_path, monkeypatch):
    from opensquilla.onboarding.mutations import upsert_image_generation_provider

    monkeypatch.setenv(
        "OPENSQUILLA_IMAGE_GENERATION_PROVIDERS__OPENAI__API_KEY", "sk-img-synthetic"
    )
    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")

    cfg = load_config(target)
    res = upsert_image_generation_provider(
        cfg,
        provider_id="openai",
        primary="openai/gpt-image-1",
        api_key="sk-img-synthetic",
        enabled=True,
    )
    persist_config(res.config, path=target)

    data = tomllib.loads(target.read_text())
    assert (
        data["image_generation"]["providers"]["openai"]["api_key"] == "sk-img-synthetic"
    )

    monkeypatch.delenv("OPENSQUILLA_IMAGE_GENERATION_PROVIDERS__OPENAI__API_KEY")
    reloaded = load_config(target)
    assert reloaded.image_generation.providers.openai.api_key == "sk-img-synthetic"


# ---------------------------------------------------------------------------
# A8: boot-time env resolutions of llm.base_url / llm.proxy must not be
# baked into the file by an unrelated gateway persist (R13).
# ---------------------------------------------------------------------------


def test_gateway_persist_does_not_bake_env_resolved_base_url_and_proxy(
    tmp_path, monkeypatch
):
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    monkeypatch.setenv("OPENAI_BASE_URL", "http://intranet-proxy.local:8080/v1")
    monkeypatch.setenv("OPENSQUILLA_LLM_PROXY", "http://127.0.0.1:7890")
    target = tmp_path / "config.toml"
    target.write_text('[llm]\nprovider = "openai"\nmodel = "gpt-5"\n')

    cfg = GatewayConfig.load_from_toml(target)
    cfg.config_path = str(target)
    runtime = resolve_llm_runtime_config(cfg)
    assert runtime.base_url == "http://intranet-proxy.local:8080/v1"
    assert cfg.llm.base_url == "http://intranet-proxy.local:8080/v1"
    assert cfg.llm.proxy == "http://127.0.0.1:7890"

    cfg.port = 18795  # unrelated change through the gateway persist path
    persist_config(cfg, path=target)

    text = target.read_text()
    assert "intranet-proxy.local" not in text
    assert "127.0.0.1:7890" not in text
    assert tomllib.loads(text)["port"] == 18795


def test_gateway_persist_keeps_operator_edit_over_env_override(tmp_path, monkeypatch):
    """An operator change made after boot beats the env-override restore."""
    from opensquilla.gateway.llm_runtime import resolve_llm_runtime_config

    monkeypatch.setenv("OPENAI_BASE_URL", "http://intranet-proxy.local:8080/v1")
    target = tmp_path / "config.toml"
    target.write_text('[llm]\nprovider = "openai"\nmodel = "gpt-5"\n')

    cfg = GatewayConfig.load_from_toml(target)
    cfg.config_path = str(target)
    resolve_llm_runtime_config(cfg)

    cfg.llm.base_url = "https://operator.example.test/v1"  # explicit hot edit
    persist_config(cfg, path=target)

    data = tomllib.loads(target.read_text())
    assert data["llm"]["base_url"] == "https://operator.example.test/v1"


# ---------------------------------------------------------------------------
# A9: the config file vanishing between load and persist must not drop the
# unchanged loaded sections from the recreated file (F6).
# ---------------------------------------------------------------------------


def _write_provider_config(target) -> None:
    target.write_text(
        "\n".join(
            [
                'search_provider = "tavily"',
                'search_api_key = "tvly-synthetic-old"',
                "",
                "[llm]",
                'provider = "openrouter"',
                'model = "deepseek/deepseek-v4-flash"',
                'api_key = "sk-or-synthetic"',
                "",
            ]
        )
    )


def test_persist_recreates_loaded_sections_when_file_vanished(tmp_path):
    """load -> file deleted -> mutate one field -> persist: the recreated
    file must contain BOTH the mutation and the previously loaded sections
    (notably the [llm] block and its api_key), not just the sparse diff."""
    target = tmp_path / "config.toml"
    _write_provider_config(target)

    cfg = load_config(target)
    target.unlink()  # reset from another session / operator mv

    cfg.search_api_key = "tvly-synthetic-new"
    result = persist_config(cfg, path=target)
    assert result.path == target

    data = tomllib.loads(target.read_text())
    assert data["search_api_key"] == "tvly-synthetic-new"  # the mutation
    assert data["llm"]["provider"] == "openrouter"  # loaded sections survive
    assert data["llm"]["model"] == "deepseek/deepseek-v4-flash"
    assert data["llm"]["api_key"] == "sk-or-synthetic"

    reloaded = load_config(target)
    assert reloaded.llm.provider == "openrouter"
    assert reloaded.llm.api_key == "sk-or-synthetic"
    assert reloaded.search_api_key == "tvly-synthetic-new"


def test_persist_recreates_loaded_sections_for_mutation_clone(tmp_path):
    """Same vanish scenario through the RPC-shaped path: the mutation clone
    (model_copy(deep=True) inside the mutation) carries the load-time raw
    contents, so persisting the clone recreates the full file too."""
    from opensquilla.onboarding.mutations import upsert_search_provider

    target = tmp_path / "config.toml"
    _write_provider_config(target)

    cfg = load_config(target)
    res = upsert_search_provider(cfg, provider_id="tavily", api_key="tvly-synthetic-new")
    target.unlink()

    persist_config(res.config, path=target)

    data = tomllib.loads(target.read_text())
    assert data["search_api_key"] == "tvly-synthetic-new"
    assert data["llm"]["provider"] == "openrouter"
    assert data["llm"]["api_key"] == "sk-or-synthetic"


def test_persist_after_vanish_uses_last_written_contents(tmp_path):
    """A persist refreshes the remembered raw contents: a later
    vanish-then-persist recreates from what was just written, keeping the
    earlier save's changes alongside the new one."""
    target = tmp_path / "config.toml"
    _write_provider_config(target)

    cfg = load_config(target)
    cfg.port = 18795
    persist_config(cfg, path=target)

    target.unlink()
    cfg.memory.flush_enabled = True
    persist_config(cfg, path=target)

    data = tomllib.loads(target.read_text())
    assert data["port"] == 18795  # first save survives the vanish
    assert data["memory"]["flush_enabled"] is True
    assert data["llm"]["api_key"] == "sk-or-synthetic"


def test_persist_vanished_file_does_not_bake_env_values(tmp_path, monkeypatch):
    """The recreated file is rebuilt from the load-time DISK contents, so
    env-derived values still never leak into it."""
    monkeypatch.setenv("OPENSQUILLA_AUTH_TOKEN", "tok-synthetic-vanish")
    target = tmp_path / "config.toml"
    _write_provider_config(target)

    cfg = load_config(target)
    assert cfg.auth.token == "tok-synthetic-vanish"
    target.unlink()

    cfg.port = 18795
    persist_config(cfg, path=target)

    text = target.read_text()
    assert "tok-synthetic-vanish" not in text
    data = tomllib.loads(text)
    assert data["port"] == 18795
    assert data["llm"]["api_key"] == "sk-or-synthetic"


def test_persist_never_loaded_missing_file_still_writes_sparse(tmp_path):
    """A config whose file never existed keeps the old behavior: the first
    persist writes only what differs from defaults (no full-default dump)."""
    target = tmp_path / "config.toml"

    cfg = load_config(target)  # first run: nothing on disk
    cfg.port = 18795
    persist_config(cfg, path=target)

    data = tomllib.loads(target.read_text())
    assert data["port"] == 18795
    assert "memory" not in data  # defaults are not dumped wholesale


def test_fresh_tokenrhythm_save_persists_explicit_provider_identity(tmp_path):
    target = tmp_path / "config.toml"
    before = load_config(target)

    changed = upsert_llm_provider(
        before,
        provider_id="tokenrhythm",
        api_key="sk_tr_abcdefghijklmnop",
    ).config
    persist_config(changed, path=target, backup=False)

    raw = tomllib.loads(target.read_text())
    assert raw["llm"]["provider"] == "tokenrhythm"
    reloaded = load_config(target)
    assert reloaded.llm.provider == changed.llm.provider == "tokenrhythm"
    assert reloaded.llm.model == changed.llm.model
    assert reloaded.llm.base_url == changed.llm.base_url
    assert reloaded.squilla_router.preset_binding == "follow_primary"
    assert reloaded.squilla_router.tiers == changed.squilla_router.tiers
    assert {
        tier["provider"] for tier in reloaded.squilla_router.tiers.values()
    } == {"tokenrhythm"}


def test_fresh_tokenrhythm_env_and_custom_endpoint_round_trip(tmp_path):
    target = tmp_path / "config.toml"
    changed = upsert_llm_provider(
        load_config(target),
        provider_id="tokenrhythm",
        api_key_env="TOKENRHYTHM_API_KEY",
        model="custom-tr-model",
        base_url="https://tr-proxy.example/v1",
    ).config
    persist_config(changed, path=target, backup=False)

    raw = tomllib.loads(target.read_text())
    assert raw["llm"]["provider"] == "tokenrhythm"
    assert raw["llm"]["api_key_env"] == "TOKENRHYTHM_API_KEY"
    reloaded = load_config(target)
    assert reloaded.llm.provider == "tokenrhythm"
    assert reloaded.llm.model == "custom-tr-model"
    assert reloaded.llm.base_url == "https://tr-proxy.example/v1"


# ---------------------------------------------------------------------------
# A10: persist-plan warnings go through structlog (F42).
# ---------------------------------------------------------------------------


def test_persist_plan_warnings_use_structlog(tmp_path):
    import structlog

    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")
    cfg = load_config(target)
    target.write_text("port = [not-valid-toml\n")  # corrupt after load

    cfg.port = 18795
    with structlog.testing.capture_logs() as captured:
        persist_config(cfg, path=target)

    events = [entry["event"] for entry in captured]
    assert "onboarding.config_persist_unreadable_toml" in events
    entry = next(
        e for e in captured if e["event"] == "onboarding.config_persist_unreadable_toml"
    )
    assert entry["log_level"] == "warning"
    assert entry["path"] == str(target)
    assert "error" in entry


def test_persist_plan_invalid_existing_warning_uses_structlog(tmp_path):
    import structlog

    target = tmp_path / "config.toml"
    target.write_text("port = 18791\n")
    cfg = load_config(target)
    target.write_text('port = "not-a-port"\n')  # parseable TOML, invalid model

    cfg.port = 18795
    with structlog.testing.capture_logs() as captured:
        persist_config(cfg, path=target)

    entry = next(
        e for e in captured if e["event"] == "onboarding.config_persist_invalid_existing"
    )
    assert entry["log_level"] == "warning"
    assert entry["path"] == str(target)
    assert entry["error"] == "ValidationError"
