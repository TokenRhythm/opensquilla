import html
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path

project_root = Path(os.environ["PROJECT_ROOT"]).expanduser().resolve()
project_dir = project_root / "project"
index_path = project_dir / "index.html"
style_path = project_dir / "style.css"
script_path = project_dir / "script.js"

def requested(name):
    return os.environ.get(f"INCLUDE_{name.upper()}", "YES") == "YES"

def normalize_path(value):
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in Path(raw).parts:
        return None, None
    if raw.startswith("project/"):
        src = raw[len("project/"):]
        disk = project_root / raw
    else:
        src = raw
        disk = project_dir / raw
    if not raw.startswith(("assets/images/", "assets/audio/", "assets/video/")):
        if not src.startswith(("assets/images/", "assets/audio/", "assets/video/")):
            return None, None
    return src, disk


def normalize_browser_src(value):
    src = str(value or "").strip().replace("\\", "/")
    while src.startswith("./"):
        src = src[2:]
    src = src.split("?", 1)[0].split("#", 1)[0]
    return src or None


def srcset_sources(value):
    sources = []
    for part in str(value or "").split(","):
        candidate = part.strip().split()
        if not candidate:
            continue
        src = normalize_browser_src(candidate[0])
        if src:
            sources.append(src)
    return sources


def css_url_sources(value):
    sources = []
    for match in re.finditer(r"url\(\s*['\"]?([^'\"\)]+)['\"]?\s*\)", str(value or ""), re.I):
        src = normalize_browser_src(match.group(1))
        if src:
            sources.append(src)
    return sources


class MediaSourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = {"image": set(), "audio": set(), "video": set()}
        self.media_stack = []
        self.picture_depth = 0
        self.style_depth = 0
        self.style_chunks = []

    def add_sources(self, kind, *values):
        for value in values:
            src = normalize_browser_src(value)
            if src:
                self.sources[kind].add(src)

    def add_srcset_sources(self, kind, value):
        for src in srcset_sources(value):
            self.sources[kind].add(src)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr_map = {name.lower(): value for name, value in attrs}
        for src in css_url_sources(attr_map.get("style")):
            self.sources["image"].add(src)
        if tag == "style":
            self.style_depth += 1
        elif tag == "picture":
            self.picture_depth += 1
        elif tag == "img":
            self.add_sources("image", attr_map.get("src"))
            self.add_srcset_sources("image", attr_map.get("srcset"))
        elif tag in {"audio", "video"}:
            self.media_stack.append(tag)
            self.add_sources(tag, attr_map.get("src"))
        elif tag == "source" and self.media_stack:
            self.add_sources(self.media_stack[-1], attr_map.get("src"))
        elif tag == "source" and self.picture_depth:
            self.add_sources("image", attr_map.get("src"))
            self.add_srcset_sources("image", attr_map.get("srcset"))

    def handle_data(self, data):
        if self.style_depth:
            self.style_chunks.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "style" and self.style_depth:
            self.style_depth -= 1
        elif tag == "picture" and self.picture_depth:
            self.picture_depth -= 1
        elif tag in {"audio", "video"}:
            for idx in range(len(self.media_stack) - 1, -1, -1):
                if self.media_stack[idx] == tag:
                    del self.media_stack[idx:]
                    return


def rendered_sources_by_kind(index_html, style_css):
    parser = MediaSourceParser()
    parser.feed(index_html)
    for src in css_url_sources(style_css):
        parser.sources["image"].add(src)
    for src in css_url_sources("\n".join(parser.style_chunks)):
        parser.sources["image"].add(src)
    return parser.sources


def collect_assets():
    ready_re = re.compile(r"^(IMAGE|AUDIO|VIDEO)_READY:\s*(\{.*\})\s*$", re.M)
    fail_re = re.compile(r"^(IMAGE|AUDIO|VIDEO)_(CONFIG_NEEDED|GENERATION_FAILED|MODEL_UNSUPPORTED):\s*(\{.*\})\s*$", re.M)
    sources = {
        "image_download": os.environ.get("IMAGE_DOWNLOAD", ""),
        "image_aigc": os.environ.get("IMAGE_AIGC", ""),
        "audio_aigc": os.environ.get("AUDIO_AIGC", ""),
        "video_aigc": os.environ.get("VIDEO_AIGC", ""),
    }
    assets = []
    generation_failures = []
    seen = set()
    for source, text in sources.items():
        for match in ready_re.finditer(text):
            kind = match.group(1).lower()
            try:
                payload = json.loads(match.group(2))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            src, disk = normalize_path(payload.get("local_path"))
            if src is None or disk is None or not disk.is_file() or disk.stat().st_size <= 0:
                continue
            key = (kind, src)
            if key in seen:
                continue
            seen.add(key)
            assets.append({
                "kind": kind,
                "src": src,
                "bytes": disk.stat().st_size,
                "subject": str(payload.get("subject") or payload.get("slot_id") or payload.get("prompt_preview") or payload.get("script_preview") or src),
            })
        for match in fail_re.finditer(text):
            try:
                payload = json.loads(match.group(3))
            except json.JSONDecodeError:
                payload = {}
            replacement_src, _ = normalize_path(payload.get("replacement_slot"))
            generation_failures.append({
                "kind": match.group(1).lower(),
                "label": f"{match.group(1)}_{match.group(2)}",
                "source_step": source,
                "reason": payload.get("reason") or payload.get("status") or payload.get("phase"),
                "missing": payload.get("missing", []),
                "replacement_slot": payload.get("replacement_slot"),
                "replacement_src": replacement_src,
            })
    return assets, generation_failures

