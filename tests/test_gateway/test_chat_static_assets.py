"""Control UI template contract for the Vue entrypoint."""

from pathlib import Path

INDEX_TEMPLATE = Path("src/opensquilla/gateway/templates/index.html")


def test_control_index_loads_only_the_vue_entrypoint() -> None:
    index = INDEX_TEMPLATE.read_text(encoding="utf-8")

    assert 'id="app"' in index
    assert 'id="opensquilla-data"' in index
    assert "vite_css_urls" in index
    assert "vite_js_url" in index
    assert "/static/js/" not in index
    assert "/static/css/" not in index
    assert "/static/vendor/" not in index
