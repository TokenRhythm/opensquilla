"""CJK font-selection contracts for the deterministic title-card renderer."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "src/opensquilla/skills/bundled/title-card-image/scripts/render.py"
SKILL = SCRIPT.parents[1] / "SKILL.md"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_title_card_image_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Mask:
    size = (8, 8)

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __bytes__(self) -> bytes:
        return self.payload


class _CoveredFont:
    def getmask(self, char: str, *, mode: str) -> _Mask:
        assert mode == "L"
        if char in {"\u0378", "\uffff", "\U0010ffff"}:
            return _Mask(b"missing")
        return _Mask(f"glyph:{char}".encode())


class _TofuFont:
    def getmask(self, char: str, *, mode: str) -> _Mask:
        del char
        assert mode == "L"
        return _Mask(b"missing")


class _AsciiOnlyFont:
    def getmask(self, char: str, *, mode: str) -> _Mask:
        assert mode == "L"
        if char in {"\u0378", "\uffff", "\U0010ffff"} or ord(char) >= 128:
            return _Mask(b"missing")
        return _Mask(f"glyph:{char}".encode())


def test_managed_cjk_font_directory_is_preferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    fonts = tmp_path / "managed-fonts"
    fonts.mkdir()
    managed = fonts / "NotoSansCJK-Regular.ttc"
    managed.write_bytes(b"synthetic-font")
    loaded: list[str] = []

    def fake_truetype(candidate: str, size: int) -> _CoveredFont:
        assert size == 48
        loaded.append(str(candidate))
        return _CoveredFont()

    import PIL.ImageFont

    monkeypatch.setenv("OPENSQUILLA_MEDIA_FONTS_DIR", str(fonts))
    monkeypatch.setattr(PIL.ImageFont, "truetype", fake_truetype)

    source = module._resolve_font_source("深夜图书馆", None)

    assert Path(source) == managed
    assert loaded == [str(managed)]


def test_explicit_tofu_font_falls_back_to_covered_managed_font(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    explicit = tmp_path / "latin-only.ttf"
    explicit.write_bytes(b"synthetic-latin")
    fonts = tmp_path / "managed-fonts"
    fonts.mkdir()
    managed = fonts / "NotoSansCJK-Regular.ttc"
    managed.write_bytes(b"synthetic-cjk")

    def fake_truetype(candidate: str, size: int):
        del size
        return _TofuFont() if Path(candidate) == explicit else _CoveredFont()

    import PIL.ImageFont

    monkeypatch.setenv("OPENSQUILLA_MEDIA_FONTS_DIR", str(fonts))
    monkeypatch.setattr(PIL.ImageFont, "truetype", fake_truetype)

    assert Path(module._resolve_font_source("完", str(explicit))) == managed


def test_missing_cjk_font_fails_with_actionable_setup_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()

    import PIL.ImageFont

    monkeypatch.setattr(
        module,
        "_iter_font_candidates",
        lambda explicit=None: iter(("latin-only.ttf",)),
    )
    monkeypatch.setattr(
        PIL.ImageFont,
        "truetype",
        lambda candidate, size: _TofuFont(),
    )

    with pytest.raises(RuntimeError) as caught:
        module._resolve_font_source("中文标题")

    message = str(caught.value)
    assert "CJK-capable font" in message
    assert "OPENSQUILLA_MEDIA_FONTS_DIR" in message
    assert "--font" in message


def test_missing_cjk_font_never_writes_tofu_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    output = tmp_path / "cover.png"
    monkeypatch.setattr(
        module,
        "_resolve_font_source",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("No CJK-capable font could be loaded. Pass --font.")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--text", "中文标题", "--output", str(output)],
    )

    assert module.main() == 1
    assert not output.exists()
    assert "CJK-capable font" in capsys.readouterr().err


def test_ascii_card_uses_pillow_default_font_on_minimal_system(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    output = tmp_path / "cover.png"
    monkeypatch.setattr(module, "_iter_font_candidates", lambda explicit=None: iter(()))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--text",
            "Release Ready",
            "--subtitle",
            "Build 42",
            "--output",
            str(output),
        ],
    )

    assert module.main() == 0
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert str(output.resolve()) in capsys.readouterr().out


def test_non_ascii_text_without_font_still_fails_actionably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    monkeypatch.setattr(module, "_iter_font_candidates", lambda explicit=None: iter(()))

    with pytest.raises(RuntimeError, match="compatible font"):
        module._resolve_font_source("Caf\u00e9")


def test_loadable_font_missing_non_ascii_glyph_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    loaded: list[str] = []

    import PIL.ImageFont

    monkeypatch.setattr(
        module,
        "_iter_font_candidates",
        lambda explicit=None: iter(("ascii-only.ttf",)),
    )

    def fake_truetype(candidate: str, size: int) -> _AsciiOnlyFont:
        assert size == 48
        loaded.append(candidate)
        return _AsciiOnlyFont()

    monkeypatch.setattr(PIL.ImageFont, "truetype", fake_truetype)

    with pytest.raises(RuntimeError, match="compatible font") as caught:
        module._resolve_font_source("Caf\u00e9")

    assert loaded == ["ascii-only.ttf"]
    assert "--font" in str(caught.value)


def test_platform_fallbacks_cover_windows_macos_and_linux() -> None:
    module = _load_script()
    candidates = set(module._SYSTEM_CJK_FONT_CANDIDATES)

    assert r"C:\Windows\Fonts\msyh.ttc" in candidates
    assert "/System/Library/Fonts/STHeiti Medium.ttc" in candidates
    assert "/System/Library/Fonts/Hiragino Sans GB.ttc" in candidates
    assert "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc" in candidates
    assert any("SourceHanSans" in candidate for candidate in candidates)


def test_manifest_forwards_explicit_font_override() -> None:
    metadata = yaml.safe_load(SKILL.read_text(encoding="utf-8").split("---", 2)[1])
    args = metadata["entrypoint"]["args"]

    index = args.index("--font")
    assert args[index + 1] == "{{ with.font | default('') }}"
