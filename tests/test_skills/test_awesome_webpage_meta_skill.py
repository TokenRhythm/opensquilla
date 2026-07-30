from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from opensquilla.skills.eligibility import EligibilityContext, check_eligibility
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.templating import evaluate_when, render_with_args

REPO = Path(__file__).resolve().parents[2]
BUNDLED = REPO / "src" / "opensquilla" / "skills" / "bundled"
SKILL_MD = BUNDLED / "AwesomeWebpageMetaSkill" / "SKILL.md"
AWESOME_MODULE = "opensquilla.skills.bundled.AwesomeWebpageMetaSkill.scripts"


def _frontmatter() -> dict:
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    assert match is not None
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict)
    return data


def _load_openrouter_video_module():
    script = (
        BUNDLED
        / "openrouter-video-generator"
        / "scripts"
        / "openrouter_video.py"
    )
    spec = importlib.util.spec_from_file_location("openrouter_video", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_openrouter_audio_module():
    script = BUNDLED / "audio-cog" / "scripts" / "openrouter_audio.py"
    spec = importlib.util.spec_from_file_location("openrouter_audio", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_openrouter_image_module():
    script = (
        BUNDLED
        / "nano-banana-pro-openrouter"
        / "scripts"
        / "openrouter_image.py"
    )
    spec = importlib.util.spec_from_file_location("openrouter_image", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_awesome_image_download_module():
    script = BUNDLED / "awesome-webpage-image-download" / "scripts" / "image_download.py"
    spec = importlib.util.spec_from_file_location("awesome_image_download", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_awesome_webpage_meta_skill_loads_and_references_fixed_skills(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    loader.invalidate_cache()

    spec = loader.get_by_name("AwesomeWebpageMetaSkill")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None
    assert plan.final_text_mode == "step:delivery_guide"

    refs = {step.skill for step in plan.steps if step.kind in {"agent", "skill_exec"}}
    assert {
        "awesome-webpage-research",
        "awesome-webpage-image-download",
        "web-search",
        "html-coder",
        "nano-banana-pro-openrouter",
        "audio-cog",
        "openrouter-video-generator",
        "filesystem",
    }.issubset(refs)
    assert "awesome-webpage-generator" not in refs
    assert "deep-research" not in refs

    assert loader.get_by_name("web-search-cn") is None
    assert loader.get_by_name("audio") is None
    assert loader.get_by_name("awesome-webpage-generator") is None
    html_coder = loader.get_by_name("html-coder")
    assert html_coder is not None
    assert html_coder.provenance.origin == "clawhub-mit0"
    mini_research = loader.get_by_name("awesome-webpage-research")
    assert mini_research is not None
    assert mini_research.user_invocable is False
    assert mini_research.disable_model_invocation is True
    video_generator = loader.get_by_name("openrouter-video-generator")
    assert video_generator is not None
    assert video_generator.user_invocable is False
    assert video_generator.disable_model_invocation is True
    filesystem = loader.get_by_name("filesystem")
    assert filesystem is not None
    assert filesystem.metadata is not None
    assert filesystem.metadata.requires is not None
    assert filesystem.metadata.requires.bins == []


def test_webpage_skills_publish_only_the_dedicated_project_bundle() -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    publish = steps["publish_webpage"]
    project_root = (
        "{{ inputs.get('config', {}).get('awesome_webpage', {}).get('output_dir') "
        "or (inputs.workspace_dir ~ '/awesome-webpage-output') }}/"
        "{{ outputs.project_slug | slugify }}/project"
    )

    assert publish["kind"] == "tool_call"
    assert publish["tool"] == "publish_artifact"
    assert publish["tool_allowlist"] == ["publish_artifact"]
    assert publish["depends_on"] == ["quick_validate"]
    assert publish["tool_args"] == {
        "path": f"{project_root}/index.html",
        "mime": "text/html",
        "bundle": "directory",
        "bundle_root": project_root,
    }
    assert steps["delivery_guide"]["depends_on"] == [
        "quick_validate",
        "publish_webpage",
    ]
    delivery_task = steps["delivery_guide"]["with"]["task"]
    assert "outputs.publish_webpage | truncate(1500)" in delivery_task
    assert "do not invent or print an artifact URL" in delivery_task

    html_coder = (BUNDLED / "html-coder" / "SKILL.md").read_text(encoding="utf-8")
    assert "## OpenSquilla Webpage Delivery" in html_coder
    assert "never place a generated site directly in the\n   workspace root" in html_coder
    assert '"path": "webpage-<topic-slug>/index.html"' in html_coder
    assert '"bundle": "directory"' in html_coder
    assert '"bundle_root": "webpage-<topic-slug>"' in html_coder
    assert "Do not bundle the entire workspace." in html_coder
    assert "source-only output" in html_coder
    assert "AwesomeWebpageMetaSkill" in html_coder


def test_audio_cog_is_openrouter_compatible_without_cellcog_key(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv("CELLCOG_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    loader.invalidate_cache()

    spec = loader.get_by_name("audio-cog")
    assert spec is not None
    assert spec.metadata is not None
    assert spec.metadata.requires is not None
    assert "CELLCOG_API_KEY" not in spec.metadata.requires.env
    assert check_eligibility(spec, EligibilityContext.auto())


def test_awesome_webpage_config_contract_keeps_runtime_values_in_config() -> None:
    fm = _frontmatter()
    cfg = fm["config"]["awesome_webpage"]

    assert cfg["provider"] == "openrouter"
    assert cfg["openrouter"]["api_key"] is None
    assert cfg["openrouter"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert set(cfg["openrouter"]["models"]) == {
        "page_generation",
        "image_generation",
        "audio_generation",
        "video_generation",
    }
    assert cfg["openrouter"]["models"]["page_generation"] == "moonshotai/kimi-k2.6"
    assert cfg["openrouter"]["models"]["image_generation"] == (
        "google/gemini-3-pro-image-preview"
    )
    assert cfg["openrouter"]["models"]["audio_generation"] == "openai/gpt-audio-mini"
    assert cfg["openrouter"]["models"]["video_generation"] == (
        "bytedance/seedance-2.0-fast"
    )
    assert cfg["output_dir"] == "{{ inputs.workspace_dir }}/awesome-webpage-output"
    assert "media_strategy" in cfg

    clawhub = cfg["clawhub_skills"]
    assert clawhub["web_search"]["url"] == "https://clawhub.ai/billyutw/web-search"
    assert "web_search_cn" not in clawhub
    assert clawhub["image_generation"]["url"] == "https://clawhub.ai/skills/nano-banana-pro-openrouter"
    assert clawhub["image_generation"]["skill"] == "nano-banana-pro-openrouter"
    assert clawhub["image_generation"]["opensquilla_compatibility"] == (
        "deterministic-skill-exec"
    )
    assert clawhub["audio_generation"]["url"] == "https://clawhub.ai/skills/audio-cog"
    assert clawhub["audio_generation"]["opensquilla_compatibility"] == (
        "openrouter-config-first"
    )
    assert clawhub["video_generation"]["skill"] == "openrouter-video-generator"
    assert clawhub["webpage_generation"]["skill"] == "html-coder"
    assert clawhub["webpage_generation"]["url"] == "https://clawhub.ai/jhauga/html-coder"
    assert clawhub["webpage_generation"]["opensquilla_compatibility"] == "scoped-agent"
    assert clawhub["filesystem"]["url"] == "https://clawhub.ai/gtrusler/clawdbot-filesystem"


def test_awesome_webpage_media_strategy_covers_video_and_required_modalities() -> None:
    fm = _frontmatter()
    cfg = fm["config"]["awesome_webpage"]
    assert cfg["media_strategy"]["default_modalities"] == ["text", "images", "audio", "video"]
    assert cfg["media_strategy"]["search_modalities"] == ["images"]
    assert cfg["media_strategy"]["direct_aigc_modalities"] == ["audio", "video"]
    assert cfg["media_strategy"]["confirmation_steps"] == [
        "ask_images",
        "ask_audio",
        "ask_video",
        "ask_style",
    ]
    assert cfg["media_strategy"]["aigc_policy"] == "search_images_direct_generate_audio_video"

    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    assert steps["ask_images"]["kind"] == "user_input"
    assert steps["ask_audio"]["kind"] == "user_input"
    assert steps["ask_video"]["kind"] == "user_input"
    assert steps["ask_style"]["kind"] == "user_input"
    assert steps["ask_images"]["depends_on"] == ["requirement_framing"]
    assert steps["ask_audio"]["depends_on"] == ["ask_images"]
    assert steps["ask_video"]["depends_on"] == ["ask_audio"]
    assert steps["ask_style"]["depends_on"] == ["ask_video"]
    assert steps["ask_images"]["clarify"]["fields"][0]["name"] == "include_images"
    assert steps["ask_images"]["clarify"]["fields"][0]["choices"] == ["YES", "NO"]
    assert steps["ask_audio"]["clarify"]["fields"][0]["name"] == "include_audio"
    assert steps["ask_audio"]["clarify"]["fields"][0]["choices"] == ["YES", "NO"]
    assert steps["ask_video"]["clarify"]["fields"][0]["name"] == "include_video"
    assert steps["ask_video"]["clarify"]["fields"][0]["choices"] == ["YES", "NO"]
    assert steps["ask_style"]["clarify"]["fields"][0]["name"] == "visual_style"
    assert steps["deep_research"]["depends_on"] == [
        "requirement_framing",
        "ask_images",
        "ask_audio",
        "ask_video",
        "ask_style",
    ]

    media_strategy = steps["media_strategy"]
    assert set(media_strategy["output_choices"]) == {"IMAGE_SEARCH_READY", "NEEDS_AIGC_IMAGE"}
    assert "Use the confirmed interactive choices as the source of truth" in (
        media_strategy["with"]["text"]
    )
    assert "Search is image-only" in media_strategy["with"]["text"]
    assert "Audio and video are direct AIGC modalities" in media_strategy["with"]["text"]
    media_search_query = steps["media_search"]["with"]["query"]
    assert "Do not search for audio or video" in media_search_query
    assert "at most 2 `web_search` calls" in media_search_query
    assert "arguments `query` and `max_results=6`" in media_search_query
    assert "type=images" not in media_search_query
    assert "NO_USABLE_IMAGE_CANDIDATES" in media_search_query
    assert steps["media_slots_normalize"]["kind"] == "tool_call"
    assert steps["media_slots_normalize"]["depends_on"] == ["page_outline"]
    assert steps["media_slots_normalize"]["tool_args"]["command"].strip() == (
        f"python -m {AWESOME_MODULE}.media_slots_normalize"
    )
    assert steps["media_search"]["depends_on"] == ["media_slots_normalize"]
    assert "outputs.media_slots_normalize | truncate(3500)" in (
        steps["media_search"]["with"]["query"]
    )
    assert "include_images" in steps["media_search"]["when"]
    assert "search_first" in steps["media_search"]["when"]
    assert "search_modalities" in steps["media_search"]["when"]
    assert "media_search_cn" not in steps

    assert steps["media_strategy"]["depends_on"] == ["media_search", "media_slots_normalize"]
    assert "search_first" in media_strategy["with"]["text"]
    assert "search_modalities" in media_strategy["with"]["text"]
    assert steps["video_aigc"]["skill"] == "openrouter-video-generator"
    assert steps["audio_script"]["kind"] == "llm_chat"
    assert steps["audio_script"]["depends_on"] == [
        "requirement_framing",
        "page_outline",
        "media_slots_normalize",
    ]
    audio_script_task = steps["audio_script"]["with"]["task"]
    assert "spoken text only" in audio_script_task
    assert "我明白了" in audio_script_task
    assert "outputs.media_slots_normalize | truncate(2200)" in audio_script_task
    assert steps["audio_aigc"]["kind"] == "skill_exec"
    assert steps["video_aigc"]["kind"] == "skill_exec"
    assert steps["audio_aigc"]["depends_on"] == ["audio_script"]
    assert "payload" in steps["audio_aigc"]["with"]
    assert "outputs.audio_script | tojson" in steps["audio_aigc"]["with"]["payload"]
    assert "Generate the narration" not in str(steps["audio_aigc"]["with"])
    assert steps["video_aigc"]["depends_on"] == ["page_outline"]
    assert "outputs.media_strategy" not in steps["audio_aigc"]["when"]
    assert "outputs.media_strategy" not in steps["video_aigc"]["when"]
    image_aigc_when = steps["image_aigc"]["when"]
    assert "outputs.media_strategy == 'NEEDS_AIGC_IMAGE'" in image_aigc_when
    assert "IMAGE_DOWNLOAD_INCOMPLETE:" in image_aigc_when
    assert "'IMAGE_READY:' not in outputs.get('image_download', '')" in image_aigc_when
    assert "include_video" in steps["video_aigc"]["when"]
    assert "include_audio" in steps["audio_aigc"]["when"]
    assert "include_images" in steps["image_aigc"]["when"]
    assert "api_key" in steps["image_aigc"]["with"]
    assert "api_key" in steps["audio_aigc"]["with"]
    assert "api_key" in steps["video_aigc"]["with"]
    assert "video_aigc" not in steps["webpage_generation"]["depends_on"]
    assert "media_slots_normalize" in steps["webpage_generation"]["depends_on"]
    assert "media_manifest_normalize" not in steps["webpage_generation"]["depends_on"]
    assert "media_assets_collect" in steps["webpage_generation"]["depends_on"]
    assert steps["webpage_source_validate"]["kind"] == "tool_call"
    assert steps["webpage_source_validate"]["depends_on"] == ["webpage_generation"]
    assert steps["webpage_source_validate"]["tool_args"]["command"].strip() == (
        f"python -m {AWESOME_MODULE}.webpage_source_validate"
    )
    assert steps["webpage_source_validate"]["tool_args"]["stdin"] == (
        "{{ outputs.webpage_generation | tojson }}"
    )
    assert steps["webpage_generation_retry"]["depends_on"] == [
        "webpage_generation",
        "webpage_source_validate",
        "media_slots_normalize",
    ]
    assert steps["webpage_generation_retry"]["when"] == (
        "not outputs.get('webpage_generation', '').strip() "
        "or 'WEBPAGE_SOURCE_INVALID:' in outputs.get('webpage_source_validate', '')"
    )
    assert steps["webpage_write"]["depends_on"] == [
        "webpage_generation",
        "webpage_source_validate",
        "webpage_generation_retry",
    ]
    assert "WEBPAGE_SOURCE_JSON" not in steps["webpage_write"]["tool_args"]["env"]
    assert steps["webpage_write"]["tool_args"]["stdin"] == (
        "{{ (outputs.get('webpage_generation_retry', '') or outputs.webpage_generation) | tojson }}"
    )

    assert "project_slug" in steps
    project_slug = steps["project_slug"]
    assert project_slug["kind"] == "llm_chat"
    assert project_slug["depends_on"] == ["requirement_framing"]
    assert "project_slug" in steps["page_outline"]["depends_on"]
    slug_task = project_slug["with"]["task"]
    assert "lowercase ASCII letters" in slug_task
    assert "max 40 characters" in slug_task
    assert "If you cannot derive a meaningful slug, output `webpage`" in slug_task

    assert "page_outline" in steps
    outline_task = steps["page_outline"]["with"]["task"]
    assert "slot_id" in outline_task
    assert "Do NOT specify filenames" in outline_task or "No filenames" in outline_task
    assert "load_bearing" in outline_task

    assert "page_layout" not in steps
    assert "layout_media_manifest_normalize" not in steps
    assert "media_manifest_normalize" not in steps

    assert "media_assets_collect" in steps
    manifest = steps["media_assets_collect"]
    assert manifest["kind"] == "tool_call"
    assert manifest["tool"] == "exec_command"
    assert set(manifest["depends_on"]) == {
        "image_download",
        "image_aigc",
        "audio_aigc",
        "video_aigc",
    }
    manifest_command = manifest["tool_args"]["command"]
    assert manifest_command.strip() == (
        f"python -m {AWESOME_MODULE}.media_assets_collect"
    )
    assert "PROJECT_ROOT" in manifest["tool_args"]["env"]

    assert "image_download" in steps
    image_download = steps["image_download"]
    assert image_download["kind"] == "skill_exec"
    assert image_download["skill"] == "awesome-webpage-image-download"
    assert image_download["depends_on"] == ["media_strategy", "media_slots_normalize"]
    assert "outputs.media_strategy == 'IMAGE_SEARCH_READY'" in image_download["when"]
    assert "include_images" in image_download["when"]
    assert "image_download" in steps["media_assets_collect"]["depends_on"]
    assert "image_download" not in steps["webpage_generation"]["depends_on"]
    assert "curl" not in str(image_download)
    assert "outputs.media_slots_normalize | tojson" in image_download["with"]["payload"]
    assert "outputs.media_search | tojson" in image_download["with"]["payload"]
    assert "get('config', {})" in image_download["with"]["output_dir"]

    assert steps["image_aigc"]["depends_on"] == [
        "media_strategy",
        "image_download",
        "media_slots_normalize",
    ]
    image_payload = steps["image_aigc"]["with"]["payload"]
    assert "media_slots" in image_payload
    assert "page_outline" in image_payload
    assert "image_download" in image_payload
    assert "include_images" in image_payload
    assert "visual_style" in image_payload


def test_awesome_webpage_media_search_respects_configured_strategy() -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    when = steps["media_search"]["when"]
    base_inputs = {
        "collected": {
            "ask_images": {"include_images": "YES"},
        },
    }

    assert evaluate_when(when, inputs=base_inputs, outputs={})
    assert not evaluate_when(
        when,
        inputs={
            **base_inputs,
            "config": {
                "awesome_webpage": {
                    "media_strategy": {
                        "search_first": False,
                        "search_modalities": ["images"],
                    },
                },
            },
        },
        outputs={},
    )
    assert not evaluate_when(
        when,
        inputs={
            **base_inputs,
            "config": {
                "awesome_webpage": {
                    "media_strategy": {
                        "search_first": True,
                        "search_modalities": [],
                    },
                },
            },
        },
        outputs={},
    )


def test_image_aigc_runs_when_search_download_produces_no_images() -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    when = steps["image_aigc"]["when"]
    inputs = {
        "collected": {
            "ask_images": {"include_images": "YES"},
        },
    }

    assert evaluate_when(
        when,
        inputs=inputs,
        outputs={
            "media_strategy": "NEEDS_AIGC_IMAGE",
            "image_download": "",
        },
    )
    assert evaluate_when(
        when,
        inputs=inputs,
        outputs={
            "media_strategy": "IMAGE_SEARCH_READY",
            "image_download": "downloaded=[]\nall candidates were text/html",
        },
    )
    assert evaluate_when(
        when,
        inputs=inputs,
        outputs={
            "media_strategy": "IMAGE_SEARCH_READY",
            "image_download": (
                'IMAGE_READY: {"local_path":"project/assets/images/hero.jpg"}\n'
                'IMAGE_DOWNLOAD_INCOMPLETE: {"unfilled_slot_ids":["turtle"]}'
            ),
        },
    )
    assert not evaluate_when(
        when,
        inputs=inputs,
        outputs={
            "media_strategy": "IMAGE_SEARCH_READY",
            "image_download": (
                'IMAGE_READY: {"local_path":"project/assets/images/hero.jpg"}'
            ),
        },
    )
    assert not evaluate_when(
        when,
        inputs={"collected": {"ask_images": {"include_images": "NO"}}},
        outputs={
            "media_strategy": "IMAGE_SEARCH_READY",
            "image_download": "",
        },
    )


def test_media_slots_normalize_synthesizes_image_slots_when_outline_has_no_slots(
    tmp_path: Path,
) -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    tool_args = steps["media_slots_normalize"]["tool_args"]
    command = tool_args["command"]
    assert "env" not in tool_args
    assert "PAGE_OUTLINE" not in str(tool_args)
    stdin = json.dumps(
        {
            "page_outline": """
            | section_id | title | purpose |
            | --- | --- | --- |
            | hero | 海洋塑料污染 | establish urgency |
            | impact | 食物链影响 | explain microplastics |
            """,
            "requirement_framing": "主题: 海洋塑料污染科普网页，包含音频、视频和图片",
            "include_image": "YES",
            "include_audio": "YES",
            "include_video": "YES",
            "visual_style": "纪录片风，清晰可信",
        }
    )

    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "MEDIA_SLOTS_READY"
    assert payload["counts"]["image"] >= 2
    assert payload["counts"]["audio"] == 1
    assert payload["counts"]["video"] == 1
    image_slots = [slot for slot in payload["slots"] if slot["modality"] == "image"]
    assert {slot["slot_id"] for slot in image_slots} >= {
        "hero-visual",
        "supporting-visual",
    }
    assert all(slot["source"] == "synthesized" for slot in image_slots)


def test_awesome_webpage_media_entrypoints_are_code_backed(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    loader.invalidate_cache()

    image = loader.get_by_name("nano-banana-pro-openrouter")
    audio = loader.get_by_name("audio-cog")
    video = loader.get_by_name("openrouter-video-generator")

    assert image is not None
    assert image.entrypoint is not None
    assert image.entrypoint["command"] == "python {baseDir}/scripts/openrouter_image.py"
    assert "--api-key" not in image.entrypoint["args"]
    assert "--api-key-env" in image.entrypoint["args"]
    assert image.entrypoint["env"][
        "{{ with.api_key_env | default('OPENROUTER_API_KEY') }}"
    ] == (
        "{{ with.api_key | default('') }}"
    )

    assert audio is not None
    assert audio.entrypoint is not None
    assert audio.entrypoint["command"] == "python {baseDir}/scripts/openrouter_audio.py"
    assert "--api-key" not in audio.entrypoint["args"]
    assert "--api-key-env" in audio.entrypoint["args"]
    assert audio.entrypoint["env"][
        "{{ with.api_key_env | default('OPENROUTER_API_KEY') }}"
    ] == (
        "{{ with.api_key | default('') }}"
    )
    assert audio.entrypoint["parse"] == "text"

    assert video is not None
    assert video.entrypoint is not None
    assert video.entrypoint["command"] == (
        "python {baseDir}/scripts/openrouter_video.py"
    )
    assert "--api-key" not in video.entrypoint["args"]
    assert "--api-key-env" in video.entrypoint["args"]
    assert video.entrypoint["env"][
        "{{ with.api_key_env | default('OPENROUTER_API_KEY') }}"
    ] == (
        "{{ with.api_key | default('') }}"
    )
    assert video.entrypoint["parse"] == "text"


def test_web_search_uses_bundled_script_entrypoint(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    loader.invalidate_cache()

    web_search = loader.get_by_name("web-search")
    assert web_search is not None
    assert web_search.entrypoint is not None
    assert web_search.entrypoint["command"] == "python {baseDir}/scripts/search.py"
    assert "{{ with.query | default(inputs.user_message) }}" in web_search.entrypoint["args"]

    body = SKILL_MD.parent.parent.joinpath("web-search", "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "python scripts/search.py" not in body
    assert "python {baseDir}/scripts/search.py" in body


def test_awesome_image_downloader_emits_ready_record(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_awesome_image_download_module()
    image_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00"
    )

    class FakeResponse:
        headers = {"Content-Type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            del args
            return None

        def read(self, *_args: object) -> bytes:
            return image_bytes

        def geturl(self) -> str:
            return "https://cdn.example/hero.png"

    def fake_urlopen(*_args: object, **_kwargs: object) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "media_slots": json.dumps(
                        {
                            "slots": [
                                {
                                    "slot_id": "hero-visual",
                                    "modality": "image",
                                    "subject": "hero ocean",
                                    "search_keywords": ["hero", "ocean"],
                                }
                            ]
                        }
                    ),
                    "media_search": "candidate https://cdn.example/hero.png",
                }
            )
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "image_download.py",
            "--output-dir",
            str(tmp_path),
            "--local-path-prefix",
            "project/assets/images",
        ],
    )

    assert module.main() == 0

    out = capsys.readouterr().out
    assert (tmp_path / "hero-visual.png").read_bytes() == image_bytes
    assert "IMAGE_READY" in out
    assert "project/assets/images/hero-visual.png" in out


def test_audio_cog_json_payload_builds_exact_transcript_messages() -> None:
    script = BUNDLED / "audio-cog" / "scripts" / "openrouter_audio.py"
    spec = importlib.util.spec_from_file_location("openrouter_audio", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    messages, preview = module._audio_messages(
        json.dumps({"script": "雨水花园会截留雨水，净化径流，并滋养社区绿地。"}, ensure_ascii=False)
    )

    assert preview == "雨水花园会截留雨水，净化径流，并滋养社区绿地。"
    assert messages[0]["role"] == "system"
    assert "Do not acknowledge" in messages[0]["content"]
    assert "Speak this exact narration transcript and no other words" in messages[1]["content"]
    assert "雨水花园会截留雨水" in messages[1]["content"]


def test_openrouter_video_resolves_relative_polling_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_openrouter_video_module()
    requests: list[tuple[str, str]] = []

    def fake_request_json(
        url: str,
        *,
        key: str,
        method: str = "GET",
        body: dict[str, object] | None = None,
        timeout: float = 60.0,
    ) -> dict[str, object]:
        del body, timeout
        assert key == "sk-or-test"
        requests.append((method, url))
        if method == "POST":
            return {
                "id": "job-abc123",
                "status": "queued",
                "polling_url": "/api/v1/videos/job-abc123",
            }
        return {
            "status": "completed",
            "unsigned_urls": ["https://storage.example/video.mp4"],
        }

    def fake_download(
        url: str,
        *,
        key: str,
        base_url: str,
        timeout: float = 120.0,
    ) -> bytes:
        del key, base_url, timeout
        assert url == "https://storage.example/video.mp4"
        return b"video-bytes"

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(module, "_request_json", fake_request_json)
    monkeypatch.setattr(module, "_download", fake_download)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("make a short video"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openrouter_video.py",
            "--model",
            "bytedance/seedance-2.0-fast",
            "--output-dir",
            str(tmp_path),
            "--filename",
            "sample.mp4",
        ],
    )

    assert module.main() == 0

    assert ("GET", "https://openrouter.ai/api/v1/videos/job-abc123") in requests
    assert (tmp_path / "sample.mp4").read_bytes() == b"video-bytes"


def test_openrouter_video_download_auth_stays_on_openrouter_origin(monkeypatch) -> None:
    module = _load_openrouter_video_module()
    opened_headers: list[dict[str, str]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"video-bytes"

    def fake_urlopen(req, timeout: float = 120.0):
        del timeout
        opened_headers.append({key.lower(): value for key, value in req.header_items()})
        return FakeResponse()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    assert (
        module._download(
            "https://storage.example/video.mp4",
            key="sk-or-secret",
            base_url="https://openrouter.ai/api/v1",
        )
        == b"video-bytes"
    )
    assert "authorization" not in opened_headers[-1]

    assert (
        module._download(
            "https://openrouter.ai/api/v1/videos/job-abc123/content",
            key="sk-or-secret",
            base_url="https://openrouter.ai/api/v1",
        )
        == b"video-bytes"
    )
    assert opened_headers[-1]["authorization"] == "Bearer sk-or-secret"


def test_openrouter_image_uses_explicit_api_key_without_env(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_openrouter_image_module()
    seen_keys: list[str] = []

    def fake_generate_one(**kwargs: object) -> dict[str, object]:
        seen_keys.append(str(kwargs["api_key"]))
        return {
            "ok": True,
            "slot_id": "image",
            "local_path": "project/assets/images/image.png",
            "mime": "image/png",
            "bytes": 1,
            "prompt_preview": "demo",
        }

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(module, "_generate_one", fake_generate_one)
    monkeypatch.setattr(sys, "stdin", io.StringIO("demo prompt"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openrouter_image.py",
            "--model",
            "google/gemini-3-pro-image-preview",
            "--base-url",
            "https://openrouter.ai/api/v1",
            "--api-key",
            "sk-configured",
            "--output-dir",
            str(tmp_path),
            "--filename",
            "image.png",
        ],
    )

    assert module.main() == 0

    out = capsys.readouterr().out
    assert seen_keys == ["sk-configured"]
    assert "IMAGE_READY" in out
    assert "IMAGE_CONFIG_NEEDED" not in out


def test_openrouter_audio_timeout_reports_generation_failed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_openrouter_audio_module()

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise TimeoutError("timed out")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "stdin", io.StringIO("short narration"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openrouter_audio.py",
            "--model",
            "openai/gpt-audio-mini",
            "--api-key",
            "sk-configured",
            "--output-dir",
            str(tmp_path),
            "--filename",
            "sample.wav",
        ],
    )

    assert module.main() == 0

    out = capsys.readouterr().out
    assert "AUDIO_GENERATION_FAILED" in out
    assert "TimeoutError" in out


def test_openrouter_video_submit_timeout_reports_generation_failed(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_openrouter_video_module()

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise TimeoutError("timed out")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "stdin", io.StringIO("short video prompt"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openrouter_video.py",
            "--model",
            "bytedance/seedance-2.0-fast",
            "--api-key",
            "sk-configured",
            "--output-dir",
            str(tmp_path),
            "--filename",
            "sample.mp4",
            "--poll-interval",
            "1",
            "--max-wait",
            "1",
        ],
    )

    assert module.main() == 0

    out = capsys.readouterr().out
    assert "VIDEO_GENERATION_FAILED" in out
    assert '"phase":"submit"' in out
    assert "TimeoutError" in out


def test_openrouter_media_entrypoints_return_config_needed_without_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    audio_script = BUNDLED / "audio-cog" / "scripts" / "openrouter_audio.py"
    audio = subprocess.run(
        [
            sys.executable,
            str(audio_script),
            "--model",
            "openai/gpt-audio-mini",
            "--output-dir",
            str(tmp_path / "audio"),
            "--filename",
            "sample.wav",
        ],
        input=b"short narration",
        capture_output=True,
        check=False,
    )
    assert audio.returncode == 0
    assert "AUDIO_CONFIG_NEEDED" in audio.stdout.decode("utf-8")
    assert not audio.stderr

    video_script = (
        BUNDLED
        / "openrouter-video-generator"
        / "scripts"
        / "openrouter_video.py"
    )
    video = subprocess.run(
        [
            sys.executable,
            str(video_script),
            "--model",
            "bytedance/seedance-2.0-fast",
            "--output-dir",
            str(tmp_path / "video"),
            "--filename",
            "sample.mp4",
        ],
        input=b"short video prompt",
        capture_output=True,
        check=False,
    )
    assert video.returncode == 0
    assert "VIDEO_CONFIG_NEEDED" in video.stdout.decode("utf-8")
    assert not video.stderr


def test_awesome_webpage_steps_pass_resolved_output_dir() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "{{ inputs.workspace_dir }}/awesome-webpage-output" in text
    assert "must not be reported as CONFIG_NEEDED" in text
    assert "do not install another filesystem skill" in text
    assert "does not mean\n          the user does not want that media modality" in text
    assert "音频不走素材搜索" in text
    assert "视频不走素材搜索" in text


def test_webpage_generation_is_scoped_to_core_file_authoring() -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    webpage_generation = steps["webpage_generation"]
    task = webpage_generation["with"]["task"]

    assert webpage_generation["kind"] == "agent"
    assert webpage_generation["skill"] == "html-coder"
    assert webpage_generation["with"]["mode"] == "generate"
    assert webpage_generation["depends_on"] == [
        "requirement_framing",
        "deep_research",
        "page_outline",
        "media_slots_normalize",
        "media_assets_collect",
    ]
    assert "Produce source text only" in task
    assert "Ignore\nhtml-coder's default Markdown/code-block output format" in task
    assert "professional HTML/CSS quality standard" in task
    assert "Author only the contents for project/index.html, project/style.css" in (
        task
    )
    assert "Do not download, search, generate, copy, move, delete, package, validate, repair" in (
        task
    )
    assert "media_assets_collect.assets[]" in task
    assert "Design-quality contract, adapted from html-coder" in task
    assert "https://clawhub.ai/jhauga/html-coder" in task
    assert "<audio controls>" in task
    assert "Place audio controls according to the audio slot placement" in task
    assert "footer-only/end-of-page" in task
    assert "gallery" in task
    assert "page_layout" not in task
    assert "layout manifest" not in task
    assert "Do not scan raw" in task
    assert "project/assets/..." in task
    assert "research_report" not in webpage_generation["with"]
    assert "framed_requirements" not in webpage_generation["with"]
    assert "outputs.page_outline | truncate(1500)" in task
    assert "outputs.media_slots_normalize | truncate(3500)" in task
    assert "outputs.media_assets_collect | truncate(6000)" in task
    assert "outputs.media_manifest_normalize | truncate(5000)" not in task
    assert "outputs.media_search | truncate" not in task
    assert "outputs.media_search_cn | truncate" not in task
    assert "outputs.image_download | truncate" not in task
    assert "outputs.image_aigc | truncate" not in task
    assert "outputs.audio_aigc | truncate" not in task
    assert "outputs.video_aigc | truncate" not in task

    retry = steps["webpage_generation_retry"]
    retry_task = retry["with"]["task"]
    assert retry["kind"] == "llm_chat"
    assert "primary source authoring step returned" in retry["with"]["system"]
    assert "Output JSON only" in retry_task
    assert "Do not call tools" in retry_task
    assert "Do not invent pending audio/video" in retry_task
    assert "media_slots_normalize" in retry_task
    assert "footer-only/end-of-page" in retry_task
    assert "outputs.page_outline | truncate(900)" in retry_task
    assert "outputs.media_slots_normalize | truncate(2200)" in retry_task
    assert "outputs.media_assets_collect | truncate(3500)" in retry_task
    assert "outputs.media_manifest_normalize | truncate" not in retry_task


def test_webpage_write_accepts_prose_wrapped_fenced_json(tmp_path: Path) -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    command = steps["webpage_write"]["tool_args"]["command"]

    project_root = tmp_path / "awesome-webpage-output" / "demo"
    source = {
        "index_html": (
            '<main><h1>Demo</h1><img src="assets/images/hero.png">'
            '<audio controls src="assets/audio/narration.wav"></audio>'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
        "style_css": "body{margin:0}.hero{display:grid}",
        "script_js": "document.documentElement.dataset.ready = 'true';",
        "summary": "demo page",
    }
    wrapped_source = (
        "Here is the requested source JSON:\n```json\n"
        + json.dumps(source)
        + "\n```\nDone."
    )
    env = os.environ.copy()
    env.update(
        {
            "WORKSPACE_DIR": str(tmp_path),
            "PROJECT_ROOT": str(project_root),
        }
    )

    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env=env,
        input=json.dumps(wrapped_source).encode("utf-8"),
        capture_output=True,
        check=False,
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0, output
    assert "WEBPAGE_FILES_WRITTEN" in output
    project_dir = project_root / "project"
    assert (project_dir / "index.html").read_text(encoding="utf-8") == source["index_html"]
    assert (project_dir / "style.css").read_text(encoding="utf-8") == source["style_css"]
    assert (project_dir / "script.js").read_text(encoding="utf-8") == source["script_js"]


def test_webpage_source_validate_marks_malformed_non_empty_source_invalid(
    tmp_path: Path,
) -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    command = steps["webpage_source_validate"]["tool_args"]["command"]

    result = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        input=json.dumps("not json {broken").encode("utf-8"),
        capture_output=True,
        check=False,
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0, output
    assert "WEBPAGE_SOURCE_INVALID" in output


def test_awesome_webpage_media_steps_forward_configured_openrouter_settings() -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    inputs = {
        "workspace_dir": "/tmp/osq-workspace",
        "config": {
            "awesome_webpage": {
                "openrouter": {
                    "api_key": "sk-configured",
                    "api_key_env": "CUSTOM_OPENROUTER_KEY",
                    "base_url": "https://openrouter.example/v1",
                    "models": {
                        "image_generation": "provider/custom-image",
                        "audio_generation": "provider/custom-audio",
                        "video_generation": "provider/custom-video",
                    },
                },
            },
        },
        "collected": {
            "ask_images": {"include_images": "YES"},
            "ask_audio": {"include_audio": "YES"},
            "ask_video": {"include_video": "YES"},
            "ask_style": {"visual_style": "clean"},
        },
    }
    outputs = {
        "project_slug": "demo",
        "requirement_framing": "demo",
        "page_outline": "demo",
        "media_slots_normalize": "{}",
        "media_strategy": "NEEDS_AIGC_IMAGE",
        "image_download": "",
        "audio_script": "demo narration",
    }

    expected_models = {
        "image_aigc": "provider/custom-image",
        "audio_aigc": "provider/custom-audio",
        "video_aigc": "provider/custom-video",
    }
    for step_id, model in expected_models.items():
        rendered = render_with_args(steps[step_id]["with"], inputs=inputs, outputs=outputs)
        assert rendered["api_key"] == "sk-configured"
        assert rendered["api_key_env"] == "CUSTOM_OPENROUTER_KEY"
        assert rendered["base_url"] == "https://openrouter.example/v1"
        assert rendered["model"] == model


def test_awesome_webpage_media_bind_validate_is_deterministic() -> None:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    assert "asset_usage_repair" not in steps
    assert "media_completeness_validate" not in steps

    bind = steps["media_bind_validate"]
    assert bind["kind"] == "tool_call"
    assert bind["tool"] == "exec_command"
    assert bind["depends_on"] == ["webpage_write", "media_assets_collect"]
    assert "MEDIA_MANIFEST" not in bind["tool_args"]["env"]
    command = bind["tool_args"]["command"]
    assert command.strip() == f"python -m {AWESOME_MODULE}.media_bind_validate"
    assert "OPENROUTER_API_KEY" not in command

    assert steps["quick_validate"]["depends_on"] == ["media_bind_validate"]
    assert "outputs.media_bind_validate | truncate(3500)" in (
        steps["quick_validate"]["with"]["task"]
    )
    assert "outputs.media_bind_validate | truncate(5000)" in (
        steps["delivery_guide"]["with"]["task"]
    )


def _run_media_bind_step(
    tmp_path: Path,
    *,
    assets: list[dict],
    index_html: str,
    include_image: str = "YES",
    include_audio: str = "YES",
    include_video: str = "YES",
    image_aigc: str = "",
    audio_aigc: str = "",
    video_aigc: str = "",
    style_css: str = "body{margin:0}",
) -> subprocess.CompletedProcess[bytes]:
    fm = _frontmatter()
    steps = {step["id"]: step for step in fm["composition"]["steps"]}
    command = steps["media_bind_validate"]["tool_args"]["command"]

    project_root = tmp_path / "awesome-webpage-output" / "demo"
    project_dir = project_root / "project"
    (project_dir / "assets" / "images").mkdir(parents=True)
    (project_dir / "assets" / "audio").mkdir(parents=True)
    (project_dir / "assets" / "video").mkdir(parents=True)
    (project_dir / "index.html").write_text(index_html, encoding="utf-8")
    (project_dir / "style.css").write_text(style_css, encoding="utf-8")
    (project_dir / "script.js").write_text("console.log('ok')", encoding="utf-8")
    ready_lines = {"image": [], "audio": [], "video": []}
    for asset in assets:
        src = str(asset.get("src") or "")
        if src:
            target = project_dir / src
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"media-bytes")
            kind = str(asset.get("kind"))
            ready_lines[kind].append(
                f'{kind.upper()}_READY: '
                + json.dumps(
                    {
                        "local_path": f"project/{src}",
                        "mime": asset.get("mime"),
                        "subject": asset.get("subject", src),
                    },
                    separators=(",", ":"),
                )
            )

    env = os.environ.copy()
    env.update(
        {
            "PROJECT_ROOT": str(project_root),
            "IMAGE_DOWNLOAD": "\n".join(ready_lines["image"]),
            "IMAGE_AIGC": image_aigc,
            "AUDIO_AIGC": "\n".join(ready_lines["audio"] + [audio_aigc]),
            "VIDEO_AIGC": "\n".join(ready_lines["video"] + [video_aigc]),
            "INCLUDE_IMAGE": include_image,
            "INCLUDE_AUDIO": include_audio,
            "INCLUDE_VIDEO": include_video,
        }
    )
    return subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
    )


def test_media_bind_validate_passes_when_requested_media_is_bound(
    tmp_path: Path,
) -> None:
    assets = [
        {"kind": "image", "src": "assets/images/hero.png"},
        {"kind": "audio", "src": "assets/audio/narration.wav"},
        {"kind": "video", "src": "assets/video/intro.mp4"},
    ]
    result = _run_media_bind_step(
        tmp_path,
        assets=assets,
        index_html=(
            '<main><img src="assets/images/hero.png">'
            '<audio controls src="assets/audio/narration.wav"></audio>'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
    )

    assert result.returncode == 0
    assert "MEDIA_BIND_OK" in result.stdout.decode("utf-8")


def test_media_bind_validate_repairs_when_requested_audio_is_unbound(
    tmp_path: Path,
) -> None:
    assets = [
        {"kind": "image", "src": "assets/images/hero.png"},
        {"kind": "audio", "src": "assets/audio/narration.wav"},
        {"kind": "video", "src": "assets/video/intro.mp4"},
    ]
    result = _run_media_bind_step(
        tmp_path,
        assets=assets,
        index_html=(
            '<main><img src="assets/images/hero.png">'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0
    assert "MEDIA_BIND_OK" in output
    index_html = (
        tmp_path
        / "awesome-webpage-output"
        / "demo"
        / "project"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert '<audio controls preload="metadata" src="assets/audio/narration.wav">' in index_html


def test_media_bind_validate_repairs_audio_when_asset_path_is_not_control_src(
    tmp_path: Path,
) -> None:
    assets = [
        {"kind": "image", "src": "assets/images/hero.png"},
        {"kind": "audio", "src": "assets/audio/narration.wav"},
        {"kind": "video", "src": "assets/video/intro.mp4"},
    ]
    result = _run_media_bind_step(
        tmp_path,
        assets=assets,
        index_html=(
            '<main><img src="assets/images/hero.png">'
            '<audio controls></audio>'
            '<p>assets/audio/narration.wav</p>'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0, output
    index_html = (
        tmp_path
        / "awesome-webpage-output"
        / "demo"
        / "project"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert '<audio controls preload="metadata" src="assets/audio/narration.wav">' in index_html


def test_media_bind_validate_repairs_image_when_asset_path_is_not_rendered(
    tmp_path: Path,
) -> None:
    assets = [
        {"kind": "image", "src": "assets/images/hero.png"},
        {"kind": "audio", "src": "assets/audio/narration.wav"},
        {"kind": "video", "src": "assets/video/intro.mp4"},
    ]
    result = _run_media_bind_step(
        tmp_path,
        assets=assets,
        index_html=(
            '<main><p>assets/images/hero.png</p>'
            '<audio controls src="assets/audio/narration.wav"></audio>'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0, output
    index_html = (
        tmp_path
        / "awesome-webpage-output"
        / "demo"
        / "project"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert '<img loading="lazy" src="assets/images/hero.png"' in index_html


def test_media_bind_validate_persists_repair_when_generated_media_css_exists(
    tmp_path: Path,
) -> None:
    assets = [
        {"kind": "image", "src": "assets/images/hero.png"},
        {"kind": "audio", "src": "assets/audio/narration.wav"},
        {"kind": "video", "src": "assets/video/intro.mp4"},
    ]
    result = _run_media_bind_step(
        tmp_path,
        assets=assets,
        index_html=(
            '<main><img src="assets/images/hero.png">'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
        style_css=".generated-media-assets{display:block}",
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0, output
    index_html = (
        tmp_path
        / "awesome-webpage-output"
        / "demo"
        / "project"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert '<audio controls preload="metadata" src="assets/audio/narration.wav">' in index_html


def test_media_bind_validate_fails_when_requested_audio_has_no_ready_asset(
    tmp_path: Path,
) -> None:
    result = _run_media_bind_step(
        tmp_path,
        assets=[
            {"kind": "image", "src": "assets/images/hero.png"},
            {"kind": "video", "src": "assets/video/intro.mp4"},
        ],
        index_html=(
            '<main><img src="assets/images/hero.png">'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode != 0
    assert "MEDIA_BIND_FAILED" in output
    assert "requested_modality_has_no_ready_asset" in output


def test_media_bind_validate_degrades_when_requested_audio_needs_config(
    tmp_path: Path,
) -> None:
    result = _run_media_bind_step(
        tmp_path,
        assets=[
            {"kind": "image", "src": "assets/images/hero.png"},
            {"kind": "video", "src": "assets/video/intro.mp4"},
        ],
        index_html=(
            '<main><img src="assets/images/hero.png">'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
        audio_aigc=(
            "AUDIO_CONFIG_NEEDED: "
            + json.dumps(
                {
                    "missing": ["OPENROUTER_API_KEY"],
                    "replacement_slot": "project/assets/audio/narration.wav",
                    "reason": "missing_api_key",
                },
                separators=(",", ":"),
            )
        ),
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0, output
    report = json.loads(result.stdout.decode("utf-8"))
    degraded_audio = report["degraded"]["audio"][0]
    assert report["status"] == "MEDIA_BIND_DEGRADED"
    assert degraded_audio["label"] == "AUDIO_CONFIG_NEEDED"
    assert degraded_audio["reason"] == "missing_api_key"
    assert degraded_audio["missing"] == ["OPENROUTER_API_KEY"]
    assert degraded_audio["replacement_src"] == "assets/audio/narration.wav"
    assert "requested_modality_has_no_ready_asset" not in output


def test_media_bind_validate_degrades_when_requested_image_partially_fails(
    tmp_path: Path,
) -> None:
    result = _run_media_bind_step(
        tmp_path,
        assets=[
            {"kind": "image", "src": "assets/images/hero.png"},
            {"kind": "audio", "src": "assets/audio/narration.wav"},
            {"kind": "video", "src": "assets/video/intro.mp4"},
        ],
        index_html=(
            '<main><img src="assets/images/hero.png">'
            '<audio controls src="assets/audio/narration.wav"></audio>'
            '<video controls src="assets/video/intro.mp4"></video></main>'
        ),
        image_aigc=(
            "IMAGE_GENERATION_FAILED: "
            + json.dumps(
                {
                    "slot_id": "missing-gallery-card",
                    "reason": "provider_timeout",
                },
                separators=(",", ":"),
            )
        ),
    )

    output = result.stdout.decode("utf-8") + result.stderr.decode("utf-8")
    assert result.returncode == 0, output
    report = json.loads(result.stdout.decode("utf-8"))
    partial_image = report["partial_generation_failures"]["image"][0]
    assert report["status"] == "MEDIA_BIND_DEGRADED"
    assert partial_image["label"] == "IMAGE_GENERATION_FAILED"
    assert partial_image["reason"] == "provider_timeout"


def test_media_bind_validate_skips_modalities_user_declined(
    tmp_path: Path,
) -> None:
    result = _run_media_bind_step(
        tmp_path,
        assets=[],
        index_html="<main><p>text-only requested</p></main>",
        include_image="NO",
        include_audio="NO",
        include_video="NO",
    )

    assert result.returncode == 0
    assert "MEDIA_BIND_OK" in result.stdout.decode("utf-8")


def test_awesome_webpage_rendered_steps_resolve_output_dir(tmp_path: Path) -> None:
    loader = SkillLoader(bundled_dir=BUNDLED, snapshot_path=tmp_path / "snapshot.json")
    loader.invalidate_cache()
    spec = loader.get_by_name("AwesomeWebpageMetaSkill")
    assert spec is not None
    plan = parse_meta_plan(spec)
    assert plan is not None

    inputs = {
        "user_message": "请使用 AwesomeWebpageMetaSkill。主题：海洋塑料污染",
        "language_instruction": "Output language rule: Chinese.",
        "workspace_dir": "/tmp/osq-workspace",
        "collected": {
            "ask_images": {"include_images": "YES"},
            "ask_audio": {"include_audio": "YES"},
            "ask_video": {"include_video": "YES"},
            "ask_style": {"visual_style": "纪录片风，清晰、可信、适合科普"},
        },
        "config": {
            "awesome_webpage": {
                "output_dir": "/tmp/custom-awesome-output",
                "media_strategy": {"target_assets": {"images": 2}},
            },
        },
    }
    outputs = {
        key: key
        for key in [
            "requirement_framing",
            "project_slug",
            "ask_images",
            "ask_audio",
            "ask_video",
            "ask_style",
            "deep_research",
            "page_outline",
            "media_slots_normalize",
            "media_search",
            "media_strategy",
            "image_download",
            "image_aigc",
            "audio_script",
            "audio_aigc",
            "video_aigc",
            "media_assets_collect",
            "webpage_generation",
            "webpage_generation_retry",
            "webpage_write",
            "media_bind_validate",
            "quick_validate",
            "publish_webpage",
        ]
    }

    for step_id in [
        "requirement_framing",
        "image_download",
        "image_aigc",
        "audio_aigc",
        "video_aigc",
        "quick_validate",
        "delivery_guide",
    ]:
        step = next(step for step in plan.steps if step.id == step_id)
        rendered = render_with_args(step.with_args, inputs=inputs, outputs=outputs)
        text = "\n".join(str(value) for value in rendered.values())
        assert "/tmp/custom-awesome-output" in text
        assert "/tmp/osq-workspace/awesome-webpage-output" not in text

    fm_steps = {step["id"]: step for step in _frontmatter()["composition"]["steps"]}
    for step_id in [
        "media_assets_collect",
        "webpage_write",
        "media_bind_validate",
        "publish_webpage",
    ]:
        rendered = render_with_args(
            fm_steps[step_id]["tool_args"],
            inputs=inputs,
            outputs=outputs,
        )
        text = "\n".join(str(value) for value in rendered.values())
        assert "/tmp/custom-awesome-output" in text
        assert "/tmp/osq-workspace/awesome-webpage-output" not in text

    rendered_publish = render_with_args(
        fm_steps["publish_webpage"]["tool_args"],
        inputs=inputs,
        outputs=outputs,
    )
    assert rendered_publish == {
        "path": "/tmp/custom-awesome-output/project_slug/project/index.html",
        "mime": "text/html",
        "bundle": "directory",
        "bundle_root": "/tmp/custom-awesome-output/project_slug/project",
    }

    for step_id in ["quick_validate", "delivery_guide"]:
        step = next(step for step in plan.steps if step.id == step_id)
        rendered = render_with_args(step.with_args, inputs=inputs, outputs=outputs)
        text = "\n".join(str(value) for value in rendered.values())
        assert "/tmp/custom-awesome-output/project_slug" in text

    for step_id in ["delivery_guide"]:
        step = next(step for step in plan.steps if step.id == step_id)
        rendered = render_with_args(step.with_args, inputs=inputs, outputs=outputs)
        text = "\n".join(str(value) for value in rendered.values())
        assert "/tmp/custom-awesome-output/project_slug/project" in text

    image_step = next(step for step in plan.steps if step.id == "image_aigc")
    rendered_image = render_with_args(
        image_step.with_args,
        inputs=inputs,
        outputs=outputs,
    )
    assert rendered_image["max_images"] == "2"
