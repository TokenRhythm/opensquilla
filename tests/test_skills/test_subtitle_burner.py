"""Media-integrity and font-wiring contracts for the subtitle-burner skill."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = (
    Path(__file__).parents[2]
    / "src/opensquilla/skills/bundled/subtitle-burner/scripts/burn.py"
)
SKILL = SCRIPT.parents[1] / "SKILL.md"


def _probe_result(
    cmd: list[str],
    *,
    returncode: int = 0,
    duration: str = "1.25",
) -> subprocess.CompletedProcess[bytes]:
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "width": 720,
                "height": 1280,
                "duration": duration,
            },
        ],
        "format": {"duration": duration},
    }
    stdout = json.dumps(payload).encode() if returncode == 0 else b""
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=b"")


def _fake_media_bins(module, monkeypatch) -> None:
    def which(name: str) -> str | None:
        executable = Path(name).name.lower()
        if executable in {"ffmpeg", "ffmpeg.exe"}:
            return "/managed/bin/ffmpeg"
        if executable in {"ffprobe", "ffprobe.exe"}:
            return "/managed/bin/ffprobe"
        return None

    monkeypatch.setattr(module.shutil, "which", which)


def _load_module():
    spec = importlib.util.spec_from_file_location("subtitle_burner", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_managed_font_directory_and_single_family_reach_libass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    video = tmp_path / "input.mp4"
    subtitles = tmp_path / "captions.srt"
    output = tmp_path / "output.mp4"
    fonts = tmp_path / "fonts"
    video.write_bytes(b"synthetic-video")
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\n中文字幕\n", encoding="utf-8")
    fonts.mkdir()
    (fonts / "NotoSansCJK-Regular.ttc").write_bytes(b"synthetic-font")
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if Path(cmd[0]).name.startswith("ffprobe"):
            return _probe_result(cmd)
        Path(cmd[-1]).write_bytes(b"encoded-video")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    _fake_media_bins(module, monkeypatch)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setenv("OPENSQUILLA_MEDIA_FONTS_DIR", str(fonts))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--input",
            str(video),
            "--subtitles",
            str(subtitles),
            "--output",
            str(output),
        ],
    )

    assert module.main() == 0
    ffmpeg_call = next(call for call in calls if Path(call[0]).name == "ffmpeg")
    vf = ffmpeg_call[ffmpeg_call.index("-vf") + 1]
    escaped_fonts = module._escape_subtitle_path(str(fonts.resolve()))
    assert f"fontsdir='{escaped_fonts}'" in vf
    assert "FontName=Noto Sans CJK SC" in vf
    assert "Microsoft YaHei," not in vf
    assert output.read_bytes() == b"encoded-video"


@pytest.mark.parametrize(
    ("native_path", "filter_path"),
    [
        (r"C:\Managed Fonts\Noto", r"C\:/Managed Fonts/Noto"),
        ("/opt/opensquilla/fonts", "/opt/opensquilla/fonts"),
    ],
)
def test_subtitle_filter_paths_are_platform_neutral(
    native_path: str,
    filter_path: str,
) -> None:
    module = _load_module()

    assert module._escape_subtitle_path(native_path) == filter_path


def test_missing_explicit_font_directory_fails_before_ffmpeg(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    video = tmp_path / "input.mp4"
    subtitles = tmp_path / "captions.srt"
    video.write_bytes(b"synthetic-video")
    subtitles.write_text("synthetic", encoding="utf-8")
    calls: list[object] = []
    _fake_media_bins(module, monkeypatch)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: calls.append(args),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--input",
            str(video),
            "--subtitles",
            str(subtitles),
            "--output",
            str(tmp_path / "output.mp4"),
            "--fonts-dir",
            str(tmp_path / "missing-fonts"),
        ],
    )

    assert module.main() == 1
    assert calls == []


def test_empty_subtitles_copy_valid_video_without_burning(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_module()
    video = tmp_path / "input.mp4"
    subtitles = tmp_path / "captions.srt"
    output = tmp_path / "output.mp4"
    video.write_bytes(b"synthetic-valid-video")
    subtitles.write_text("\n\t\n", encoding="utf-8")
    output.write_bytes(b"stale-output")
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if Path(cmd[0]).name.startswith("ffmpeg"):
            raise AssertionError("ffmpeg subtitle burn must not run for empty SRT")
        return _probe_result(cmd)

    _fake_media_bins(module, monkeypatch)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--input",
            str(video),
            "--subtitles",
            str(subtitles),
            "--output",
            str(output),
        ],
    )

    assert module.main() == 0
    assert output.read_bytes() == video.read_bytes()
    stdout = capsys.readouterr().out
    assert "SUBTITLES_SKIPPED: empty" in stdout
    assert str(output.resolve()) in stdout
    assert all(Path(call[0]).name == "ffprobe" for call in calls)
    assert list(tmp_path.glob(".output.*.mp4")) == []


def test_fake_input_bytes_do_not_pass_empty_subtitle_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    video = tmp_path / "input.mp4"
    subtitles = tmp_path / "captions.srt"
    output = tmp_path / "output.mp4"
    video.write_bytes(b"not-an-mp4")
    subtitles.write_text("", encoding="utf-8")
    output.write_bytes(b"existing-output")
    _fake_media_bins(module, monkeypatch)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda cmd, **kwargs: _probe_result(cmd, returncode=1),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--input",
            str(video),
            "--subtitles",
            str(subtitles),
            "--output",
            str(output),
        ],
    )

    assert module.main() == 1
    assert output.read_bytes() == b"existing-output"


def test_zero_exit_ffmpeg_with_undecodable_output_is_rejected_atomically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_module()
    video = tmp_path / "input.mp4"
    subtitles = tmp_path / "captions.srt"
    output = tmp_path / "output.mp4"
    video.write_bytes(b"synthetic-valid-video")
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    output.write_bytes(b"existing-output")

    def run(cmd, **kwargs):
        executable = Path(cmd[0]).name
        if executable == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"not-a-video")
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if Path(cmd[-1]) == video:
            return _probe_result(cmd)
        return _probe_result(cmd, returncode=1)

    _fake_media_bins(module, monkeypatch)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--input",
            str(video),
            "--subtitles",
            str(subtitles),
            "--output",
            str(output),
        ],
    )

    assert module.main() == 1
    assert output.read_bytes() == b"existing-output"
    assert list(tmp_path.glob(".output.*.mp4")) == []


def test_ffprobe_resolution_changes_only_executable_basename(tmp_path: Path) -> None:
    module = _load_module()
    bin_dir = tmp_path / "ffmpeg-full" / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg"
    ffprobe = bin_dir / "ffprobe"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")

    assert module._resolve_ffprobe("ffprobe", str(ffmpeg)) == str(ffprobe)


def test_manifest_declares_probe_and_forwards_font_directory() -> None:
    frontmatter = SKILL.read_text(encoding="utf-8").split("---", 2)[1]
    manifest = yaml.safe_load(frontmatter)

    assert manifest["metadata"]["opensquilla"]["requires"]["bins"] == [
        "ffmpeg",
        "ffprobe",
    ]
    args = manifest["entrypoint"]["args"]
    fonts_index = args.index("--fonts-dir")
    assert args[fonts_index + 1] == "{{ with.fonts_dir | default('') }}"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="real ffmpeg/ffprobe integration requires both executables",
)
def test_real_ffmpeg_empty_subtitles_produce_decodable_video(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    assert ffmpeg is not None and ffprobe is not None
    video = tmp_path / "input.mp4"
    subtitles = tmp_path / "captions.srt"
    output = tmp_path / "output.mp4"
    subtitles.write_text("\n", encoding="utf-8")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x96:r=10",
            "-t",
            "0.4",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(video),
        ],
        check=True,
        capture_output=True,
    )

    burned = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(video),
            "--subtitles",
            str(subtitles),
            "--output",
            str(output),
            "--ffmpeg-path",
            ffmpeg,
            "--ffprobe-path",
            ffprobe,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert burned.returncode == 0, burned.stderr
    assert "SUBTITLES_SKIPPED: empty" in burned.stdout
    probed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type,width,height:format=duration",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(probed.stdout)
    assert payload["streams"][0]["codec_type"] == "video"
    assert payload["streams"][0]["width"] == 64
    assert payload["streams"][0]["height"] == 96
    assert float(payload["format"]["duration"]) > 0
