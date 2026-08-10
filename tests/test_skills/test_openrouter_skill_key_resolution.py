"""Regression tests for bundled OpenRouter skill key fallback.

The bundled scripts are intentionally imported by file path here because the
skill directories contain hyphens and run as standalone subprocess scripts.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_SCRIPT = (
    REPO_ROOT
    / "src/opensquilla/skills/bundled/nano-banana-pro/scripts/generate_image.py"
)
VIDEO_SCRIPT = (
    REPO_ROOT
    / "src/opensquilla/skills/bundled/seedance-2-prompt/scripts/generate_video.py"
)


def _load_script(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def image_script() -> ModuleType:
    return _load_script(IMAGE_SCRIPT, "_opensquilla_test_generate_image")


@pytest.fixture(scope="module")
def video_script() -> ModuleType:
    return _load_script(VIDEO_SCRIPT, "_opensquilla_test_generate_video")


@pytest.fixture
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in (
        "OPENROUTER_API_KEY",
        "OPENSQUILLA_META_CAPABILITY_PROVIDER",
        "OPENSQUILLA_META_CAPABILITY_API_KEY",
        "OPENSQUILLA_META_CAPABILITY_BASE_URL",
        "OPENSQUILLA_META_CAPABILITY_PROXY",
        "OPENSQUILLA_META_OPENROUTER_API_KEY",
        "OPENSQUILLA_LLM_API_KEY",
        "OPENSQUILLA_LLM_PROVIDER",
        "OPENSQUILLA_GATEWAY_CONFIG_PATH",
        "OPENSQUILLA_STATE_DIR",
        "CUSTOM_OPENROUTER_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "ARK_API_KEY",
        "VOLC_ARK_API_KEY",
        "BYTEPLUS_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(workspace)
    return tmp_path


def _write_toml(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def _valid_image_bytes(*, image_format: str = "PNG", size: tuple[int, int] = (2, 2)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=(24, 48, 96)).save(output, format=image_format)
    return output.getvalue()


def _resolve_video_openrouter(video_script: ModuleType) -> str | None:
    return video_script._resolve_api_key(
        None,
        ("OPENROUTER_API_KEY",),
        provider_name="openrouter",
    )


def _set_parent_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str = "openrouter",
    key: str = "parent-capability-key",
    base_url: str = "https://openrouter.ai/api/v1",
    proxy: str = "",
) -> None:
    monkeypatch.setenv("OPENSQUILLA_META_CAPABILITY_PROVIDER", provider)
    monkeypatch.setenv("OPENSQUILLA_META_CAPABILITY_API_KEY", key)
    monkeypatch.setenv("OPENSQUILLA_META_CAPABILITY_BASE_URL", base_url)
    if proxy:
        monkeypatch.setenv("OPENSQUILLA_META_CAPABILITY_PROXY", proxy)


def test_paid_media_auth_docs_match_parent_bound_runtime_contract() -> None:
    for path in (
        IMAGE_SCRIPT.parents[1] / "SKILL.md",
        VIDEO_SCRIPT.parents[1] / "SKILL.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert "OPENSQUILLA_META_CAPABILITY_PROVIDER" in text
        assert "OPENSQUILLA_META_CAPABILITY_API_KEY" in text
        assert "OPENSQUILLA_META_CAPABILITY_BASE_URL" in text
        assert "OPENSQUILLA_META_CAPABILITY_PROXY" in text
        assert "OPENSQUILLA_META_OPENROUTER_API_KEY" in text
        assert "never discovers or parses `opensquilla.toml`" in text
        assert "OPENSQUILLA_LLM_API_KEY" not in text
        assert "`./opensquilla.toml`" not in text


def test_openrouter_skills_use_parent_injected_active_credential(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_toml(
        Path.cwd() / "opensquilla.toml",
        """
        [llm]
        provider = "openrouter"
        api_key_env = "AWS_SECRET_ACCESS_KEY"
        """,
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "workspace-selected-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-openrouter-key")
    monkeypatch.setenv(
        "OPENSQUILLA_META_OPENROUTER_API_KEY",
        "active-config-openrouter-key",
    )

    assert image_script.resolve_api_key(None) == "active-config-openrouter-key"
    assert _resolve_video_openrouter(video_script) == "active-config-openrouter-key"


def test_openrouter_skills_never_rediscover_workspace_config_api_key_env(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_toml(
        Path.cwd() / "opensquilla.toml",
        """
        [llm]
        provider = "openrouter"
        api_key_env = "AWS_SECRET_ACCESS_KEY"
        """,
    )
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-used-as-bearer")

    assert image_script.resolve_api_key(None) is None
    assert _resolve_video_openrouter(video_script) is None


def test_openrouter_skills_do_not_treat_other_provider_llm_env_as_openrouter(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "anthropic-key")

    assert image_script.resolve_api_key(None) is None
    assert _resolve_video_openrouter(video_script) is None


def test_explicit_media_key_wins_parent_injected_key(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OPENSQUILLA_META_OPENROUTER_API_KEY",
        "active-config-openrouter-key",
    )

    assert image_script.resolve_api_key("explicit-media-key") == "explicit-media-key"
    assert (
        video_script._resolve_api_key(
            "explicit-media-key",
            ("OPENROUTER_API_KEY",),
            provider_name="openrouter",
        )
        == "explicit-media-key"
    )


def test_openrouter_specific_env_still_wins_for_openrouter_skills(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-env-key")
    monkeypatch.setenv("OPENSQUILLA_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "anthropic-key")

    assert image_script.resolve_api_key(None) == "openrouter-env-key"
    assert _resolve_video_openrouter(video_script) == "openrouter-env-key"


def test_non_openrouter_video_provider_uses_only_its_provider_env(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    monkeypatch.setenv("OPENSQUILLA_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "openrouter-key")

    assert (
        video_script._resolve_api_key(
            None,
            ("ARK_API_KEY", "VOLC_ARK_API_KEY"),
            provider_name="volcengine",
        )
        == "ark-key"
    )


def test_paid_media_clients_resolve_generic_parent_connection_as_atomic_tuple(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_runtime
    key = "parent-key-must-stay-private"
    proxy = "http://proxy-user:proxy-secret@proxy.example:8080"
    base_url = "https://router-gateway.example/openrouter/v1"
    _set_parent_connection(
        monkeypatch,
        key=key,
        base_url=base_url,
        proxy=proxy,
    )

    image_connection = image_script._resolve_provider_connection(None, "")
    video_connection = video_script._resolve_provider_connection(
        video_script.PROVIDERS["openrouter"],
        None,
        "",
    )

    assert image_connection is not None
    assert video_connection is not None
    for connection in (image_connection, video_connection):
        assert connection.provider == "openrouter"
        assert connection.api_key == key
        assert connection.base_url == base_url
        assert connection.proxy == proxy
        assert key not in repr(connection)
        assert "proxy-secret" not in repr(connection)


def test_paid_media_generic_parent_connection_requires_key_provider_and_base_url(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key = "partial-parent-key-must-not-submit"
    monkeypatch.setenv("OPENSQUILLA_META_CAPABILITY_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENSQUILLA_META_CAPABILITY_API_KEY", key)
    image_calls = 0
    video_calls = 0

    def image_submit(**_: object) -> tuple[bytes, dict]:
        nonlocal image_calls
        image_calls += 1
        raise AssertionError("partial connection must not submit")

    def video_submit(**_: object) -> tuple[dict, dict]:
        nonlocal video_calls
        video_calls += 1
        raise AssertionError("partial connection must not submit")

    monkeypatch.setattr(image_script, "_try_one_attempt", image_submit)
    monkeypatch.setattr(video_script, "_run_attempt", video_submit)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(isolated_runtime / "partial.png"),
        ],
    )
    assert image_script.main() == image_script.SAFE_NO_SUBMIT_EXIT_CODE
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(isolated_runtime / "partial.mp4"),
            "--duration",
            "4",
        ],
    )
    assert video_script.main() == video_script.SAFE_NO_SUBMIT_EXIT_CODE

    captured = capsys.readouterr()
    assert image_calls == 0
    assert video_calls == 0
    assert "incomplete parent provider connection" in captured.err
    assert key not in captured.err


@pytest.mark.parametrize("source", ["generic", "canonical", "legacy"])
def test_image_bound_credentials_reject_cross_origin_override_before_post(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    key = f"{source}-image-key-must-not-submit"
    if source == "generic":
        _set_parent_connection(monkeypatch, key=key)
    elif source == "legacy":
        monkeypatch.setenv("OPENSQUILLA_META_OPENROUTER_API_KEY", key)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)
    calls = 0

    def unexpected_post(**_: object) -> tuple[bytes, dict]:
        nonlocal calls
        calls += 1
        raise AssertionError("cross-origin credential reuse must not post")

    monkeypatch.setattr(image_script, "_try_one_attempt", unexpected_post)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(isolated_runtime / f"cross-{source}.png"),
            "--base-url",
            "https://attacker.example/collect?token=endpoint-secret",
        ],
    )

    assert image_script.main() == image_script.SAFE_NO_SUBMIT_EXIT_CODE
    captured = capsys.readouterr()
    assert calls == 0
    assert key not in captured.err
    assert "attacker.example" not in captured.err
    assert "endpoint-secret" not in captured.err


@pytest.mark.parametrize("source", ["generic", "canonical", "legacy"])
def test_video_bound_credentials_reject_cross_origin_override_before_post(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    key = f"{source}-video-key-must-not-submit"
    if source == "generic":
        _set_parent_connection(monkeypatch, key=key)
    elif source == "legacy":
        monkeypatch.setenv("OPENSQUILLA_META_OPENROUTER_API_KEY", key)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)
    calls = 0

    def unexpected_post(**_: object) -> tuple[dict, dict]:
        nonlocal calls
        calls += 1
        raise AssertionError("cross-origin credential reuse must not post")

    monkeypatch.setattr(video_script, "_run_attempt", unexpected_post)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(isolated_runtime / f"cross-{source}.mp4"),
            "--duration",
            "4",
            "--base-url",
            "https://attacker.example/collect?token=endpoint-secret",
        ],
    )

    assert video_script.main() == video_script.SAFE_NO_SUBMIT_EXIT_CODE
    captured = capsys.readouterr()
    assert calls == 0
    assert key not in captured.err
    assert "attacker.example" not in captured.err
    assert "endpoint-secret" not in captured.err


def test_paid_media_connections_allow_same_origin_paths_and_explicit_custom_origins(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del isolated_runtime
    _set_parent_connection(monkeypatch, key="parent-path-key")
    same_origin = "https://openrouter.ai:443/compatible/v1"
    image_parent = image_script._resolve_provider_connection(None, same_origin)
    video_parent = video_script._resolve_provider_connection(
        video_script.PROVIDERS["openrouter"],
        None,
        same_origin,
    )
    assert image_parent is not None and image_parent.base_url == same_origin
    assert video_parent is not None and video_parent.base_url == same_origin

    custom_origin = "https://private-router.example/v1"
    image_explicit = image_script._resolve_provider_connection(
        "explicit-image-key",
        custom_origin,
    )
    video_explicit = video_script._resolve_provider_connection(
        video_script.PROVIDERS["openrouter"],
        "explicit-video-key",
        custom_origin,
    )
    assert image_explicit is not None and image_explicit.base_url == custom_origin
    assert video_explicit is not None and video_explicit.base_url == custom_origin


def test_generic_parent_proxy_is_limited_to_matching_provider_connection(
    image_script: ModuleType,
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del image_script, isolated_runtime
    _set_parent_connection(
        monkeypatch,
        key="openrouter-parent-key",
        proxy="http://proxy.example:8080",
    )

    with pytest.raises(
        video_script._RequestError,
        match="does not match selected provider",
    ):
        video_script._resolve_provider_connection(
            video_script.PROVIDERS["volcengine"],
            None,
            "",
        )


def test_paid_media_authenticated_requests_receive_only_selected_custom_proxy(
    image_script: ModuleType,
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = "http://proxy-user:proxy-secret@proxy.example:8080"
    seen: list[tuple[str, str]] = []

    def image_open(
        request: object,
        *,
        timeout: int,
        proxy_url: str = "",
    ) -> io.BytesIO:
        del timeout
        seen.append((request.full_url, proxy_url))
        return io.BytesIO(b"{}")

    def video_open(
        request: object,
        *,
        timeout: int,
        proxy_url: str = "",
    ) -> io.BytesIO:
        del timeout
        seen.append((request.full_url, proxy_url))
        return io.BytesIO(b'{"status":"processing"}')

    monkeypatch.setattr(image_script, "_open_authenticated_request", image_open)
    image_script.post_chat_completions(
        image_script.DEFAULT_BASE_URL,
        "image-key",
        {"model": image_script.DEFAULT_MODEL},
        30,
        proxy_url=proxy,
    )
    monkeypatch.setattr(video_script, "_open_authenticated_request", video_open)
    video_script._http_request(
        "GET",
        "https://openrouter.ai/api/v1/videos/job-safe",
        "video-key",
        trusted_base_url="https://openrouter.ai/api/v1",
        proxy_url=proxy,
    )

    assert seen == [
        ("https://openrouter.ai/api/v1/chat/completions", proxy),
        ("https://openrouter.ai/api/v1/videos/job-safe", proxy),
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://openrouter.ai/api/v1",
        "https://user:password@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?token=endpoint-secret",
        "https://openrouter.ai/api/v1#private-fragment",
        "https://openrouter.ai:0/api/v1",
        "https://openrouter.ai:99999/api/v1",
        "https://openrouter.ai\\@attacker.example/api/v1",
    ],
)
def test_image_rejects_unsafe_authenticated_api_url_before_open(
    image_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    key = "image-key-must-not-open"
    calls = 0

    def unexpected_open(*_: object, **__: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("unsafe authenticated URL must not be opened")

    monkeypatch.setattr(image_script, "_open_authenticated_request", unexpected_open)
    with pytest.raises(
        image_script._ImageRequestError,
        match="invalid authenticated API URL",
    ) as caught:
        image_script.post_chat_completions(
            base_url,
            key,
            {"model": image_script.DEFAULT_MODEL},
            30,
        )

    assert calls == 0
    assert key not in str(caught.value)
    assert "endpoint-secret" not in str(caught.value)


def test_image_success_persists_sanitized_provider_receipt(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-or-secret-must-not-appear"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    output = isolated_runtime / "generated.png"
    original_replace = image_script.os.replace
    replacements: list[tuple[Path, Path]] = []
    valid_png = _valid_image_bytes()

    def fake_attempt(**_: object) -> tuple[bytes, dict]:
        return (
            valid_png,
            {
                "id": "req-image-123",
                "usage": {"cost": 0.05, "provider_debug": secret},
                "raw_debug": secret,
            },
        )

    monkeypatch.setattr(image_script, "_try_one_attempt", fake_attempt)

    def record_replace(source: object, destination: object) -> None:
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr(image_script.os, "replace", record_replace)
    monkeypatch.setattr(
        sys,
        "argv",
        ["generate_image.py", "--prompt", "synthetic", "--filename", str(output)],
    )

    assert image_script.main() == 0
    stdout = capsys.readouterr().out
    receipt_path = output.with_suffix(".png.receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert output.read_bytes() == valid_png
    assert receipt["status"] == "generated"
    assert receipt["provider"] == "openrouter"
    assert receipt["model"] == image_script.DEFAULT_MODEL
    assert receipt["request_id"] == "req-image-123"
    assert receipt["placeholder"] is False
    assert receipt["usage"] == {"cost": 0.05}
    assert any(destination == receipt_path for _, destination in replacements)
    assert stdout.splitlines()[0] == str(output)
    assert "IMAGE_GENERATION_RECEIPT:" in stdout
    assert secret not in stdout
    assert secret not in receipt_path.read_text(encoding="utf-8")


def test_image_http_policy_rejection_is_single_attempt_and_sanitized(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-policy-test")
    output = isolated_runtime / "policy-placeholder.png"
    secret_url = "https://openrouter.ai/api/v1/chat?token=signed-secret#fragment"
    policy_code = "InputImageSensitiveContentDetected.PrivacyInformation"
    calls = 0

    def fail_open(*args: object, **kwargs: object) -> object:
        nonlocal calls
        del args, kwargs
        calls += 1
        raw_provider_error = json.dumps(
            {
                "error": {
                    "code": policy_code,
                    "message": "request_id=req-private; token=raw-secret",
                }
            }
        )
        body = json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": "provider message must be discarded",
                    "metadata": {
                        "raw": raw_provider_error,
                        "request_id": "req-private",
                        "url": secret_url,
                    },
                }
            }
        ).encode()
        raise image_script.urllib.error.HTTPError(
            secret_url,
            400,
            "provider message must be discarded",
            {},
            io.BytesIO(body),
        )

    def write_placeholder(path: Path, prompt: str, aspect_ratio: str) -> None:
        del prompt, aspect_ratio
        path.write_bytes(b"synthetic-policy-placeholder")

    monkeypatch.setattr(image_script, "_open_authenticated_request", fail_open)
    monkeypatch.setattr(image_script, "_write_placeholder_png", write_placeholder)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "clearly fictional synthetic character",
            "--filename",
            str(output),
            "--max-retries",
            "3",
            "--fallback-model",
            "google/gemini-3-pro-image-preview",
            "--placeholder-on-fail",
            "yes",
        ],
    )

    assert image_script.main() == 0
    captured = capsys.readouterr()
    assert calls == 1
    assert output.read_bytes() == b"synthetic-policy-placeholder"
    receipt_path = output.with_suffix(".png.receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt == {
        "model": image_script.DEFAULT_MODEL,
        "placeholder": True,
        "policy_code": policy_code,
        "provider": "openrouter",
        "reason": "provider_policy_rejected",
        "status": "policy_rejected",
    }
    assert captured.out.splitlines()[0] == str(output)
    assert '"status": "policy_rejected"' in captured.out
    assert '"status": "generated"' not in captured.out
    serialized = captured.out + captured.err + receipt_path.read_text(encoding="utf-8")
    assert "req-private" not in serialized
    assert "signed-secret" not in serialized
    assert "raw-secret" not in serialized
    assert "provider message must be discarded" not in serialized


def test_image_policy_rejection_without_placeholder_persists_honest_failure(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-policy-test")
    output = isolated_runtime / "policy-failed.png"
    calls: list[str] = []
    policy_code = "ContentPolicyViolation"

    def reject_attempt(**kwargs: object) -> tuple[bytes, dict]:
        calls.append(str(kwargs["model"]))
        raise image_script._ImageRequestError(
            "upstream image provider rejected generation",
            provider_code=policy_code,
            retryable=False,
        )

    monkeypatch.setattr(image_script, "_try_one_attempt", reject_attempt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--max-retries",
            "2",
            "--fallback-model",
            "google/gemini-3-pro-image-preview",
        ],
    )

    assert image_script.main() == 1
    captured = capsys.readouterr()
    assert calls == [image_script.DEFAULT_MODEL]
    assert not output.exists()
    receipt = json.loads(
        output.with_suffix(".png.receipt.json").read_text(encoding="utf-8")
    )
    assert receipt == {
        "model": image_script.DEFAULT_MODEL,
        "placeholder": False,
        "policy_code": policy_code,
        "provider": "openrouter",
        "reason": "provider_policy_rejected",
        "status": "policy_rejected",
    }
    assert "policy_code=ContentPolicyViolation" in captured.err
    receipt_line = captured.out.strip()
    assert receipt_line.startswith("IMAGE_GENERATION_RECEIPT: ")
    assert json.loads(receipt_line.partition(": ")[2]) == receipt


@pytest.mark.parametrize(
    ("status", "exit_name"),
    [
        (401, "PROVIDER_AUTH_INVALID_EXIT_CODE"),
        (402, "PROVIDER_INSUFFICIENT_CREDITS_EXIT_CODE"),
        (429, "PROVIDER_RATE_LIMITED_EXIT_CODE"),
    ],
)
def test_image_credential_failure_uses_reserved_parent_exit_without_placeholder(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    exit_name: str,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / f"credential-{status}.png"

    def fail_attempt(**_: object) -> tuple[bytes, dict]:
        raise image_script._ImageRequestError(
            f"OpenRouter HTTP {status}",
            status=status,
            retryable=False,
        )

    monkeypatch.setattr(image_script, "_try_one_attempt", fail_attempt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--placeholder-on-fail",
            "yes",
        ],
    )

    assert image_script.main() == getattr(image_script, exit_name)
    assert not output.exists()
    assert not output.with_suffix(".png.receipt.json").exists()


def test_image_bare_403_does_not_rotate_provider_credential(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "permission-or-policy-403.png"

    def fail_attempt(**_: object) -> tuple[bytes, dict]:
        raise image_script._ImageRequestError(
            "OpenRouter HTTP 403",
            status=403,
            retryable=False,
        )

    monkeypatch.setattr(image_script, "_try_one_attempt", fail_attempt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
        ],
    )

    assert image_script.main() == 1
    assert image_script._credential_failure_exit_code(403) is None
    assert not output.exists()


def test_image_http_error_keeps_only_allowlisted_policy_code(
    image_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://openrouter.ai/api/v1/chat?token=signed-secret#fragment"
    error = image_script.urllib.error.HTTPError(
        secret_url,
        403,
        "request_id=req-secret; provider prose",
        {},
        io.BytesIO(
            b'{"error":{"code":"not a policy failure",'
            b'"message":"request_id=req-secret; token=raw-secret"}}'
        ),
    )

    def fail_open(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise error

    monkeypatch.setattr(image_script, "_open_authenticated_request", fail_open)

    with pytest.raises(image_script._ImageRequestError) as caught:
        image_script.post_chat_completions(
            image_script.DEFAULT_BASE_URL,
            "sk-or-secret",
            {"model": image_script.DEFAULT_MODEL},
            30,
        )

    assert str(caught.value) == "OpenRouter HTTP 403"
    assert caught.value.provider_code is None
    assert caught.value.policy_rejected is False
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    serialized = str(caught.value)
    assert "signed-secret" not in serialized
    assert "req-secret" not in serialized
    assert "raw-secret" not in serialized
    assert "provider prose" not in serialized


@pytest.mark.parametrize(
    "value",
    [
        "https://signed.example/image",
        "sk-policy-secret",
        "req-privacy-private",
        "provider policy prose with spaces",
    ],
)
def test_image_policy_code_rejects_urls_secrets_ids_and_prose(
    image_script: ModuleType,
    value: str,
) -> None:
    assert image_script._safe_policy_code(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "InputImageSensitiveContentDetected.PrivacyInformation",
        "ContentPolicyViolation",
    ],
)
def test_image_policy_code_preserves_known_policy_identifiers(
    image_script: ModuleType,
    value: str,
) -> None:
    assert image_script._safe_policy_code(value) == value


@pytest.mark.parametrize(
    "destination",
    [
        "https://openrouter.ai/api/v1/redirected",
        "https://attacker.example/collect?token=signed-secret#fragment",
    ],
    ids=["same-origin", "cross-origin"],
)
def test_image_authenticated_redirect_handler_never_creates_second_request(
    image_script: ModuleType,
    destination: str,
) -> None:
    original = image_script.urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": "Bearer sk-or-must-not-forward"},
    )
    handler = image_script._NoRedirectHandler()

    redirected = handler.redirect_request(
        original,
        io.BytesIO(),
        302,
        "found",
        {"Location": destination},
        destination,
    )

    assert redirected is None
    assert original.get_header("Authorization") == "Bearer sk-or-must-not-forward"


def test_image_redirect_error_is_sanitized_without_opening_second_origin(
    image_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "sk-or-must-not-forward"
    redirect_url = "https://attacker.example/collect?token=signed-secret#fragment"
    opened: list[tuple[str, str | None]] = []

    def redirect_once(
        request: object,
        *,
        timeout: int,
    ) -> object:
        del timeout
        opened.append(
            (
                request.full_url,
                request.get_header("Authorization"),
            )
        )
        raise image_script.urllib.error.HTTPError(
            redirect_url,
            302,
            "redirect includes request_id=req-secret",
            {"Location": redirect_url},
            io.BytesIO(b'{"request_id":"req-secret","token":"raw-secret"}'),
        )

    monkeypatch.setattr(image_script, "_open_authenticated_request", redirect_once)

    with pytest.raises(image_script._ImageRequestError) as caught:
        image_script.post_chat_completions(
            "https://custom-image-provider.example/v1",
            key,
            {"model": image_script.DEFAULT_MODEL},
            30,
        )

    assert opened == [
        (
            "https://custom-image-provider.example/v1/chat/completions",
            f"Bearer {key}",
        )
    ]
    assert str(caught.value) == "OpenRouter HTTP 302"
    serialized = str(caught.value)
    assert "attacker.example" not in serialized
    assert "signed-secret" not in serialized
    assert "req-secret" not in serialized
    assert "raw-secret" not in serialized
    assert key not in serialized


def test_image_network_error_discards_untrusted_exception_chain(
    image_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://openrouter.ai/api/v1/chat?token=signed-secret#fragment"

    def fail_open(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise image_script.urllib.error.URLError(
            f"request_id=req-private url={secret_url} token=raw-secret"
        )

    monkeypatch.setattr(image_script, "_open_authenticated_request", fail_open)

    with pytest.raises(image_script._ImageRequestError) as caught:
        image_script.post_chat_completions(
            image_script.DEFAULT_BASE_URL,
            "sk-or-secret",
            {"model": image_script.DEFAULT_MODEL},
            30,
        )

    assert str(caught.value) == "OpenRouter network request failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_image_ambiguous_submit_failure_never_retries_or_uses_fallback(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "must-stay-old.png"
    output.write_bytes(b"old-output")
    post_calls = 0

    def accepted_but_response_lost(*args: object, **kwargs: object) -> object:
        nonlocal post_calls
        del args, kwargs
        post_calls += 1
        raise image_script.urllib.error.URLError(
            "request_id=req-private token=raw-secret"
        )

    def unexpected_sleep(_: float) -> None:
        raise AssertionError("an ambiguous paid POST must not sleep and retry")

    monkeypatch.setattr(image_script, "_open_authenticated_request", accepted_but_response_lost)
    monkeypatch.setattr(image_script.time, "sleep", unexpected_sleep)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--max-retries",
            "999",
            "--fallback-model",
            "google/gemini-3-pro-image-preview",
            "--fallback-model",
            "google/gemini-2.5-flash-image",
        ],
    )

    assert image_script.main() == 1
    captured = capsys.readouterr()
    assert post_calls == 1
    assert output.read_bytes() == b"old-output"
    assert "non-retryable provider response; stopping" in captured.err
    assert "sleeping" not in captured.err
    assert "req-private" not in captured.err
    assert "raw-secret" not in captured.err


def test_image_json_response_has_declared_and_cumulative_byte_limits(
    image_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_script, "MAX_OPENROUTER_RESPONSE_BYTES", 16)

    class DeclaredOversize(io.BytesIO):
        headers = {"Content-Length": "17"}

        def read(self, size: int = -1) -> bytes:
            del size
            raise AssertionError("declared oversized response must not be read")

    responses = iter((DeclaredOversize(), io.BytesIO(b"private-response!!")))
    monkeypatch.setattr(
        image_script,
        "_open_authenticated_request",
        lambda *args, **kwargs: next(responses),
    )

    for _ in range(2):
        with pytest.raises(
            image_script._ImageRequestError,
            match="OpenRouter response exceeds size limit",
        ) as caught:
            image_script.post_chat_completions(
                image_script.DEFAULT_BASE_URL,
                "sk-or-secret",
                {"model": image_script.DEFAULT_MODEL},
                30,
            )
        assert caught.value.retryable is False
        assert "private-response" not in str(caught.value)


def test_image_decoded_output_has_a_byte_limit(
    image_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_script, "MAX_DECODED_IMAGE_BYTES", 3)
    encoded = image_script.base64.b64encode(b"four").decode("ascii")

    with pytest.raises(
        image_script._ImageRequestError,
        match="OpenRouter image output exceeds size limit",
    ) as caught:
        image_script.decode_data_url(f"data:image/png;base64,{encoded}")

    assert caught.value.retryable is False
    assert encoded not in str(caught.value)


def test_image_decode_verifies_and_normalizes_supported_provider_image(
    image_script: ModuleType,
) -> None:
    jpeg = _valid_image_bytes(image_format="JPEG")
    encoded = image_script.base64.b64encode(jpeg).decode("ascii")

    normalized = image_script.decode_data_url(f"data:image/jpeg;base64,{encoded}")

    assert normalized.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.format == "PNG"
        assert image.size == (2, 2)
        image.verify()


@pytest.mark.parametrize(
    ("mime", "payload"),
    [
        ("image/png", b"not-an-image"),
        ("image/png", _valid_image_bytes()[:-8]),
        ("image/jpeg", _valid_image_bytes()),
    ],
)
def test_image_decode_rejects_corrupt_truncated_or_mime_mismatched_bytes(
    image_script: ModuleType,
    mime: str,
    payload: bytes,
) -> None:
    encoded = image_script.base64.b64encode(payload).decode("ascii")

    with pytest.raises(
        image_script._ImageRequestError,
        match="invalid image data|MIME does not match",
    ):
        image_script.decode_data_url(f"data:{mime};base64,{encoded}")


def test_image_decode_rejects_unsupported_declared_mime(
    image_script: ModuleType,
) -> None:
    encoded = image_script.base64.b64encode(_valid_image_bytes()).decode("ascii")

    with pytest.raises(ValueError, match="non-base64 image URL"):
        image_script.decode_data_url(f"data:text/plain;base64,{encoded}")


def test_image_decode_rejects_oversized_dimensions_before_receipt(
    image_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image_script, "MAX_IMAGE_DIMENSION", 1)
    encoded = image_script.base64.b64encode(_valid_image_bytes()).decode("ascii")

    with pytest.raises(
        image_script._ImageRequestError,
        match="dimensions are invalid",
    ):
        image_script.decode_data_url(f"data:image/png;base64,{encoded}")


def test_image_explicit_safe_retry_budget_is_capped(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    attempts = 0

    def safe_pre_submit_failure(**kwargs: object) -> tuple[bytes, dict]:
        nonlocal attempts
        del kwargs
        attempts += 1
        raise image_script._ImageRequestError(
            "safe pre-submit failure",
            retryable=True,
        )

    monkeypatch.setattr(image_script, "_try_one_attempt", safe_pre_submit_failure)
    monkeypatch.setattr(image_script.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(isolated_runtime / "never-created.png"),
            "--max-retries",
            "999",
        ],
    )

    assert image_script.main() == 1
    assert attempts == 6


def test_image_invalid_json_discards_parser_input_and_exception_chain(
    image_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        b'{"signed_url":"https://signed.example/image?token=raw-secret",'
        b'"request_id":"req-private"'
    )

    def return_invalid_json(*args: object, **kwargs: object) -> io.BytesIO:
        del args, kwargs
        return io.BytesIO(raw)

    monkeypatch.setattr(image_script, "_open_authenticated_request", return_invalid_json)

    with pytest.raises(image_script._ImageRequestError) as caught:
        image_script.post_chat_completions(
            image_script.DEFAULT_BASE_URL,
            "sk-or-secret",
            {"model": image_script.DEFAULT_MODEL},
            30,
        )

    assert str(caught.value) == "OpenRouter returned invalid JSON"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_image_placeholder_receipt_cannot_masquerade_as_api_success(
    image_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-placeholder-test")
    output = isolated_runtime / "placeholder.png"

    def fail_attempt(**_: object) -> tuple[bytes, dict]:
        raise RuntimeError("synthetic provider failure")

    def write_placeholder(path: Path, prompt: str, aspect_ratio: str) -> None:
        del prompt, aspect_ratio
        path.write_bytes(b"synthetic-placeholder")

    monkeypatch.setattr(image_script, "_try_one_attempt", fail_attempt)
    monkeypatch.setattr(image_script, "_write_placeholder_png", write_placeholder)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_image.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--placeholder-on-fail",
            "yes",
        ],
    )

    assert image_script.main() == 0
    stdout = capsys.readouterr().out
    receipt = json.loads(
        output.with_suffix(".png.receipt.json").read_text(encoding="utf-8")
    )

    assert receipt["status"] == "placeholder"
    assert receipt["provider"] == "local"
    assert receipt["placeholder"] is True
    assert receipt["reason"] == "all_model_attempts_failed"
    assert "request_id" not in receipt
    assert stdout.splitlines()[0] == str(output)
    assert '"status": "placeholder"' in stdout
    assert '"status": "generated"' not in stdout


def test_image_response_without_provider_id_is_not_verified(
    image_script: ModuleType,
) -> None:
    receipt = image_script._generated_receipt(
        {"usage": {"cost": 0.05}},
        model=image_script.DEFAULT_MODEL,
    )

    assert receipt["status"] == "generated_unverified"
    assert receipt["reason"] == "provider_response_missing_request_id"
    assert "request_id" not in receipt


def test_seedance_openrouter_payload_explicitly_requests_audio(
    video_script: ModuleType,
) -> None:
    args = video_script.Args(
        prompt="synthetic prompt",
        model="bytedance/seedance-2.0",
        aspect_ratio="9:16",
        duration=5,
        resolution="720p",
        input_image="",
        input_references=[],
    )

    payload = video_script._build_openrouter_payload(args)

    assert payload["generate_audio"] is True


def test_seedance_three_seconds_uses_supported_provider_duration(
    video_script: ModuleType,
) -> None:
    provider = video_script.PROVIDERS["openrouter"]

    assert video_script._provider_duration(
        provider,
        "bytedance/seedance-2.0",
        3,
    ) == 4
    assert video_script._provider_duration(
        provider,
        "bytedance/seedance-2.0-fast",
        3,
    ) == 4
    assert video_script._provider_duration(
        provider,
        "bytedance/seedance-2.0",
        4,
    ) == 4


def test_seedance_plain_http_400_is_not_retryable(video_script: ModuleType) -> None:
    assert video_script._http_error_is_retryable("HTTP 400: bad duration") is False
    assert video_script._http_error_is_retryable("HTTP 429: slow down") is True
    assert video_script._http_error_is_retryable("HTTP 503: unavailable") is True


def test_seedance_http_error_discards_raw_body_and_signed_url(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://openrouter.ai/api/v1/videos?token=signed-secret#fragment"
    error = video_script.urllib.error.HTTPError(
        secret_url,
        403,
        "forbidden",
        {},
        io.BytesIO(b'{"request_id":"req-secret","token":"raw-secret"}'),
    )

    def fail_request(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise error

    monkeypatch.setattr(video_script, "_open_authenticated_request", fail_request)

    with pytest.raises(video_script._RequestError) as caught:
        video_script._http_request(
            "GET",
            secret_url,
            "sk-or-secret",
            trusted_base_url="https://openrouter.ai/api/v1",
        )

    assert str(caught.value) == "HTTP 403"
    assert caught.value.retryable is False
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "signed-secret" not in str(caught.value)
    assert "req-secret" not in str(caught.value)
    assert "raw-secret" not in str(caught.value)


def test_seedance_http_error_preserves_only_allowlisted_policy_code(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://openrouter.ai/api/v1/videos?token=signed-secret#fragment"
    error = video_script.urllib.error.HTTPError(
        secret_url,
        400,
        "bad request",
        {},
        io.BytesIO(
            b'{"error":{"code":"ContentPolicyViolation",'
            b'"message":"request_id=req-secret; token=raw-secret"}}'
        ),
    )

    def fail_request(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise error

    monkeypatch.setattr(video_script, "_open_authenticated_request", fail_request)

    with pytest.raises(video_script._RequestError) as caught:
        video_script._http_request(
            "POST",
            secret_url,
            "sk-or-secret",
            trusted_base_url="https://openrouter.ai/api/v1",
            body={"prompt": "synthetic"},
        )

    assert str(caught.value) == "HTTP 400"
    assert caught.value.provider_code == "ContentPolicyViolation"
    assert caught.value.retryable is False
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "signed-secret" not in str(caught.value)
    assert "req-secret" not in str(caught.value)
    assert "raw-secret" not in str(caught.value)


def test_seedance_network_error_discards_untrusted_exception_chain(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://openrouter.ai/api/v1/videos?token=signed-secret#fragment"

    def fail_request(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise video_script.urllib.error.URLError(
            f"request_id=req-private url={secret_url} token=raw-secret"
        )

    monkeypatch.setattr(video_script, "_open_authenticated_request", fail_request)

    with pytest.raises(video_script._RequestError) as caught:
        video_script._http_request(
            "GET",
            "https://openrouter.ai/api/v1/videos/job-safe",
            "sk-or-secret",
            trusted_base_url="https://openrouter.ai/api/v1",
        )

    assert str(caught.value) == "network request failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_seedance_invalid_json_discards_parser_input_and_exception_chain(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = (
        b'{"signed_url":"https://signed.example/video?token=raw-secret",'
        b'"request_id":"req-private"'
    )

    def return_invalid_json(*args: object, **kwargs: object) -> io.BytesIO:
        del args, kwargs
        return io.BytesIO(raw)

    monkeypatch.setattr(video_script, "_open_authenticated_request", return_invalid_json)

    with pytest.raises(video_script._RequestError) as caught:
        video_script._http_request(
            "GET",
            "https://openrouter.ai/api/v1/videos/job-safe",
            "sk-or-secret",
            trusted_base_url="https://openrouter.ai/api/v1",
        )

    assert str(caught.value) == "provider returned invalid JSON"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "signed.example" not in str(caught.value)
    assert "raw-secret" not in str(caught.value)
    assert "req-private" not in str(caught.value)


def test_seedance_submit_and_poll_json_have_cumulative_byte_limit(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_script, "MAX_PROVIDER_JSON_RESPONSE_BYTES", 16)
    monkeypatch.setattr(
        video_script,
        "_open_authenticated_request",
        lambda *args, **kwargs: io.BytesIO(b"private-response!!"),
    )

    for method in ("POST", "GET"):
        with pytest.raises(
            video_script._RequestError,
            match="provider JSON response exceeds size limit",
        ) as caught:
            video_script._http_request(
                method,
                "https://openrouter.ai/api/v1/videos",
                "sk-or-secret",
                trusted_base_url="https://openrouter.ai/api/v1",
                body={"prompt": "synthetic"} if method == "POST" else None,
            )
        assert caught.value.retryable is False
        assert "private-response" not in str(caught.value)


@pytest.mark.parametrize(
    "provider_code",
    [
        "sk-or-secret",
        "sk-policy-secret",
        "req-private",
        "req-privacy-private",
        "job-private",
        "trace-private",
        "generic_provider_error",
        "https://signed.example/private",
        "provider prose",
    ],
)
def test_seedance_provider_problem_does_not_expose_generic_secret_or_identifier_codes(
    video_script: ModuleType,
    provider_code: str,
) -> None:
    problem = video_script._provider_problem(
        {
            "error": {
                "status": 400,
                "code": provider_code,
                "message": "request_id=req-private; token=raw-secret",
            }
        }
    )

    assert problem is not None
    assert problem.code is None
    assert problem.summary("submit") == "submit rejected by provider (HTTP 400)"


@pytest.mark.parametrize(
    "provider_code",
    [
        "InputImageSensitiveContentDetected.PrivacyInformation",
        "ContentPolicyViolation",
    ],
)
def test_seedance_provider_problem_preserves_known_policy_codes(
    video_script: ModuleType,
    provider_code: str,
) -> None:
    problem = video_script._provider_problem(
        {"error": {"status": 400, "code": provider_code}}
    )

    assert problem is not None
    assert problem.code == provider_code
    assert f"code={provider_code}" in problem.summary("submit")


def test_seedance_job_id_rejects_key_like_values(video_script: ModuleType) -> None:
    assert video_script._safe_job_id("sk-or-must-not-persist") is None
    assert video_script._safe_job_id("Bearer-secret") is None
    assert video_script._safe_job_id("job-provider-456") == "job-provider-456"


@pytest.mark.parametrize(
    "destination",
    [
        "https://openrouter.ai/api/v1/redirected",
        "https://attacker.example/collect?token=signed-secret#fragment",
    ],
    ids=["same-origin", "cross-origin"],
)
def test_seedance_authenticated_redirect_handler_never_creates_second_request(
    video_script: ModuleType,
    destination: str,
) -> None:
    original = video_script.urllib.request.Request(
        "https://openrouter.ai/api/v1/videos",
        headers={"Authorization": "Bearer sk-or-must-not-forward"},
    )
    handler = video_script._NoRedirectHandler()

    redirected = handler.redirect_request(
        original,
        io.BytesIO(),
        302,
        "found",
        {"Location": destination},
        destination,
    )

    assert redirected is None
    assert original.get_header("Authorization") == "Bearer sk-or-must-not-forward"


def test_seedance_authenticated_request_accepts_only_trusted_origin(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[tuple[str, str | None]] = []

    def fake_open(
        request: object,
        *,
        timeout: int,
    ) -> io.BytesIO:
        del timeout
        opened.append(
            (
                request.full_url,
                request.get_header("Authorization"),
            )
        )
        return io.BytesIO(b'{"status":"processing"}')

    monkeypatch.setattr(video_script, "_open_authenticated_request", fake_open)
    trusted_base = "https://openrouter.ai/api/v1"

    response = video_script._http_request(
        "GET",
        "https://openrouter.ai:443/api/v1/videos/job-safe",
        "sk-or-safe-test",
        trusted_base_url=trusted_base,
    )

    assert response == {"status": "processing"}
    assert opened == [
        (
            "https://openrouter.ai:443/api/v1/videos/job-safe",
            "Bearer sk-or-safe-test",
        )
    ]

    with pytest.raises(
        video_script._RequestError,
        match="outside trusted API origin",
    ):
        video_script._http_request(
            "GET",
            "https://attacker.example/poll?token=provider-secret#fragment",
            "sk-or-must-not-leak",
            trusted_base_url=trusted_base,
        )
    assert len(opened) == 1


def test_seedance_malicious_polling_url_falls_back_to_trusted_job_path(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opened: list[tuple[str, str | None]] = []

    def fake_open(
        request: object,
        *,
        timeout: int,
    ) -> io.BytesIO:
        del timeout
        opened.append(
            (
                request.full_url,
                request.get_header("Authorization"),
            )
        )
        return io.BytesIO(b'{"status":"completed"}')

    monkeypatch.setattr(video_script, "_open_authenticated_request", fake_open)
    malicious = "https://attacker.example/poll?token=provider-secret#fragment"

    result = video_script._poll(
        video_script.PROVIDERS["openrouter"],
        "https://openrouter.ai/api/v1",
        "sk-or-must-stay-trusted",
        "job-trusted-123",
        malicious,
        timeout_total=60,
        poll_interval=0,
    )
    captured = capsys.readouterr()

    assert result == {"status": "completed"}
    assert opened == [
        (
            "https://openrouter.ai/api/v1/videos/job-trusted-123",
            "Bearer sk-or-must-stay-trusted",
        )
    ]
    assert "attacker.example" not in captured.err
    assert "provider-secret" not in captured.err
    assert "#fragment" not in captured.err


class _AsyncMediaResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
        fail_if_read: bool = False,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self._fail_if_read = fail_if_read

    async def __aenter__(self) -> _AsyncMediaResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def aiter_bytes(self, _chunk_size: int):
        if self._fail_if_read:
            raise AssertionError("declared oversized media must not be read")
        for chunk in self._chunks:
            yield chunk


def _install_fake_media_client(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_AsyncMediaResponse],
) -> tuple[list[dict[str, object]], list[tuple[str, str, dict[str, str]]]]:
    client_kwargs: list[dict[str, object]] = []
    requests: list[tuple[str, str, dict[str, str]]] = []
    pending = list(responses)

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            client_kwargs.append(dict(kwargs))

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def stream(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str],
        ) -> _AsyncMediaResponse:
            requests.append((method, url, dict(headers)))
            return pending.pop(0)

    monkeypatch.setattr(video_script.httpx, "AsyncClient", FakeAsyncClient)
    return client_kwargs, requests


def test_seedance_external_download_is_dns_pinned_https_and_anonymous(
    video_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "download.mp4"
    resolver_calls: list[str] = []
    pin_calls: list[tuple[str, list[str]]] = []
    pinned_transport = object()

    def resolve(url: str) -> list[str]:
        resolver_calls.append(url)
        return ["93.184.216.34"]

    def pin(url: str, addresses: list[str], **_kwargs: object) -> object:
        pin_calls.append((url, addresses))
        return pinned_transport

    monkeypatch.setattr(video_script, "validate_http_url_for_fetch", resolve)
    monkeypatch.setattr(video_script, "_pinned_transport", pin)
    client_kwargs, requests = _install_fake_media_client(
        video_script,
        monkeypatch,
        [_AsyncMediaResponse(chunks=[b"synthetic-provider-media"])],
    )
    video_script._download_url_to_path(
        "https://openrouter.ai/download/video.mp4?signature=signed-secret#fragment",
        "sk-or-must-never-be-sent",
        output,
        timeout=30,
    )

    assert output.read_bytes() == b"synthetic-provider-media"
    safe_url = "https://openrouter.ai/download/video.mp4?signature=signed-secret"
    assert resolver_calls == [safe_url]
    assert pin_calls == [(safe_url, ["93.184.216.34"])]
    assert requests == [("GET", safe_url, {"Accept-Encoding": "identity"})]
    assert client_kwargs[0]["transport"] is pinned_transport
    assert client_kwargs[0]["trust_env"] is False
    assert "sk-or-must-never-be-sent" not in repr(client_kwargs + requests)


def test_seedance_download_uses_environment_proxy_only_when_explicitly_enabled(
    video_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        video_script,
        "validate_http_url_for_fetch",
        lambda _url: ["93.184.216.34"],
    )
    proxy_calls: list[str] = []
    monkeypatch.setattr(
        video_script,
        "environment_proxy_url",
        lambda url: proxy_calls.append(url) or "http://proxy.example:8080",
    )
    pin_kwargs: list[dict[str, object]] = []

    def pin(_url: str, _addresses: list[str], **kwargs: object) -> object:
        pin_kwargs.append(kwargs)
        return object()

    monkeypatch.setattr(video_script, "_pinned_transport", pin)
    _install_fake_media_client(
        video_script,
        monkeypatch,
        [
            _AsyncMediaResponse(chunks=[b"direct"]),
            _AsyncMediaResponse(chunks=[b"proxied"]),
        ],
    )

    monkeypatch.setattr(video_script, "_trust_env", lambda: False)
    video_script._download_url_to_path(
        "https://media.example/direct.mp4",
        "unused",
        tmp_path / "direct.mp4",
        timeout=30,
    )
    monkeypatch.setattr(video_script, "_trust_env", lambda: True)
    video_script._download_url_to_path(
        "https://media.example/proxied.mp4",
        "unused",
        tmp_path / "proxied.mp4",
        timeout=30,
    )

    assert proxy_calls == ["https://media.example/proxied.mp4"]
    assert pin_kwargs == [{}, {"proxy": "http://proxy.example:8080"}]


def test_seedance_download_revalidates_and_pins_every_redirect(
    video_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "redirected.mp4"
    resolved: list[str] = []
    pinned: list[tuple[str, list[str]]] = []

    def resolve(url: str) -> list[str]:
        resolved.append(url)
        return ["93.184.216.34"] if "media.example" in url else ["203.0.113.8"]

    def pin(url: str, addresses: list[str], **_kwargs: object) -> object:
        pinned.append((url, addresses))
        return object()

    monkeypatch.setattr(video_script, "validate_http_url_for_fetch", resolve)
    monkeypatch.setattr(video_script, "_pinned_transport", pin)
    _client_kwargs, requests = _install_fake_media_client(
        video_script,
        monkeypatch,
        [
            _AsyncMediaResponse(
                status_code=302,
                headers={"location": "https://cdn.example/final.mp4"},
            ),
            _AsyncMediaResponse(chunks=[b"video"]),
        ],
    )

    video_script._download_url_to_path(
        "https://media.example/start.mp4",
        "sk-or-unused",
        output,
        timeout=30,
    )

    assert resolved == [
        "https://media.example/start.mp4",
        "https://cdn.example/final.mp4",
    ]
    assert pinned == [
        ("https://media.example/start.mp4", ["93.184.216.34"]),
        ("https://cdn.example/final.mp4", ["203.0.113.8"]),
    ]
    assert [request[1] for request in requests] == resolved
    assert output.read_bytes() == b"video"


def test_seedance_download_rejects_oversized_content_length_before_reading(
    video_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "oversized.mp4"
    destination.write_bytes(b"stale-private-candidate")
    monkeypatch.setattr(video_script, "MAX_VIDEO_DOWNLOAD_BYTES", 8)
    monkeypatch.setattr(
        video_script,
        "validate_http_url_for_fetch",
        lambda _url: ["93.184.216.34"],
    )
    monkeypatch.setattr(
        video_script,
        "_pinned_transport",
        lambda *_args, **_kwargs: object(),
    )
    _install_fake_media_client(
        video_script,
        monkeypatch,
        [
            _AsyncMediaResponse(
                headers={"Content-Length": "9"},
                fail_if_read=True,
            )
        ],
    )

    with pytest.raises(
        video_script._RequestError,
        match="provider media exceeds download size limit",
    ) as caught:
        video_script._download_url_to_path(
            "https://media.example/video.mp4?token=private#fragment",
            "sk-or-must-never-be-sent",
            destination,
            timeout=30,
        )

    assert not destination.exists()
    assert "private" not in str(caught.value)


def test_seedance_download_enforces_cumulative_limit_and_deletes_partial_candidate(
    video_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "partial.mp4"
    monkeypatch.setattr(video_script, "MAX_VIDEO_DOWNLOAD_BYTES", 8)
    monkeypatch.setattr(video_script, "_VIDEO_READ_CHUNK_BYTES", 4)
    monkeypatch.setattr(
        video_script,
        "validate_http_url_for_fetch",
        lambda _url: ["93.184.216.34"],
    )
    monkeypatch.setattr(
        video_script,
        "_pinned_transport",
        lambda *_args, **_kwargs: object(),
    )
    _install_fake_media_client(
        video_script,
        monkeypatch,
        [_AsyncMediaResponse(chunks=[b"1234", b"5678", b"9"])],
    )

    with pytest.raises(
        video_script._RequestError,
        match="provider media exceeds download size limit",
    ) as caught:
        video_script._download_url_to_path(
            "https://media.example/video.mp4?token=private#fragment",
            "sk-or-must-never-be-sent",
            destination,
            timeout=30,
        )

    assert not destination.exists()
    assert "private" not in str(caught.value)


def test_seedance_download_http_error_discards_signed_url_body_and_exception_chain(
    video_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "download.mp4"
    signed_url = "https://media.example/video.mp4?token=signed-secret#fragment"
    monkeypatch.setattr(
        video_script,
        "validate_http_url_for_fetch",
        lambda _url: ["93.184.216.34"],
    )
    monkeypatch.setattr(
        video_script,
        "_pinned_transport",
        lambda *_args, **_kwargs: object(),
    )
    _install_fake_media_client(
        video_script,
        monkeypatch,
        [
            _AsyncMediaResponse(
                status_code=403,
                headers={"X-Provider-Token": "raw-secret"},
                chunks=[b'{"request_id":"req-private","token":"raw-secret"}'],
                fail_if_read=True,
            )
        ],
    )

    with pytest.raises(video_script._RequestError) as caught:
        video_script._download_url_to_path(
            signed_url,
            "sk-or-must-never-be-sent",
            output,
            timeout=30,
        )

    assert str(caught.value) == "HTTP 403"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    serialized = str(caught.value)
    assert "signed-secret" not in serialized
    assert "req-private" not in serialized
    assert "raw-secret" not in serialized


@pytest.mark.parametrize(
    "url",
    [
        "http://93.184.216.34/video.mp4",
        "https://user:password@93.184.216.34/video.mp4",
        "https://127.0.0.1/video.mp4",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/video.mp4",
    ],
)
def test_seedance_download_rejects_insecure_or_nonpublic_targets(
    video_script: ModuleType,
    url: str,
) -> None:
    with pytest.raises(video_script._RequestError):
        video_script._validate_download_url(url)


def _verified_media(
    duration_s: float = 4.0,
) -> dict[str, int | float | bool | str]:
    return {
        "duration_s": duration_s,
        "width": 720,
        "height": 1280,
        "has_audio": True,
        "video_codec": "h264",
    }


def test_seedance_attempt_downloads_candidate_and_builds_safe_receipt(
    video_script: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = video_script.PROVIDERS["openrouter"]
    payload = {
        "model": "bytedance/seedance-2.0",
        "prompt": "synthetic",
    }

    monkeypatch.setattr(
        video_script,
        "_http_request",
        lambda *args, **kwargs: {
            "id": "job-provider-456",
            "polling_url": "https://openrouter.ai/api/v1/videos/job-provider-456",
        },
    )
    monkeypatch.setattr(
        video_script,
        "_poll",
        lambda *args, **kwargs: {
            "status": "completed",
            "generation_id": "generation-789",
            "unsigned_urls": ["https://openrouter.ai/api/v1/videos/result.mp4"],
            "usage": {"cost": 0.76, "raw_provider_debug": "not-public"},
        },
    )
    candidate = tmp_path / "provider-candidate.mp4"

    def fake_download(
        url: str,
        api_key: str,
        destination: Path,
        timeout: int,
    ) -> None:
        del url, api_key, timeout
        destination.write_bytes(b"synthetic-mp4")

    monkeypatch.setattr(video_script, "_download_url_to_path", fake_download)
    monkeypatch.setattr(
        video_script,
        "_probe_video",
        lambda *args, **kwargs: _verified_media(),
    )

    validation, receipt = video_script._run_attempt(
        provider=provider,
        base_url=provider.default_base_url,
        submit_url=provider.default_base_url + provider.submit_path,
        api_key="sk-or-never-persist",
        payload=payload,
        timeout_total=60,
        poll_interval=1,
        download_path=candidate,
        expected_duration_s=4,
    )

    assert candidate.read_bytes() == b"synthetic-mp4"
    assert validation == _verified_media()
    assert receipt == {
        "status": "generated",
        "provider": "openrouter",
        "model": "bytedance/seedance-2.0",
        "job_id": "job-provider-456",
        "fallback": False,
    }


def test_video_success_persists_sanitized_job_receipt(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "sk-or-video-secret-must-not-appear"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    output = isolated_runtime / "generated.mp4"
    output.write_bytes(b"previous-output")
    original_replace = video_script.os.replace
    replacements: list[tuple[Path, Path]] = []

    def fake_run_attempt(**kwargs: object) -> tuple[dict, dict]:
        assert kwargs["max_transient_retries"] == 5
        destination = kwargs["download_path"]
        assert isinstance(destination, Path)
        destination.write_bytes(b"synthetic-mp4")
        return (
            _verified_media(5.0),
            {
                "status": "generated",
                "provider": "openrouter",
                "model": "bytedance/seedance-2.0",
                "job_id": "job-video-123",
                "fallback": False,
                "usage": {"cost": 0.76},
                "signed_url": "https://cdn.example/video?token=do-not-write#secret",
            },
        )

    def record_replace(source: object, destination: object) -> None:
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr(video_script, "_run_attempt", fake_run_attempt)
    monkeypatch.setattr(video_script.os, "replace", record_replace)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "5",
            "--max-retries",
            "999",
        ],
    )

    assert video_script.main() == 0
    stdout = capsys.readouterr().out
    receipt_path = output.with_suffix(".mp4.receipt.json")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert output.read_bytes() == b"synthetic-mp4"
    assert receipt["status"] == "generated"
    assert receipt["provider"] == "openrouter"
    assert receipt["model"] == "bytedance/seedance-2.0"
    assert receipt["job_id"] == "job-video-123"
    assert receipt["fallback"] is False
    assert receipt["validation"]["expected_provider_duration_s"] == 5
    assert receipt["validation"]["expected_final_duration_s"] == 5
    assert receipt["validation"]["final_media"] == _verified_media(5.0)
    assert "usage" not in receipt
    assert "signed_url" not in receipt
    assert any(destination == output for _, destination in replacements)
    assert stdout.splitlines()[0] == str(output)
    assert "VIDEO_GENERATION_RECEIPT:" in stdout
    assert secret not in stdout
    assert secret not in receipt_path.read_text(encoding="utf-8")


def test_three_second_video_records_real_provider_duration_and_trim(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "three-seconds.mp4"
    seen: dict[str, object] = {}

    def fake_run_attempt(**kwargs: object) -> tuple[dict, dict]:
        seen["payload"] = kwargs["payload"]
        destination = kwargs["download_path"]
        assert isinstance(destination, Path)
        destination.write_bytes(b"provider-four-seconds")
        return (
            _verified_media(4.0),
            {
                "status": "generated",
                "provider": "openrouter",
                "model": "bytedance/seedance-2.0",
                "job_id": "job-real-provider",
                "fallback": False,
            },
        )

    def fake_trim(
        source_path: Path,
        out_path: Path,
        *,
        duration_s: int,
    ) -> dict:
        seen["trim"] = (source_path.read_bytes(), duration_s)
        out_path.write_bytes(b"trimmed-three-seconds")
        return _verified_media(3.0)

    monkeypatch.setattr(video_script, "_run_attempt", fake_run_attempt)
    monkeypatch.setattr(video_script, "_write_trimmed_video", fake_trim)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "3",
        ],
    )

    assert video_script.main() == 0
    assert seen["payload"]["duration"] == 4
    assert seen["trim"] == (b"provider-four-seconds", 3)
    receipt = json.loads(
        output.with_suffix(".mp4.receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["job_id"] == "job-real-provider"
    validation = receipt["validation"]
    assert validation["expected_final_duration_s"] == 3
    assert validation["expected_provider_duration_s"] == 4
    assert validation["trimmed"] is True
    assert validation["provider_media"]["duration_s"] == 4.0
    assert validation["final_media"]["duration_s"] == 3.0


@pytest.mark.parametrize(
    "payload",
    [
        b"<!doctype html><title>provider error</title>",
        b"\x00\x00\x00\x18ftypmp42\x00\x00",
    ],
    ids=["html", "truncated-mp4"],
)
def test_seedance_provider_bytes_must_be_probeable_video(
    video_script: ModuleType,
    tmp_path: Path,
    payload: bytes,
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe is required for this runtime contract")
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(payload)

    with pytest.raises(
        video_script._MediaValidationError,
        match="not a readable video",
    ):
        video_script._probe_video(
            candidate,
            expected_duration_s=4,
            duration_tolerance_s=1,
        )


def test_seedance_ffprobe_accepts_real_four_second_media_and_revalidates_trim(
    video_script: ModuleType,
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are required for this runtime contract")
    candidate = tmp_path / "four-seconds.mp4"
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=160x96:r=12:d=4",
        "-an",
        "-c:v",
        "mpeg4",
        "-q:v",
        "5",
        str(candidate),
    ]
    proc = subprocess.run(command, capture_output=True, check=False, timeout=30)
    if proc.returncode != 0:
        pytest.skip("local ffmpeg cannot create the synthetic MP4 fixture")

    metadata = video_script._probe_video(
        candidate,
        expected_duration_s=4,
        duration_tolerance_s=0.25,
    )

    assert metadata["duration_s"] == pytest.approx(4.0, abs=0.1)
    assert metadata["width"] == 160
    assert metadata["height"] == 96
    assert metadata["has_audio"] is False

    trimmed = tmp_path / "three-seconds.mp4"
    trimmed_metadata = video_script._write_trimmed_video(
        candidate,
        trimmed,
        duration_s=3,
    )
    assert trimmed_metadata["duration_s"] == pytest.approx(3.0, abs=0.15)
    assert trimmed_metadata["width"] == 160
    assert trimmed_metadata["height"] == 96


def test_seedance_poll_401_stops_without_resubmitting(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "must-stay-old.mp4"
    output.write_bytes(b"old-output")
    calls: list[str] = []

    def fake_request(method: str, *args: object, **kwargs: object) -> dict:
        del args, kwargs
        calls.append(method)
        if method == "POST":
            return {
                "id": "job-auth-401",
                "polling_url": (
                    "https://openrouter.ai/api/v1/videos/job-auth-401"
                    "?token=signed-secret#fragment"
                ),
            }
        raise video_script._RequestError(
            "HTTP 401",
            status=401,
            retryable=False,
        )

    monkeypatch.setattr(video_script, "_http_request", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "4",
            "--max-retries",
            "2",
        ],
    )

    assert video_script.main() == video_script.PROVIDER_AUTH_INVALID_EXIT_CODE
    captured = capsys.readouterr()
    assert calls == ["POST", "GET"]
    assert output.read_bytes() == b"old-output"
    assert "HTTP 401" in captured.err
    assert "signed-secret" not in captured.err
    assert "#fragment" not in captured.err


@pytest.mark.parametrize(
    ("status", "exit_name"),
    [
        (401, "PROVIDER_AUTH_INVALID_EXIT_CODE"),
        (402, "PROVIDER_INSUFFICIENT_CREDITS_EXIT_CODE"),
        (429, "PROVIDER_RATE_LIMITED_EXIT_CODE"),
    ],
)
def test_seedance_credential_failure_uses_reserved_parent_exit(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    exit_name: str,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / f"credential-{status}.mp4"

    def fail_attempt(**_: object) -> tuple[dict, dict]:
        raise video_script._AttemptError(
            f"HTTP {status}",
            retryable=False,
            provider_status=status,
        )

    monkeypatch.setattr(video_script, "_run_attempt", fail_attempt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "4",
        ],
    )

    assert video_script.main() == getattr(video_script, exit_name)
    assert not output.exists()


def test_seedance_bare_403_does_not_rotate_provider_credential(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "permission-or-policy-403.mp4"

    def fail_attempt(**_: object) -> tuple[dict, dict]:
        raise video_script._AttemptError(
            "HTTP 403",
            retryable=False,
            provider_status=403,
        )

    monkeypatch.setattr(video_script, "_run_attempt", fail_attempt)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "4",
        ],
    )

    assert video_script.main() == 1
    assert video_script._credential_failure_exit_code(403) is None
    assert not output.exists()


def test_seedance_ambiguous_submit_failure_never_creates_a_second_paid_job(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "must-stay-old.mp4"
    output.write_bytes(b"old-output")
    submit_calls = 0

    def accepted_but_response_lost(*args: object, **kwargs: object) -> dict:
        nonlocal submit_calls
        del args, kwargs
        submit_calls += 1
        raise video_script._RequestError(
            "network request failed",
            retryable=True,
        )

    monkeypatch.setattr(video_script, "_http_request", accepted_but_response_lost)
    monkeypatch.setattr(video_script.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "4",
            "--max-retries",
            "5",
        ],
    )

    assert video_script.main() == 1
    captured = capsys.readouterr()
    assert submit_calls == 1
    assert output.read_bytes() == b"old-output"
    assert "non-retryable provider response; stopping" in captured.err
    assert "retrying submit" not in captured.err


def test_seedance_poll_429_retries_the_same_job(
    video_script: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_request(method: str, url: str, *args: object, **kwargs: object) -> dict:
        del args, kwargs
        assert method == "GET"
        calls.append(url)
        if len(calls) == 1:
            raise video_script._RequestError(
                "HTTP 429",
                status=429,
                retryable=True,
            )
        return {"status": "completed"}

    monkeypatch.setattr(video_script, "_http_request", fake_request)
    monkeypatch.setattr(video_script.time, "sleep", lambda _: None)
    polling_url = "https://openrouter.ai/api/v1/videos/job-rate-limited"

    result = video_script._poll(
        video_script.PROVIDERS["openrouter"],
        "https://openrouter.ai/api/v1",
        "sk-or-test-only",
        "job-rate-limited",
        polling_url,
        timeout_total=60,
        poll_interval=0,
        max_transient_retries=1,
    )

    assert result == {"status": "completed"}
    assert calls == [polling_url, polling_url]


def test_seedance_completed_job_error_envelope_never_resubmits(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "must-stay-old.mp4"
    output.write_bytes(b"old-output")
    submit_calls = 0

    def fake_submit(*args: object, **kwargs: object) -> dict:
        nonlocal submit_calls
        del args, kwargs
        submit_calls += 1
        return {"id": "job-already-paid"}

    monkeypatch.setattr(video_script, "_http_request", fake_submit)
    monkeypatch.setattr(
        video_script,
        "_poll",
        lambda *args, **kwargs: {
            "status": "completed",
            "error": {"status": 503, "message": "private provider diagnostics"},
        },
    )
    monkeypatch.setattr(video_script.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "4",
            "--max-retries",
            "5",
        ],
    )

    assert video_script.main() == 1
    assert submit_calls == 1
    assert output.read_bytes() == b"old-output"


def test_seedance_terminal_policy_failure_does_not_resubmit(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "policy-failed.mp4"
    calls: list[str] = []

    def fake_request(method: str, *args: object, **kwargs: object) -> dict:
        del args, kwargs
        calls.append(method)
        if method == "POST":
            return {"id": "job-policy", "polling_url": "https://example.test/poll"}
        return {
            "status": "failed",
            "error": {
                "code": "ContentPolicyViolation",
                "message": "https://signed.example/file?token=secret#fragment",
            },
        }

    monkeypatch.setattr(video_script, "_http_request", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "4",
            "--max-retries",
            "2",
        ],
    )

    assert video_script.main() == 1
    captured = capsys.readouterr()
    assert calls == ["POST", "GET"]
    assert "ContentPolicyViolation" in captured.err
    assert "signed.example" not in captured.err
    assert "token=secret" not in captured.err
    assert "#fragment" not in captured.err
    receipt = json.loads(
        output.with_suffix(".mp4.receipt.json").read_text(encoding="utf-8")
    )
    assert receipt == {
        "fallback": False,
        "model": "bytedance/seedance-2.0",
        "policy_code": "ContentPolicyViolation",
        "provider": "openrouter",
        "reason": "provider_policy_rejected",
        "status": "policy_rejected",
    }
    serialized = json.dumps(receipt)
    assert "job-policy" not in serialized
    assert "signed.example" not in serialized


def test_seedance_http_200_error_envelope_is_safe_and_nonretryable(
    video_script: ModuleType,
    isolated_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-only")
    output = isolated_runtime / "policy-envelope.mp4"
    calls = 0

    def fake_request(*args: object, **kwargs: object) -> dict:
        nonlocal calls
        del args, kwargs
        calls += 1
        return {
            "error": {
                "code": 400,
                "message": (
                    'HTTP 400: {"error":{"code":'
                    '"InputImageSensitiveContentDetected.PrivacyInformation",'
                    '"request_id":"req-secret",'
                    '"url":"https://signed.example/video?token=secret#fragment"}}'
                ),
            }
        }

    monkeypatch.setattr(video_script, "_http_request", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_video.py",
            "--prompt",
            "synthetic",
            "--filename",
            str(output),
            "--duration",
            "4",
            "--max-retries",
            "2",
        ],
    )

    assert video_script.main() == 1
    captured = capsys.readouterr()
    assert calls == 1
    assert "HTTP 400" in captured.err
    assert "InputImageSensitiveContentDetected.PrivacyInformation" in captured.err
    assert "req-secret" not in captured.err
    assert "signed.example" not in captured.err
    assert "token=secret" not in captured.err
    assert "#fragment" not in captured.err
    receipt = json.loads(
        output.with_suffix(".mp4.receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "policy_rejected"
    assert receipt["reason"] == "provider_policy_rejected"
    assert receipt["policy_code"] == (
        "InputImageSensitiveContentDetected.PrivacyInformation"
    )
    serialized = json.dumps(receipt)
    assert "req-secret" not in serialized
    assert "signed.example" not in serialized
    assert "token=secret" not in serialized
