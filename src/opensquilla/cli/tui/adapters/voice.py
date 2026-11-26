"""Terminal voice input helper for the /voice slash command (PR-4).

Records microphone audio (ffmpeg), transcribes it through the configured STT
chain, and returns the text for the caller to submit as a user turn. The
recording is toggle-driven (Enter to start, Enter to stop, Ctrl+C to cancel) so
it works in the plain/native terminal without a raw keyboard listener. The
full-screen OpenTUI host owns the terminal and is not supported here yet; the
caller passes ``host_ui=True`` to surface a notice instead.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from opensquilla.cli.ui import console

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig


async def _read_line(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def capture_voice_text(
    config: GatewayConfig, *, host_ui: bool = False
) -> str | None:
    """Record → transcribe → return text (None on cancel/failure/empty)."""
    from opensquilla.audio.stt_types import SttAudio
    from opensquilla.audio.voice_capture import (
        MicrophoneRecorder,
        VoiceCaptureError,
        resolve_device,
        transcribe_audio,
    )

    if host_ui:
        console.print(
            "[yellow]/voice currently needs the plain terminal "
            "(opensquilla chat --ui plain); the full-screen host does not yet "
            "expose microphone capture.[/yellow]"
        )
        return None

    if not getattr(config.audio, "enabled", False):
        console.print(
            "[yellow]Audio is disabled. Set [audio].enabled = true and configure "
            "an STT provider ([audio.stt]) to use /voice.[/yellow]"
        )
        return None

    capture_cfg = config.audio.capture
    try:
        device = resolve_device(capture_cfg.device)
    except VoiceCaptureError as exc:
        console.print(f"[red]{exc}[/red]")
        return None

    recorder = MicrophoneRecorder(max_seconds=capture_cfg.max_seconds)
    try:
        await recorder.start(device)
    except VoiceCaptureError as exc:
        console.print(f"[red]{exc}[/red]")
        return None

    console.print(
        "[bold red]● 录音中[/bold red] [dim]直接说话，说完按 Enter 停止并发送（Ctrl+C 取消）[/dim]"
    )
    try:
        await _read_line("")
    except (KeyboardInterrupt, EOFError):
        await recorder.cancel()
        console.print("[dim]已取消录音。[/dim]")
        return None

    data = await recorder.stop()
    if not data:
        console.print("[yellow]未录到音频。[/yellow]")
        return None

    console.print("[dim]转写中…[/dim]")
    audio = SttAudio(data=data, filename="voice.wav", mime_type="audio/wav")
    try:
        result = await transcribe_audio(config, audio)
    except VoiceCaptureError as exc:
        console.print(f"[red]转写失败：{exc}[/red]")
        return None
    if result is None or not (result.text or "").strip():
        console.print("[yellow]未识别到语音内容。[/yellow]")
        return None
    return result.text.strip()


__all__ = ["capture_voice_text"]
