import json
import os
import re
import sys


def load_payload():
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


payload = load_payload()

outline = str(payload.get("page_outline") or os.environ.get("PAGE_OUTLINE", ""))
requirement = str(
    payload.get("requirement_framing") or os.environ.get("REQUIREMENT_FRAMING", "")
)
visual_style = str(payload.get("visual_style") or os.environ.get("VISUAL_STYLE", ""))


def wants(name):
    payload_name = {
        "INCLUDE_IMAGE": "include_image",
        "INCLUDE_AUDIO": "include_audio",
        "INCLUDE_VIDEO": "include_video",
    }.get(name)
    raw = payload.get(payload_name) if payload_name else None
    value = str(raw if raw is not None else os.environ.get(name, "YES")).strip().lower()
    return value not in {"no", "false", "0", "n", "否", "不要", "不需要"}

include_image = wants("INCLUDE_IMAGE")
include_audio = wants("INCLUDE_AUDIO")
include_video = wants("INCLUDE_VIDEO")
allowed = set()
if include_image:
    allowed.add("image")
if include_audio:
    allowed.add("audio")
if include_video:
    allowed.add("video")


def clean_slot(raw, fallback):
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(raw or "").strip()).strip("-").lower()
    return (text or fallback)[:64]

def field(block, *names):
    for name in names:
        match = re.search(rf"{name}\s*[:：]\s*(.+)", block, re.I)
        if match:
            return match.group(1).strip().strip("`\"'")[:500]
    return ""

def modality(value):
    text = str(value or "").strip().lower()
    raw = str(value or "")
    if re.search(r"\bimage\b", text) or any(x in raw for x in ("图片", "图像", "视觉", "插图")):
        return "image"
    if re.search(r"\baudio\b|\bsound\b|voice|narration", text) or any(x in raw for x in ("音频", "声音", "旁白")):
        return "audio"
    if re.search(r"\bvideo\b|motion|film", text) or any(x in raw for x in ("视频", "影片", "短片")):
        return "video"
    return ""

def split_keywords(value):
    if isinstance(value, list):
        return [str(x).strip()[:80] for x in value if str(x).strip()][:8]
    text = str(value or "").strip()
    if not text:
        return []
    return [x.strip()[:80] for x in re.split(r"[,，;/、]+", text) if x.strip()][:8]

def truthy(value):
    return str(value or "").strip().lower() in {"true", "yes", "1", "y", "是", "需要"}

def append(slots, seen, *, slot_id, kind, placement="", subject="", prompt_hint="", keywords="", load_bearing=False, source="outline"):
    if kind not in allowed:
        return
    slot = clean_slot(slot_id, f"{kind}-{len(slots) + 1}")
    if slot in seen:
        return
    seen.add(slot)
    slots.append({
        "slot_id": slot,
        "modality": kind,
        "placement": str(placement or "").strip()[:240],
        "subject": str(subject or placement or slot.replace("-", " ")).strip()[:500],
        "prompt_hint": str(prompt_hint or "").strip()[:500],
        "search_keywords": split_keywords(keywords),
        "load_bearing": bool(load_bearing),
        "source": source,
    })

def split_table_row(line):
    stripped = line.strip()
    if not stripped.startswith("|") or "|" not in stripped[1:]:
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]

def separator(cells):
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell.strip()) for cell in cells)

def hindex(headers, *needles):
    normalized = [re.sub(r"[\s_\-/]+", "", h.strip().lower()) for h in headers]
    for needle in needles:
        n = re.sub(r"[\s_\-/]+", "", needle.strip().lower())
        for index, header in enumerate(normalized):
            if n and n in header:
                return index
    return None

def cell(cells, index):
    if index is None or index >= len(cells):
        return ""
    return cells[index].strip().strip("`\"'")

slots: list[dict[str, object]] = []
seen: set[str] = set()

matches = list(re.finditer(r"slot_id\s*[:：]\s*([a-zA-Z0-9][a-zA-Z0-9_-]{0,80})", outline, re.I))
for index, match in enumerate(matches):
    end = matches[index + 1].start() if index + 1 < len(matches) else min(len(outline), match.start() + 900)
    block = outline[match.start():end]
    kind = modality(field(block, "modality", "media", "type", "类型", "媒体", "模态"))
    append(
        slots,
        seen,
        slot_id=match.group(1),
        kind=kind,
        placement=field(block, "placement", "section", "位置", "章节"),
        subject=field(block, "subject", "画面主题", "主题", "description", "描述"),
        prompt_hint=field(block, "prompt_hint", "prompt", "hint", "构图", "描述"),
        keywords=field(block, "search_keywords", "keywords", "关键词"),
        load_bearing=truthy(field(block, "load_bearing", "required", "必要")),
    )

