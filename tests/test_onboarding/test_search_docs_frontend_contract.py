from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_search_docs_describe_runtime_provider_matrix() -> None:
    docs = "\n".join(
        [
            _read("docs/search.md"),
            _read("docs/configuration.md"),
            _read("docs/troubleshooting.md"),
            _read("README.md"),
            _read("README.product.md"),
            _read("opensquilla.toml.example"),
        ]
    )

    for expected in [
        "Bocha",
        "BOCHA_SEARCH_API_KEY",
        "Alibaba Cloud IQS",
        "IQS_SEARCH_API_KEY",
        "Tavily",
        "TAVILY_API_KEY",
        "Exa",
        "EXA_API_KEY",
        "DuckDuckGo",
        "no-key",
        "partial-key",
        "all-key",
        "search_api_key_env",
        "search_fallback_policy",
        "search_diagnostics",
    ]:
        assert expected in docs

    assert '"duckduckgo", "bocha", "brave", "iqs", "tavily", or "exa"' in docs
    assert "web search (DuckDuckGo, Bocha, Brave, IQS, Tavily, or Exa)" in docs


def test_desktop_search_surfaces_use_shared_runtime_provider_catalog() -> None:
    main_ts = _read("desktop/electron/src/main.ts")
    platform_types = _read("opensquilla-webui/src/platform/types.ts")

    # The desktop native onboarding wizard and the desktop settings snapshot
    # surface the full runtime search catalog (Bocha/Tavily/Exa), never a
    # hardcoded brave/duckduckgo-only list.
    for expected in [
        "SEARCH_PROVIDER_CATALOG",
        "BOCHA_SEARCH_API_KEY",
        "IQS_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "Bocha",
        "Alibaba Cloud IQS",
        "Tavily",
        "Exa",
    ]:
        assert expected in main_ts

    old_normalizer = (
        "return String(raw || '').trim().toLowerCase() === 'brave' "
        "? 'brave' : 'duckduckgo'"
    )
    assert old_normalizer not in main_ts
    assert "searchProviders: SEARCH_PROVIDER_CATALOG" in main_ts
    assert "searchProviders?: SearchProviderOption[]" in platform_types

    # The bespoke desktop settings view and its standalone search selector were
    # removed: desktop now renders the same RPC-backed SettingsDialog as web,
    # whose Capabilities section drives search from the gateway runtime catalog.
    # That is the strongest form of "shared runtime provider catalog" — desktop
    # cannot drift back to a hardcoded list because it owns no search picker.
    assert not (
        ROOT / "opensquilla-webui/src/views/desktop/DesktopSettingsView.vue"
    ).exists()
    assert not (
        ROOT / "opensquilla-webui/src/components/settings/SearchProviderSelector.vue"
    ).exists()

    web_routes = _read("opensquilla-webui/src/router/webRoutes.ts")
    assert "platforms: ['web', 'desktop']" in web_routes

    settings_view = _read("opensquilla-webui/src/views/web/SettingsView.vue")
    assert "@/views/web/SettingsView.vue" in web_routes
    assert "@/components/settings/SettingsDialog.vue" in settings_view
    assert "<SettingsDialog />" in settings_view

    settings_dialog = _read(
        "opensquilla-webui/src/components/settings/SettingsDialog.vue"
    )
    assert "SetupCapabilitiesPanel" in settings_dialog
