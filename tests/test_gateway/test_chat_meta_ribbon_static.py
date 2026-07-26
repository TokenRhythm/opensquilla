"""Vue Control UI wiring contracts for the meta-run surfaces.

Interactive behavior is covered by ``opensquilla-webui/e2e/meta-ribbon.spec.ts``;
these focused checks keep the active Vue components mounted after the vanilla
frontend assets are removed.
"""

from pathlib import Path

META_RIBBON = Path("opensquilla-webui/src/components/chat/MetaRibbon.vue")
META_PREFLIGHT = Path("opensquilla-webui/src/components/chat/MetaPreflightCard.vue")
CHAT_VIEW = Path("opensquilla-webui/src/views/ChatView.vue")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_vue_chat_view_wires_meta_ribbon_and_preflight() -> None:
    assert META_RIBBON.is_file()
    assert META_PREFLIGHT.is_file()

    view = _read(CHAT_VIEW)
    assert "MetaRibbon" in view
    assert "MetaPreflightCard" in view
    assert "useMetaRuns" in view


def test_vue_meta_components_preserve_accessible_markup_contract() -> None:
    ribbon = _read(META_RIBBON)
    assert "meta-ribbon" in ribbon
    assert "meta-ribbon-chips" in ribbon
    assert 'role="progressbar"' in ribbon

    preflight = _read(META_PREFLIGHT)
    assert "meta-preflight" in preflight
