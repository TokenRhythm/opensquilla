import json
import os
import sys
from pathlib import Path

from .webpage_source import (
    REQUIRED_SOURCE_KEYS,
    WebpageSourceParseError,
    WebpageSourcePayloadError,
    load_source_payload,
    missing_required_keys,
)

workspace = Path(os.environ["WORKSPACE_DIR"]).expanduser().resolve()
project_root = Path(os.environ["PROJECT_ROOT"]).expanduser().resolve()
project_root.relative_to(workspace)

raw_env = os.environ.get("WEBPAGE_SOURCE_JSON")
raw_payload_source = raw_env if raw_env is not None else (
    "" if sys.stdin.isatty() else sys.stdin.read()
)
try:
    data = load_source_payload(raw_payload_source)
except WebpageSourcePayloadError as exc:
    raise SystemExit(f"WEBPAGE_WRITE_FAILED: {exc}") from exc
except WebpageSourceParseError as exc:
    if str(exc) == "source must be a JSON object":
        raise SystemExit("WEBPAGE_WRITE_FAILED: source must be a JSON object") from exc
    raise SystemExit(f"WEBPAGE_WRITE_FAILED: invalid source JSON: {exc}") from exc

missing = missing_required_keys(data)
if missing:
    raise SystemExit("WEBPAGE_WRITE_FAILED: missing keys " + ",".join(missing))

project_dir = project_root / "project"
for rel in [
    "assets/images",
    "assets/audio",
    "assets/video",
]:
    (project_dir / rel).mkdir(parents=True, exist_ok=True)

written = []
for key, filename in REQUIRED_SOURCE_KEYS.items():
    target = (project_dir / filename).resolve()
    target.relative_to(project_dir.resolve())
    target.write_text(str(data[key]), encoding="utf-8")
    written.append(str(target))

print(json.dumps({
    "status": "WEBPAGE_FILES_WRITTEN",
    "project_root": str(project_root),
    "files": written,
    "summary": str(data.get("summary", ""))[:500],
}, ensure_ascii=True, separators=(",", ":")))
