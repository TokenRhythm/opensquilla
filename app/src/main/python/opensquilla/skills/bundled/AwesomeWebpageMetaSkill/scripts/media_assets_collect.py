import json
import os
import re
from pathlib import Path

project_root = Path(os.environ["PROJECT_ROOT"]).expanduser().resolve()
project_parent = project_root.parent

ready_re = re.compile(r"^(IMAGE|AUDIO|VIDEO)_READY:\s*(\{.*\})\s*$", re.M)
fail_re = re.compile(r"^(IMAGE|AUDIO|VIDEO)_(CONFIG_NEEDED|GENERATION_FAILED|MODEL_UNSUPPORTED):\s*(\{.*\})\s*$", re.M)
sources = {
    "image_download": os.environ.get("IMAGE_DOWNLOAD", ""),
    "image_aigc": os.environ.get("IMAGE_AIGC", ""),
    "audio_aigc": os.environ.get("AUDIO_AIGC", ""),
    "video_aigc": os.environ.get("VIDEO_AIGC", ""),
}

def load_payload(raw):
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None

def normalize_path(value):
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in Path(raw).parts:
        return None, None
    if raw.startswith("project/"):
        src = raw[len("project/"):]
        disk = project_parent / raw
    else:
        src = raw
        disk = project_root / raw
    if not src.startswith(("assets/images/", "assets/audio/", "assets/video/")):
        return None, None
    return src, disk

assets = []
missing_assets = []
generation_failures = []
invalid_records = []

for source, text in sources.items():
    for match in ready_re.finditer(text):
        kind = match.group(1).lower()
        payload = load_payload(match.group(2))
        if payload is None:
            invalid_records.append({"source": source, "kind": kind, "reason": "invalid_ready_json"})
            continue
        src, disk = normalize_path(payload.get("local_path"))
        if src is None or disk is None:
            invalid_records.append({"source": source, "kind": kind, "reason": "invalid_local_path", "local_path": payload.get("local_path")})
            continue
        record = {
            "kind": kind,
            "src": src,
            "local_path": payload.get("local_path"),
            "source_step": source,
            "mime": payload.get("mime"),
            "slot_id": payload.get("slot_id"),
            "subject": payload.get("subject") or payload.get("prompt_preview") or payload.get("script_preview"),
        }
        if disk.is_file():
            size = disk.stat().st_size
            record["bytes"] = size
            assets.append(record)
        else:
            record["reason"] = "file_missing"
            missing_assets.append(record)

    for match in fail_re.finditer(text):
        kind = match.group(1).lower()
        label = f"{match.group(1)}_{match.group(2)}"
        payload = load_payload(match.group(3)) or {}
        replacement_src, _ = normalize_path(payload.get("replacement_slot"))
        generation_failures.append({
            "kind": kind,
            "label": label,
            "source_step": source,
            "replacement_src": replacement_src,
            "missing": payload.get("missing", []),
            "reason": payload.get("reason") or payload.get("status") or payload.get("phase"),
        })

manifest = {
    "project_root": str(project_root),
    "assets": assets,
    "missing_assets": missing_assets,
    "generation_failures": generation_failures,
    "invalid_records": invalid_records,
    "path_policy": "Use assets[].src for HTML/CSS/JS references. Do not use raw project/assets local_path values as browser src paths.",
}
print(json.dumps(manifest, ensure_ascii=True, separators=(",", ":")))