headers: list[str] | None = None
for raw_line in outline.splitlines():
    cells = split_table_row(raw_line)
    if not cells:
        headers = None
        continue
    if separator(cells):
        continue
    if headers is None:
        lower = [c.lower() for c in cells]
        has_slot = any("slot" in c or "槽位" in c for c in lower + cells)
        has_media = any("modality" in c or "media" in c or "媒体" in c or "模态" in c or "类型" in c for c in lower + cells)
        if has_slot and has_media:
            headers = cells
        continue
    slot_idx = hindex(headers, "slot_id", "slot id", "slot", "槽位")
    modality_idx = hindex(headers, "modality", "media", "媒体", "模态", "类型")
    if slot_idx is None or modality_idx is None:
        continue
    kind = modality(cell(cells, modality_idx))
    append(
        slots,
        seen,
        slot_id=cell(cells, slot_idx),
        kind=kind,
        placement=cell(cells, hindex(headers, "placement", "section", "role", "位置", "章节", "角色")),
        subject=cell(cells, hindex(headers, "subject", "visual", "description", "画面", "主题", "描述")),
        prompt_hint=cell(cells, hindex(headers, "prompt_hint", "prompt", "hint", "构图", "风格")),
        keywords=cell(cells, hindex(headers, "search_keywords", "keywords", "关键词")),
        load_bearing=truthy(cell(cells, hindex(headers, "load_bearing", "required", "必要"))),
    )

for line in outline.splitlines():
    kind = modality(line)
    if not kind:
        continue
    line_match = re.search(r"slot[_\s-]*id\s*[:：=]\s*([a-zA-Z0-9][a-zA-Z0-9_-]{0,80})", line, re.I)
    if not line_match:
        continue
    append(slots, seen, slot_id=line_match.group(1), kind=kind, subject=line[:500], prompt_hint=line[:500])

def topic():
    text = re.sub(r"[#*_`|>{}\[\]\"]+", " ", requirement + "\n" + outline)
    text = re.sub(r"\s+", " ", text).strip()
    for marker in ("主题", "topic", "brief", "request", "需求"):
        match = re.search(rf"{marker}\s*[:：]\s*([^。.\n|]{{6,120}})", text, re.I)
        if match:
            return match.group(1).strip()[:160]
    return (text[:160] if text else "requested webpage topic")

synthesized: list[str] = []
image_count = sum(1 for slot in slots if slot["modality"] == "image")
if include_image and image_count == 0:
    base = topic()
    synthetic = [
        ("hero-visual", "hero", f"Primary webpage hero visual for {base}", "wide 16:9 polished webpage hero, no text overlays"),
        ("supporting-visual", "body section", f"Supporting explanatory visual for {base}", "clean editorial webpage image, no text overlays"),
    ]
    for slot_id, placement, subject, hint in synthetic:
        append(
            slots,
            seen,
            slot_id=slot_id,
            kind="image",
            placement=placement,
            subject=subject,
            prompt_hint=f"{hint}. Visual style: {visual_style}".strip(),
            keywords=base,
            load_bearing=True,
            source="synthesized",
        )
        synthesized.append(slot_id)

if include_audio and not any(slot["modality"] == "audio" for slot in slots):
    append(slots, seen, slot_id="narration-audio", kind="audio", placement="page narration", subject=f"Narration or soundscape for {topic()}", source="synthesized")
    synthesized.append("narration-audio")
if include_video and not any(slot["modality"] == "video" for slot in slots):
    append(slots, seen, slot_id="intro-video", kind="video", placement="intro section", subject=f"Short intro video for {topic()}", source="synthesized")
    synthesized.append("intro-video")

counts = {kind: sum(1 for slot in slots if slot["modality"] == kind) for kind in ("image", "audio", "video")}
print(json.dumps({
    "status": "MEDIA_SLOTS_READY",
    "slots": slots,
    "counts": counts,
    "synthesized": synthesized,
    "policy": "Downstream media producers must use slots[] as the authoritative shopping list.",
}, ensure_ascii=True, separators=(",", ":")))
