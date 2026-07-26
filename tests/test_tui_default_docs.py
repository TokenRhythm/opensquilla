from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_current_user_docs_keep_opentui_source_only() -> None:
    current_docs = {
        path: _read(path)
        for path in (
            "README.md",
            "docs/tui.md",
            "docs/cli.md",
            "docs/features/tui-frontend.md",
            "docs/features/tui-product-contract.md",
            "docs/tui-real-terminal-harness.md",
        )
    }

    combined = _squash("\n".join(current_docs.values())).lower()
    assert "opensquilla_tui_dev_source_host=1" in combined
    assert "opensquilla chat --ui tui" in combined
    assert "current releases do not publish" in combined
    assert "not currently wired into the formal `v*` release workflow" in combined

    public_install_docs = "\n".join(
        _read(path)
        for path in (
            "README.product.md",
            "README.zh-Hans.md",
            "docs/quickstart.md",
        )
    )
    assert "opensquilla_tui_host-" not in public_install_docs
    assert "--tui-host-only" not in public_install_docs


def test_current_docs_describe_one_development_full_screen_tui() -> None:
    product_contract = _squash(_read("docs/features/tui-product-contract.md"))
    harness = _squash(_read("docs/tui-real-terminal-harness.md"))

    assert "intended terminal product" in product_contract
    assert "source-checkout development surface" in product_contract
    assert "development full-screen renderer" in harness
