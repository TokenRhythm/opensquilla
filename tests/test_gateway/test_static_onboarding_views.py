"""Vue onboarding-route and example-config contracts."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_ROUTES = REPO_ROOT / "opensquilla-webui" / "src" / "router" / "webRoutes.ts"
SETTINGS_VIEW = REPO_ROOT / "opensquilla-webui" / "src" / "views" / "web" / "SettingsView.vue"


def test_setup_deep_link_redirects_into_vue_settings() -> None:
    routes = WEB_ROUTES.read_text(encoding="utf-8")

    assert "SettingsView.vue" in routes
    assert "path: '/settings'" in routes
    assert "name: 'settings'" in routes
    assert "path: '/setup'" in routes
    assert "redirect: '/settings/auto'" in routes
    assert SETTINGS_VIEW.is_file()


def test_example_config_does_not_advertise_local_embedding_model_override() -> None:
    text = (REPO_ROOT / "opensquilla.toml.example").read_text(encoding="utf-8")
    local_section = text.split("# [memory.embedding.local]", 1)[1].split(
        "# [memory.embedding.remote]",
        1,
    )[0]

    assert "model =" not in local_section
    assert "onnx_dir" in local_section
