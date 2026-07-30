from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).parent.parent


def _load_compose() -> dict:
    return yaml.safe_load((_ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_compose_no_version_field() -> None:
    data = _load_compose()
    assert "version" not in data, (
        "compose.yaml must not have a top-level 'version:' field (use Compose v2 syntax)"
    )


def test_compose_gateway_port_is_loopback() -> None:
    data = _load_compose()
    ports = data["services"]["gateway"]["ports"]
    assert any(
        str(p) == "127.0.0.1:18791:18791" for p in ports
    ), f"Expected '127.0.0.1:18791:18791' in ports, got: {ports}"


def test_compose_gateway_healthcheck_exists() -> None:
    data = _load_compose()
    hc = data["services"]["gateway"].get("healthcheck")
    assert hc is not None, "services.gateway.healthcheck must be defined"


def test_compose_gateway_environment_has_openrouter_key() -> None:
    data = _load_compose()
    env = data["services"]["gateway"].get("environment", {})
    # environment can be a dict or a list of "KEY=VAL" strings
    if isinstance(env, dict):
        assert "OPENROUTER_API_KEY" in env, (
            f"OPENROUTER_API_KEY missing from environment dict: {env}"
        )
    else:
        keys = [item.split("=")[0] for item in env]
        assert "OPENROUTER_API_KEY" in keys, (
            f"OPENROUTER_API_KEY missing from environment list: {env}"
        )


def _load_dockerfile() -> str:
    return (_ROOT / "Dockerfile").read_text(encoding="utf-8")


def _load_dockerignore_rules() -> set[str]:
    lines = (_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    return {
        line
        for raw_line in lines
        if (line := raw_line.strip()) and not line.startswith("#")
    }


def test_docker_build_validates_generated_webui_before_python_packaging() -> None:
    dockerfile = _load_dockerfile()

    assert (
        "FROM --platform=$BUILDPLATFORM node:22.12.0-bookworm-slim AS webui-builder"
        in dockerfile
    )
    assert "--mount=type=cache,target=/root/.npm,sharing=locked npm ci" in dockerfile
    assert "RUN npm run build:artifact" in dockerfile
    assert "ARG OPENSQUILLA_FORBID_PERSONAL_BGM=0" in dockerfile
    assert "npm run verify:release-dist" in dockerfile
    assert "COPY hatch_build.py ./" in dockerfile
    assert "COPY scripts/verify_webui_artifact.py ./scripts/verify_webui_artifact.py" in dockerfile
    assert "COPY opensquilla-webui/ ./opensquilla-webui/" in dockerfile
    assert "COPY --from=webui-builder" in dockerfile
    assert dockerfile.index("COPY --from=webui-builder") < dockerfile.index(
        'RUN pip install ".[recommended]"'
    )
    assert "rm -rf hatch_build.py scripts opensquilla-webui" in dockerfile
    assert "!scripts/verify_webui_artifact.py" in _load_dockerignore_rules()


def test_dockerignore_prevents_stale_webui_and_nested_secrets_from_entering_context() -> None:
    rules = _load_dockerignore_rules()

    assert {
        "src/opensquilla/gateway/static/dist",
        ".env*",
        "**/.env*",
        ".npmrc",
        "**/.npmrc",
        "*.pem",
        "**/*.pem",
        "*.key",
        "**/*.key",
    } <= rules
    assert "!opensquilla-webui/.node-version" not in rules, (
        "The root-only hidden-file rule does not exclude the nested .node-version; "
        "keeping a negation for it incorrectly documents Docker ignore semantics."
    )


def test_dockerfile_gateway_port_matches_compose() -> None:
    """Dockerfile's container gateway port must match compose's 18791.

    Drift here — e.g. the Dockerfile keeping EXPOSE 18790 while compose
    publishes 18791 — makes the documented `docker compose` path
    unreachable.
    """
    dockerfile = _load_dockerfile()
    compose = _load_compose()

    ports = [str(p) for p in compose["services"]["gateway"]["ports"]]
    assert any("18791:18791" in p for p in ports), (
        f"compose.yaml must publish 18791:18791, got: {ports}"
    )

    assert "18790" not in dockerfile, (
        "Dockerfile still references the stale 18790 gateway port; "
        "it must use 18791 to match compose.yaml"
    )
    assert "OPENSQUILLA_GATEWAY_PORT=18791" in dockerfile
    assert "EXPOSE 18791" in dockerfile
    assert "http://127.0.0.1:18791/healthz" in dockerfile


def test_compose_persists_state_via_named_volume() -> None:
    """Gateway config and state must persist via a Docker named volume
    mounted at the image's OPENSQUILLA_STATE_DIR.

    The container runs as a non-root user, so a host bind mount to
    /root/.opensquilla never receives anything the gateway writes — config
    and state would silently vanish on every container recreate.
    """
    compose = _load_compose()
    dockerfile = _load_dockerfile()

    match = re.search(r"OPENSQUILLA_STATE_DIR=(\S+)", dockerfile)
    assert match, "Dockerfile must pin OPENSQUILLA_STATE_DIR for a stable volume target"
    state_dir = match.group(1)
    assert state_dir.startswith("/"), (
        f"OPENSQUILLA_STATE_DIR must be an absolute path, got: {state_dir!r}"
    )

    # This is a static shape test: it pins the Dockerfile/compose text, not a
    # built image. The mkdir/chown assertions are coupled to the current
    # command spelling — update them alongside any Dockerfile rewording.
    # The image pre-creates the state root owned by the non-root user so a
    # freshly initialized named volume inherits writable ownership.
    assert f"mkdir -p {state_dir}" in dockerfile, (
        f"Dockerfile must create the state root {state_dir}"
    )
    assert re.search(
        rf"chown\b[^\n]*opensquilla:opensquilla[^\n]*{re.escape(state_dir)}", dockerfile
    ), f"Dockerfile must chown {state_dir} to the non-root opensquilla user"

    gateway_volumes = [str(v) for v in compose["services"]["gateway"]["volumes"]]
    assert f"opensquilla-state:{state_dir}" in gateway_volumes, (
        f"gateway must mount the 'opensquilla-state' named volume at {state_dir}, "
        f"got: {gateway_volumes}"
    )
    assert not any("/root/" in v for v in gateway_volumes), (
        "compose must not mount into /root — the container runs as a non-root user"
    )

    assert "opensquilla-state" in compose.get("volumes", {}), (
        "compose must declare the top-level 'opensquilla-state' named volume"
    )
