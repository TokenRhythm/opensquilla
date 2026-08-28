# Background Music Library

Audio files in this directory are bundled into the Control UI build (and the
desktop DMG) and served alongside the app's static assets. **No audio files or
personal playlists are committed to the repository** — `*.mp3` / `*.m4a` /
`*.ogg` / `*.flac` / `*.wav` and `playlist.local.json` here are gitignored.
Drop in your own legally-obtained files locally.

## Adding tracks

1. Copy the audio file into this directory, e.g. `my-track.mp3`.
2. Create `playlist.local.json` (gitignored; when present it replaces the
   tracked `playlist.json`, which ships deliberately empty):

```json
{
  "tracks": [
    { "id": "my-track", "title": "My Track", "src": "my-track.mp3" },
    { "id": "my-stream", "title": "Some stream", "src": "https://example.com/track.mp3" }
  ]
}
```

- `id` — stable unique key; the last-selected track is remembered by id.
- `title` — display name in the picker menu.
- `src` — either a filename relative to this directory (bundled at build
  time) or an absolute `https://` URL (streamed at runtime). Other absolute
  URL schemes, root-relative paths, path traversal (`.` / `..`, including
  encoded or backslash forms), and scheme-relative URLs are ignored. Nested
  subdirectories such as `album/track.mp3` are supported.

Playing an HTTPS stream contacts that third-party host directly and exposes
ordinary request metadata to it, including your IP address and browser user
agent.

The first entry in `tracks` is the default selection. A track whose file is
missing simply fails to play; the music control itself always works and can
also play an ad-hoc local file via "Choose local file…".

The player is off by default — enable it under Settings → Appearance →
Background music, or via the command palette.

## Copyright

Do not commit or publicly distribute builds containing copyrighted music you
do not have redistribution rights for. Bundling music for personal use of
your own build is your responsibility. Standard source archives reject these
ignored files to reduce accidental disclosure; use a direct local wheel or
local Docker image for a private customized artifact.