failures = []
for path, label in [
    (index_path, "project/index.html"),
    (style_path, "project/style.css"),
    (script_path, "project/script.js"),
]:
    if not path.is_file():
        failures.append({"kind": "page", "reason": "missing_authored_file", "path": label})
if failures:
    print(json.dumps({"status": "MEDIA_BIND_FAILED", "failures": failures}, ensure_ascii=True, separators=(",", ":")))
    raise SystemExit("MEDIA_BIND_FAILED")

assets, generation_failures = collect_assets()
index_html = index_path.read_text(encoding="utf-8")
style_css = style_path.read_text(encoding="utf-8")
script_js = script_path.read_text(encoding="utf-8")
combined = "\n".join([index_html, style_css, script_js])
lower_html = index_html.lower()
rendered_sources = rendered_sources_by_kind(index_html, style_css)

repair_map = {}
for asset in assets:
    if asset["src"] not in combined:
        repair_map[(asset["kind"], asset["src"])] = asset
image_assets = [asset for asset in assets if asset["kind"] == "image"]
audio_assets = [asset for asset in assets if asset["kind"] == "audio"]
video_assets = [asset for asset in assets if asset["kind"] == "video"]
for asset in image_assets:
    if asset["src"] not in rendered_sources["image"]:
        repair_map[(asset["kind"], asset["src"])] = asset
for asset in audio_assets:
    if asset["src"] not in rendered_sources["audio"]:
        repair_map[(asset["kind"], asset["src"])] = asset
for asset in video_assets:
    if asset["src"] not in rendered_sources["video"]:
        repair_map[(asset["kind"], asset["src"])] = asset

if repair_map:
    repair_assets = list(repair_map.values())
    images = [asset for asset in repair_assets if asset["kind"] == "image"]
    audio = [asset for asset in repair_assets if asset["kind"] == "audio"]
    video = [asset for asset in repair_assets if asset["kind"] == "video"]
    blocks = [
        '<section id="generated-media-assets" class="generated-media-assets" aria-labelledby="generated-media-title">',
        '  <div class="generated-media-assets__inner">',
        '    <p class="generated-media-assets__eyebrow">Generated media</p>',
        '    <h2 id="generated-media-title">本地生成媒体</h2>',
        '    <p class="generated-media-assets__intro">以下素材已生成并绑定到页面，可直接播放或替换同名文件。</p>',
    ]
    for asset in audio:
        label = html.escape(asset["subject"][:120])
        src = html.escape(asset["src"], quote=True)
        blocks.extend([
            '    <article class="generated-media-card generated-media-card--audio">',
            '      <div><h3>音频导览</h3>',
            f'      <p>{label}</p></div>',
            f'      <audio controls preload="metadata" src="{src}"></audio>',
            '    </article>',
        ])
    for asset in video:
        label = html.escape(asset["subject"][:120])
        src = html.escape(asset["src"], quote=True)
        blocks.extend([
            '    <article class="generated-media-card generated-media-card--video">',
            '      <div><h3>视频片段</h3>',
            f'      <p>{label}</p></div>',
            f'      <video controls playsinline preload="metadata" src="{src}"></video>',
            '    </article>',
        ])
    if images:
        blocks.append('    <div class="generated-media-gallery" aria-label="生成图片素材">')
        for asset in images:
            label = html.escape(asset["subject"][:120])
            src = html.escape(asset["src"], quote=True)
            blocks.extend([
                '      <figure>',
                f'        <img loading="lazy" src="{src}" alt="{label}">',
                f'        <figcaption>{label}</figcaption>',
                '      </figure>',
            ])
        blocks.append('    </div>')
    blocks.extend(['  </div>', '</section>'])
    repair_html = "\n".join(blocks)
    insert_idx = lower_html.rfind("</main>")
    if insert_idx < 0:
        insert_idx = lower_html.rfind("</body>")
    if insert_idx >= 0:
        index_html = index_html[:insert_idx] + repair_html + "\n" + index_html[insert_idx:]
    else:
        index_html += "\n" + repair_html + "\n"
    if ".generated-media-assets" not in style_css:
        style_css += """

.generated-media-assets {
  background: #f5f7fb;
  color: #15202b;
  padding: clamp(2rem, 6vw, 5rem) clamp(1rem, 4vw, 3rem);
}
.generated-media-assets__inner {
  max-width: 1120px;
  margin: 0 auto;
}
.generated-media-assets__eyebrow {
  margin: 0 0 .5rem;
  color: #0f766e;
  font-size: .78rem;
  font-weight: 700;
  text-transform: uppercase;
}
.generated-media-assets h2 {
  margin: 0 0 1rem;
  font-size: clamp(1.75rem, 4vw, 3.25rem);
  line-height: 1.08;
}
.generated-media-assets__intro {
  max-width: 64ch;
  margin: 0 0 2rem;
  color: #475569;
  line-height: 1.75;
}
.generated-media-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(280px, 460px);
  gap: 1.5rem;
  align-items: center;
  padding: clamp(1rem, 3vw, 2rem);
  margin-bottom: 1.5rem;
  border: 1px solid rgba(15, 23, 42, .12);
  border-radius: 8px;
  background: #fff;
}
.generated-media-card audio,
.generated-media-card video {
  width: 100%;
}
.generated-media-gallery {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}
.generated-media-gallery figure {
  margin: 0;
  overflow: hidden;
  border-radius: 8px;
  background: #fff;
}
.generated-media-gallery img {
  display: block;
  width: 100%;
  aspect-ratio: 4 / 3;
  object-fit: cover;
}
.generated-media-gallery figcaption {
  padding: .85rem 1rem 1rem;
  color: #475569;
  line-height: 1.55;
}
@media (max-width: 760px) {
  .generated-media-card {
    grid-template-columns: 1fr;
  }
}
"""
    index_path.write_text(index_html, encoding="utf-8")
    style_path.write_text(style_css, encoding="utf-8")

