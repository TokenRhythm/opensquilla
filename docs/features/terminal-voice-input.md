# Terminal Voice Input (TUI /voice)

Date: 2026-11-13

## Goal

Let terminal users speak a prompt instead of typing it (PRD F1.4 / PR-4). The
chat TUI gains a `/voice` command that records the microphone, transcribes the
audio through the same STT chain the WebUI uses, and sends the text as a user
turn.

## Usage

In `opensquilla chat`:

```
/voice
```

1. Recording starts **immediately** — just speak.
2. Press **Enter** to stop and send (or **Ctrl+C** to cancel).
3. The audio is transcribed and the text is sent as your message.

Requires `[audio].enabled = true` and a configured STT provider (`[audio.stt]`,
e.g. local SenseVoice or a cloud provider). Microphone capture uses **ffmpeg**
(already a SenseVoice dependency), so no extra runtime package is needed.

## Architecture

- `audio/voice_capture.py`
  - `MicrophoneRecorder` — start/stop ffmpeg capture to 16 kHz mono WAV.
    `start` detects an immediate device-open failure; `stop` finalizes
    gracefully (`q`) and returns the bytes; `cancel` aborts and discards.
  - `list_capture_devices` / `resolve_device` — best-effort device discovery
    (Windows dshow; parses both the new `(audio)`-tagged and legacy section
    formats), falling back to the OS default.
  - `transcribe_audio` — iterates `build_stt_chain(config)` with the WebUI's
    semantics: first non-empty result wins, an empty transcription is a miss,
    an all-empty chain returns `None`.
- `cli/tui/adapters/voice.py` — `capture_voice_text` starts recording immediately
  on `/voice`, waits for Enter to stop, and returns the transcribed text.
- `/voice` is registered in `engine/commands.py` and dispatched in both the
  gateway and standalone slash adapters, submitting via the normal turn stream.

## Full-screen TUI hotkey (Alt+V push-to-talk)

In the full-screen OpenTUI host (`opensquilla chat --ui tui`), **hold Alt+V to
record, release to transcribe**; the text is filled into the composer for review
before sending.

- The host enables the Kitty keyboard protocol (`useKittyKeyboard.events`) so it
  receives key **release** events. Alt+V key-down sends `input.voice`
  `action:"press"`; key-up sends `action:"release"`. Held-key auto-repeat is
  ignored.
- Python (`OpenTuiSurface`) starts the microphone on press and stops +
  transcribes on release, then sends `composer.set` to fill the input box and
  `notice.write` for the recording/transcribing indicator.
- **Raw-terminal fallback:** terminals without Kitty support never send release
  events, so a second Alt+V press toggles capture off (press-to-start,
  press-to-stop). A stray release with nothing recording is a no-op.
- Run the source host with `OPENSQUILLA_TUI_DEV_SOURCE_HOST=1` (requires Bun
  1.3.14 and `bun install` in `cli/tui/opentui/package`).

## Configuration

See `[audio.capture]` in `opensquilla.toml.example`: `device` (empty =
auto-detect), `max_seconds` (hard stop, 1–600), `auto_send` (reserved).

## Scope and follow-ups

- Two surfaces share one capture/STT core (`audio/voice_capture.py`):
  - **plain/native terminal**: command-driven `/voice` (record immediately,
    Enter to stop and send). No raw keyboard listener, so it never fights the
    terminal for input rights (PRD §12.4).
  - **full-screen OpenTUI host**: Alt+V hold-to-talk (Kitty release) with a
    raw-terminal toggle fallback; fills the composer for edit-before-send.
- A live recording-level meter is a possible refinement (the indicator is
  currently a text notice).
- `auto_send` (submit without review) is reserved; the hotkey fills the composer
  and `/voice` sends directly.

## Testing

`tests/test_voice_capture.py` covers the chain fallback semantics, device-list
parsing (both ffmpeg formats), recorder command construction and stop/read, and
the `/voice` helper guards. `tests/unit/cli/tui/test_opentui_voice.py` covers the
Alt+V handler: hold/release (Kitty) flow, raw-terminal toggle fallback,
stray-release no-op, disabled-audio and empty-transcription notices, and
`input.voice` action parsing. The host JS suite (349 tests) covers the Kitty
renderer contract and key handling. The capture→transcribe path was verified
end-to-end with a synthetic WAV through the real SenseVoice provider, and with a
real microphone. Interactive hold-release requires a real terminal (TTY).
