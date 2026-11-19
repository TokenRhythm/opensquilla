"""Microphone capture and STT-chain transcription for terminal voice input (PR-4).

Captures microphone audio with ffmpeg (already a SenseVoice dependency, so no
new runtime requirement) and transcribes it through the same provider chain the
WebUI uses (:func:`opensquilla.audio.stt_registry.build_stt_chain`). The
recorder is start/stop driven so a terminal surface can toggle recording on a
key press without owning a raw keyboard listener.

Platform capture backends: Windows ``dshow``, macOS ``avfoundation``, Linux
``pulse``/``alsa``. Device discovery is best-effort; an explicit device name may
always be configured.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
from typing import TYPE_CHECKING

from opensquilla.audio.stt_registry import SttConfigError, build_stt_chain
from opensquilla.audio.stt_types import SttAudio, SttResult

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig

_TARGET_SAMPLE_RATE = 16000
_DEFAULT_MAX_SECONDS = 60.0


class VoiceCaptureError(RuntimeError):
    """Microphone capture or transcription setup failure."""


def find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not ffmpeg:
        raise VoiceCaptureError(
            "ffmpeg not found on PATH; install it (winget install Gyan.FFmpeg) "
            "to use terminal voice input."
        )
    return ffmpeg


def _capture_input_args(device: str) -> list[str]:
    """Platform-specific ffmpeg input arguments for the default microphone."""
    if sys.platform == "win32":
        return ["-f", "dshow", "-i", f"audio={device}"]
    if sys.platform == "darwin":
        # avfoundation audio device index; ":none" selects no video.
        return ["-f", "avfoundation", "-i", f":{device or '0'}"]
    # Linux: prefer PulseAudio default source, fall back to ALSA default.
    return ["-f", "pulse", "-i", device or "default"]


def list_capture_devices() -> list[str]:
    """Best-effort list of audio capture device names (Windows dshow).

    Returns an empty list on platforms where discovery is not implemented or
    ffmpeg is unavailable; callers should fall back to the OS default device.
    """
    if sys.platform != "win32":
        return []
    try:
        ffmpeg = find_ffmpeg()
    except VoiceCaptureError:
        return []
    import subprocess

    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    text = proc.stderr.decode("utf-8", "replace")
    devices: list[str] = []
    in_audio = False
    for line in text.splitlines():
        if "Alternative name" in line:
            continue
        # Newer ffmpeg tags each device line with "(audio)"/"(video)".
        tagged = re.search(r'"([^"]+)"\s*\(audio\)', line)
        if tagged:
            devices.append(tagged.group(1))
            continue
        # Older ffmpeg groups devices under section headers.
        if "DirectShow audio devices" in line:
            in_audio = True
            continue
        if "DirectShow video devices" in line:
            in_audio = False
            continue
        if in_audio:
            match = re.search(r'"([^"]+)"', line)
            if match:
                devices.append(match.group(1))
    return devices


def resolve_device(configured: str) -> str:
    """Pick the configured device, else the first discovered capture device."""
    if configured.strip():
        return configured.strip()
    devices = list_capture_devices()
    return devices[0] if devices else ""


class MicrophoneRecorder:
    """Start/stop microphone capture backed by an ffmpeg subprocess.

    ``start`` launches ffmpeg writing 16 kHz mono WAV to a temp file; ``stop``
    asks ffmpeg to finalize gracefully and returns the recorded bytes;
    ``cancel`` aborts and discards. ``elapsed`` reports seconds since start so a
    surface can render a recording indicator.
    """

    def __init__(self, *, max_seconds: float = _DEFAULT_MAX_SECONDS) -> None:
        self._max_seconds = max(1.0, float(max_seconds))
        self._proc: asyncio.subprocess.Process | None = None
        self._path: str | None = None
        self._started_at: float | None = None

    @property
    def recording(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.monotonic() - self._started_at

    async def start(self, device: str = "") -> None:
        if self.recording:
            raise VoiceCaptureError("recorder is already running")
        ffmpeg = find_ffmpeg()
        fd, self._path = tempfile.mkstemp(suffix=".wav", prefix="opensquilla-voice-")
        os.close(fd)
        args = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            *_capture_input_args(device),
            "-t", str(self._max_seconds),
            "-ac", "1", "-ar", str(_TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le", "-y", self._path,
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._cleanup_file()
            raise VoiceCaptureError(f"failed to start ffmpeg capture: {exc}") from exc
        # Detect an immediate device-open failure (e.g. a virtual microphone
        # ffmpeg cannot read) instead of silently reporting "recording".
        await asyncio.sleep(0.15)
        if self._proc.returncode is not None:
            detail = ""
            stderr = self._proc.stderr
            if stderr is not None:
                try:
                    detail = (
                        (await stderr.read()).decode("utf-8", "replace").strip()[:300]
                    )
                except (OSError, ValueError):
                    pass
            self._cleanup_file()
            self._proc = None
            self._path = None
            raise VoiceCaptureError(
                f"microphone capture failed to start: {detail or 'ffmpeg exited immediately'}"
            )
        self._started_at = time.monotonic()

    async def stop(self) -> bytes:
        """Finalize the recording and return WAV bytes (empty if nothing)."""
        proc = self._proc
        path = self._path
        if proc is None or path is None:
            raise VoiceCaptureError("recorder is not running")
        try:
            if proc.returncode is None:
                # 'q' asks ffmpeg to stop cleanly and write the WAV trailer.
                stdin = proc.stdin
                if stdin is not None:
                    try:
                        stdin.write(b"q")
                        await stdin.drain()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    proc.terminate()
                    await proc.wait()
            data = b""
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError:
                data = b""
            return data
        finally:
            self._cleanup_file()
            self._proc = None
            self._path = None
            self._started_at = None

    async def cancel(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except TimeoutError:
                proc.kill()
        self._cleanup_file()
        self._proc = None
        self._path = None
        self._started_at = None

    def _cleanup_file(self) -> None:
        if self._path:
            try:
                os.unlink(self._path)
            except OSError:
                pass


async def transcribe_audio(
    config: GatewayConfig,
    audio: SttAudio,
    *,
    language: str | None = None,
) -> SttResult | None:
    """Transcribe through the configured STT chain; first non-empty wins.

    Mirrors the WebUI route semantics: providers are tried in order, an empty
    transcription is a miss (try the next link), and an all-empty chain returns
    None rather than raising. Raises :class:`VoiceCaptureError` when no provider
    is configured.
    """
    try:
        chain = list(build_stt_chain(config))
    except SttConfigError as exc:
        raise VoiceCaptureError(str(exc)) from exc
    if not chain:
        raise VoiceCaptureError("no speech-to-text provider configured")
    global_language = str(getattr(config.audio.stt, "language", "") or "").strip()
    hint = language or global_language or None
    errors: list[str] = []
    for provider in chain:
        try:
            candidate = await provider.transcribe(audio, model_id=None, language=hint)
        except Exception as exc:  # noqa: BLE001 - try the next link
            errors.append(f"{provider.provider_id}: {exc}")
            continue
        if not (candidate.text or "").strip():
            errors.append(f"{provider.provider_id}: empty transcription")
            continue
        return candidate
    if errors and all("empty transcription" in error for error in errors):
        return None
    raise VoiceCaptureError("; ".join(errors) or "transcription failed")


__all__ = [
    "MicrophoneRecorder",
    "VoiceCaptureError",
    "find_ffmpeg",
    "list_capture_devices",
    "resolve_device",
    "transcribe_audio",
]