index_html = index_path.read_text(encoding="utf-8")
style_css = style_path.read_text(encoding="utf-8")
script_js = script_path.read_text(encoding="utf-8")
combined = "\n".join([index_html, style_css, script_js])
lower_html = index_html.lower()
rendered_sources = rendered_sources_by_kind(index_html, style_css)
assets_by_kind = {"image": [], "audio": [], "video": []}
for asset in assets:
    assets_by_kind[asset["kind"]].append(asset)
degraded_by_kind = {"image": [], "audio": [], "video": []}
fatal_generation_failures_by_kind = {"image": [], "audio": [], "video": []}
for failure in generation_failures:
    kind = failure.get("kind")
    label = str(failure.get("label") or "")
    if kind in degraded_by_kind:
        if label.endswith(("_CONFIG_NEEDED", "_MODEL_UNSUPPORTED")):
            degraded_by_kind[kind].append(failure)
        else:
            fatal_generation_failures_by_kind[kind].append(failure)

required = {"image": requested("image"), "audio": requested("audio"), "video": requested("video")}
for kind, is_required in required.items():
    if not is_required:
        continue
    if not assets_by_kind[kind]:
        if degraded_by_kind[kind] and not fatal_generation_failures_by_kind[kind]:
            continue
        failures.append({"kind": kind, "reason": "requested_modality_has_no_ready_asset"})
        continue
    for asset in assets_by_kind[kind]:
        if asset["src"] not in combined:
            failures.append({"kind": kind, "reason": "asset_not_referenced_by_page", "src": asset["src"]})
    if kind == "image":
        for asset in assets_by_kind[kind]:
            if asset["src"] not in rendered_sources[kind]:
                failures.append({
                    "kind": kind,
                    "reason": "image_render_missing_or_unbound",
                    "src": asset["src"],
                })
    if kind in {"audio", "video"}:
        for asset in assets_by_kind[kind]:
            if asset["src"] not in rendered_sources[kind]:
                failures.append({
                    "kind": kind,
                    "reason": f"{kind}_control_missing_or_unbound",
                    "src": asset["src"],
                })
requested_degraded = {
    kind: items
    for kind, items in degraded_by_kind.items()
    if required[kind] and items
}
partial_generation_failures = {
    kind: items
    for kind, items in fatal_generation_failures_by_kind.items()
    if required[kind] and assets_by_kind[kind] and items
}
reported_degraded = {
    kind: requested_degraded.get(kind, []) + partial_generation_failures.get(kind, [])
    for kind in assets_by_kind
    if requested_degraded.get(kind) or partial_generation_failures.get(kind)
}

report = {
    "status": "MEDIA_BIND_FAILED" if failures else (
        "MEDIA_BIND_DEGRADED" if reported_degraded else "MEDIA_BIND_OK"
    ),
    "requested": required,
    "ready_counts": {kind: len(items) for kind, items in assets_by_kind.items()},
    "referenced": {
        kind: [asset["src"] for asset in items if asset["src"] in combined]
        for kind, items in assets_by_kind.items()
    },
    "patched_assets": [asset["src"] for asset in repair_map.values()],
    "degraded": reported_degraded,
    "partial_generation_failures": partial_generation_failures,
    "generation_failures": generation_failures,
    "failures": failures,
}
print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
if failures:
    raise SystemExit("MEDIA_BIND_FAILED")
