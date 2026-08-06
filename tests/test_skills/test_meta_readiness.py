"""Declared dependency preflight for composed meta-skills."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.skills import capability_runtime
from opensquilla.skills.capability_runtime import (
    CAPABILITY_AUDIO_GENERATE,
    CAPABILITY_IMAGE_GENERATE,
    CAPABILITY_IMAGE_REFERENCE,
    CAPABILITY_VIDEO_GENERATE,
    META_CAPABILITY_API_KEY_ENV,
    META_CAPABILITY_BASE_URL_ENV,
    META_CAPABILITY_PROVIDER_ENV,
    CapabilityProviderCandidate,
)
from opensquilla.skills.eligibility import EligibilityContext
from opensquilla.skills.loader import SkillLoader
from opensquilla.skills.meta.parser import parse_meta_plan
from opensquilla.skills.meta.readiness import (
    META_OPENROUTER_API_KEY_ENV,
    assess_meta_skill_readiness,
    configured_meta_readiness_env_aliases,
    configured_meta_skill_runtime_env,
    meta_readiness_context,
)
from opensquilla.skills.types import (
    SkillInstallSpec,
    SkillLayer,
    SkillPlatformMeta,
    SkillRequires,
    SkillSpec,
)

_BUNDLED = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "opensquilla"
    / "skills"
    / "bundled"
)


def _spec(
    name: str,
    *,
    kind: str = "skill",
    bins: list[str] | None = None,
    env_any: list[str] | None = None,
    composition: dict[str, object] | None = None,
    install: list[SkillInstallSpec] | None = None,
    layer: SkillLayer = SkillLayer.BUNDLED,
) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=name,
        layer=layer,
        always=False,
        triggers=[],
        content="",
        kind=kind,
        metadata=SkillPlatformMeta(
            requires=SkillRequires(bins=bins or [], env_any=env_any or []),
            install=install or [],
        ),
        composition_raw=composition,
    )


def _trusted_short_drama_fixture(
    *,
    layer: SkillLayer = SkillLayer.BUNDLED,
) -> tuple[SkillSpec, object, SkillSpec, SkillSpec, dict[str, SkillSpec]]:
    image = _spec("nano-banana-pro", env_any=["OPENROUTER_API_KEY"])
    video = _spec(
        "seedance-2-prompt",
        env_any=["OPENROUTER_API_KEY", "ARK_API_KEY"],
    )
    review = _spec("short-drama-review-normalizer")
    consent = "'DECISION: proceed' in outputs.review_normalize"
    paid_consent = (
        f"{consent} and "
        "(outputs.final_script | short_drama_duration_contract_valid)"
    )
    steps: list[dict[str, object]] = [
        {
            "id": "review_gate",
            "kind": "user_input",
            "clarify": {
                "mode": "form",
                "fields": [
                    {
                        "name": "review",
                        "type": "string",
                        "required": True,
                        "prompt": "Review",
                    }
                ],
            },
        },
        {
            "id": "review_intent",
            "kind": "skill_exec",
            "skill": review.name,
            "depends_on": ["review_gate"],
        },
        {
            "id": "script_draft",
            "kind": "llm_chat",
        },
        {
            "id": "script_reread",
            "kind": "llm_chat",
            "depends_on": ["review_gate", "script_draft"],
        },
        {
            "id": "script_revised",
            "kind": "llm_chat",
            "depends_on": ["review_intent", "script_reread"],
            "when": (
                "'DECISION: revise' in outputs.review_intent and "
                "'HAS_OVERRIDES: yes' in outputs.review_intent"
            ),
        },
        {
            "id": "revision_confirm_gate",
            "kind": "user_input",
            "depends_on": [
                "review_intent",
                "script_draft",
                "script_reread",
                "script_revised",
            ],
            "when": (
                "'DECISION: revise' in outputs.review_intent or "
                "('DECISION: proceed' in outputs.review_intent and "
                "outputs.script_reread != outputs.script_draft)"
            ),
            "clarify": {
                "mode": "form",
                "fields": [
                    {
                        "name": "review",
                        "type": "string",
                        "required": True,
                        "prompt": "Approve revised script",
                    }
                ],
            },
        },
        {
            "id": "review_normalize",
            "kind": "skill_exec",
            "skill": review.name,
            "depends_on": ["review_intent", "revision_confirm_gate"],
            "with": {
                "payload": {
                    "phase": "media_approval",
                    "approval_snapshot_changed": (
                        "{{ outputs.script_reread != outputs.script_draft }}"
                    ),
                }
            },
        },
        {
            "id": "reference_image",
            "kind": "skill_exec",
            "skill": image.name,
            "side_effect": "external_paid_submit",
            "when": paid_consent,
        }
    ]
    for shot in range(1, 11):
        present = (
            f"{paid_consent} and "
            f"'=== SHOT_{shot} ===' in outputs.final_script.splitlines()"
        )
        steps.extend(
            [
                {
                    "id": f"shot{shot}_image",
                    "kind": "skill_exec",
                    "skill": image.name,
                    "side_effect": "external_paid_submit",
                    "when": (
                        f"{present} and '__SHOT_ABSENT__' not in "
                        f"outputs.shot{shot}_img_prompt"
                    ),
                },
                {
                    "id": f"shot{shot}_video",
                    "kind": "skill_exec",
                    "skill": video.name,
                    "side_effect": "external_paid_submit",
                    "when": (
                        f"{present} and '__SHOT_ABSENT__' not in "
                        f"outputs.shot{shot}_vid_prompt"
                    ),
                },
            ]
        )
    meta = _spec(
        "meta-short-drama",
        kind="meta",
        composition={"steps": steps},
        layer=layer,
    )
    plan = parse_meta_plan(meta)
    assert plan is not None
    skill_index = {spec.name: spec for spec in (meta, review, image, video)}
    return meta, plan, image, video, skill_index


def test_openrouter_config_key_satisfies_canonical_skill_env_without_exposing_key(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    secret = "synthetic-openrouter-config-key"
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="openrouter",
            api_key=secret,
            api_key_env="",
        )
    )
    meta, plan, image, video, skill_index = _trusted_short_drama_fixture()

    aliases = configured_meta_readiness_env_aliases(
        config,
        parent_spec=meta,
        plan=plan,
        skill_resolver=skill_index,
    )
    readiness_ctx = meta_readiness_context(
        config=config,
        parent_spec=meta,
        plan=plan,
        skill_resolver=skill_index,
    )
    readiness = assess_meta_skill_readiness(
        meta,
        skill_index=skill_index,
        ctx=readiness_ctx,
        config=config,
        validated_plan=plan,
    )

    assert aliases == ("OPENROUTER_API_KEY",)
    assert readiness.ready is True
    assert secret not in repr(readiness_ctx.env_cache)


def test_openrouter_custom_api_key_env_satisfies_canonical_skill_env(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv(
        "OPENSQUILLA_TEST_CUSTOM_OPENROUTER_KEY",
        "synthetic-openrouter-custom-env-key",
    )
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider=" OPENROUTER ",
            api_key="",
            api_key_env="OPENSQUILLA_TEST_CUSTOM_OPENROUTER_KEY",
        )
    )
    meta, plan, image, video, skill_index = _trusted_short_drama_fixture()

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index=skill_index,
        ctx=meta_readiness_context(
            config=config,
            parent_spec=meta,
            plan=plan,
            skill_resolver=skill_index,
        ),
        config=config,
        validated_plan=plan,
    )

    assert readiness.ready is True
    assert readiness.missing_env_any == ()


def test_parent_runtime_env_scopes_active_key_to_paid_media_skills(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-key-must-not-win")
    secret = "synthetic-active-config-key"
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="openrouter",
            api_key=secret,
            api_key_env="",
        )
    )
    meta, plan, _image, _video, skill_index = _trusted_short_drama_fixture()

    runtime_env = configured_meta_skill_runtime_env(
        config,
        parent_spec=meta,
        plan=plan,
        skill_resolver=skill_index,
    )

    assert set(runtime_env) == {"nano-banana-pro", "seedance-2-prompt"}
    for values in runtime_env.values():
        assert values == {
            META_CAPABILITY_PROVIDER_ENV: "openrouter",
            META_CAPABILITY_API_KEY_ENV: secret,
            META_CAPABILITY_BASE_URL_ENV: "https://openrouter.ai/api/v1",
            META_OPENROUTER_API_KEY_ENV: secret,
        }
    assert "paper-latex-sanitizer" not in runtime_env


def test_secondary_openrouter_profile_satisfies_paid_media_readiness(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    secret = "synthetic-secondary-openrouter-key"
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="tokenrhythm",
            model="deepseek-v4-pro",
            api_key="synthetic-primary-key",
            api_key_env="",
            base_url="https://tokenrhythm.studio/v1",
            proxy="",
        ),
        llm_profiles={
            "openrouter": SimpleNamespace(
                model="bytedance/seedance-2.0",
                api_key=secret,
                api_key_env="",
                api_key_env_pool=[],
                base_url="https://openrouter.ai/api/v1",
                proxy="",
            )
        },
    )
    meta, plan, _image, _video, skill_index = _trusted_short_drama_fixture()

    assert configured_meta_readiness_env_aliases(
        config,
        parent_spec=meta,
        plan=plan,
        skill_resolver=skill_index,
    ) == ("OPENROUTER_API_KEY",)
    runtime_env = configured_meta_skill_runtime_env(
        config,
        parent_spec=meta,
        plan=plan,
        session_key="agent:main:secondary-profile",
        skill_resolver=skill_index,
    )

    assert runtime_env["nano-banana-pro"][META_CAPABILITY_API_KEY_ENV] == secret
    assert runtime_env["seedance-2-prompt"][META_CAPABILITY_API_KEY_ENV] == secret
    assert secret not in repr(
        configured_meta_readiness_env_aliases(
            config,
            parent_spec=meta,
            plan=plan,
            skill_resolver=skill_index,
        )
    )


def test_readiness_env_alias_allowlist_tracks_capability_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta, plan, _image, _video, skill_index = _trusted_short_drama_fixture()
    current = capability_runtime._CONSUMER_REQUIREMENTS["nano-banana-pro"][0]
    expanded = replace(
        current,
        provider_candidates=(
            *current.provider_candidates,
            CapabilityProviderCandidate(
                provider_id="openai",
                model="synthetic-image-model",
            ),
        ),
        portable_env_aliases=(
            *current.portable_env_aliases,
            "OPENAI_API_KEY",
        ),
    )
    monkeypatch.setitem(
        capability_runtime._CONSUMER_REQUIREMENTS,
        "nano-banana-pro",
        (expanded,),
    )

    ctx = meta_readiness_context(
        env_aliases=("OPENAI_API_KEY", "UNTRUSTED_LOOKALIKE_KEY"),
        parent_spec=meta,
        plan=plan,
        skill_resolver=skill_index,
    )

    assert ctx.env_cache["OPENAI_API_KEY"] == "configured"
    assert "UNTRUSTED_LOOKALIKE_KEY" not in ctx.env_cache


def test_missing_media_provider_projects_generic_manual_setup_action(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    meta, plan, image, video, skill_index = _trusted_short_drama_fixture()
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="tokenrhythm",
            model="deepseek-v4-pro",
            api_key="synthetic-primary-only-key",
            base_url="https://tokenrhythm.studio/v1",
            proxy="",
        ),
        llm_profiles={},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index=skill_index,
        config=config,
        validated_plan=plan,
    )

    assert readiness.ready is False
    assert readiness.missing_provider_capabilities == (
        CAPABILITY_IMAGE_REFERENCE,
        CAPABILITY_VIDEO_GENERATE,
    )
    assert readiness.missing_capabilities == ()
    assert readiness.missing_env == ()
    assert readiness.missing_env_any == ()
    assert len(readiness.manual_setup_actions) == 1
    action = readiness.manual_setup_actions[0]
    assert action.kind == "provider_connection"
    assert action.provider_id == "openrouter"
    assert action.label == "OpenRouter"
    assert action.capability_ids == (
        CAPABILITY_IMAGE_REFERENCE,
        CAPABILITY_VIDEO_GENERATE,
    )
    payload = readiness.to_dict()
    assert payload["manual_setup_actions"][0]["id"] == "provider:openrouter"
    assert "synthetic-primary-only-key" not in repr(payload)
    assert "OPENROUTER_API_KEY" not in repr(payload)


def test_awesome_webpage_missing_key_uses_generic_provider_setup_handoff(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    loader = SkillLoader(
        bundled_dir=_BUNDLED,
        snapshot_path=tmp_path / "awesome-readiness-snapshot.json",
    )
    skill_index = {skill.name: skill for skill in loader.load_all()}
    meta = skill_index["AwesomeWebpageMetaSkill"]
    plan = parse_meta_plan(meta)
    assert plan is not None
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="tokenrhythm",
            model="synthetic-primary-model",
            api_key="synthetic-primary-key",
            api_key_env="",
            base_url="https://tokenrhythm.studio/v1",
            proxy="",
            provider_routing={},
        ),
        llm_profiles={},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index=skill_index,
        config=config,
        validated_plan=plan,
        verify_capabilities=False,
    )

    assert readiness.ready is False
    assert readiness.missing_provider_capabilities == (
        CAPABILITY_AUDIO_GENERATE,
        CAPABILITY_IMAGE_GENERATE,
        CAPABILITY_VIDEO_GENERATE,
    )
    assert readiness.missing_env == ()
    assert readiness.missing_env_any == ()
    assert len(readiness.manual_setup_actions) == 1
    action = readiness.manual_setup_actions[0]
    assert action.id == "provider:openrouter"
    assert action.kind == "provider_connection"
    assert action.provider_id == "openrouter"
    assert action.label == "OpenRouter"
    assert "synthetic-primary-key" not in repr(readiness.to_dict())


def test_awesome_webpage_secondary_profile_satisfies_readiness_and_runtime_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    loader = SkillLoader(
        bundled_dir=_BUNDLED,
        snapshot_path=tmp_path / "awesome-profile-snapshot.json",
    )
    skill_index = {skill.name: skill for skill in loader.load_all()}
    meta = skill_index["AwesomeWebpageMetaSkill"]
    plan = parse_meta_plan(meta)
    assert plan is not None
    secret = "synthetic-awesome-profile-key"
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="tokenrhythm",
            model="synthetic-primary-model",
            api_key="synthetic-primary-key",
            api_key_env="",
            base_url="https://tokenrhythm.studio/v1",
            proxy="",
            provider_routing={},
        ),
        llm_profiles={
            "openrouter": SimpleNamespace(
                model="synthetic-profile-model",
                api_key=secret,
                api_key_env="",
                api_key_env_pool=[],
                base_url="https://openrouter.ai/api/v1",
                proxy="",
            )
        },
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index=skill_index,
        config=config,
        validated_plan=plan,
        verify_capabilities=False,
    )
    runtime_env = configured_meta_skill_runtime_env(
        config,
        parent_spec=meta,
        plan=plan,
        session_key="agent:main:awesome-profile",
        skill_resolver=skill_index,
    )

    assert readiness.ready is True
    assert set(runtime_env) == {
        "audio-cog",
        "nano-banana-pro-openrouter",
        "openrouter-video-generator",
    }
    assert all(
        values[META_CAPABILITY_API_KEY_ENV] == secret
        for values in runtime_env.values()
    )
    assert secret not in repr(readiness.to_dict())


def test_invalid_media_provider_proxy_projects_secret_free_manual_action(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    meta, plan, image, video, skill_index = _trusted_short_drama_fixture()
    secret = "synthetic-invalid-proxy-readiness-key"
    proxy_secret = "synthetic-proxy-password"
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key=secret,
            api_key_env="",
            base_url="https://openrouter.ai/api/v1",
            proxy=f"http://proxy.example.test:8080/path?token={proxy_secret}",
            provider_routing={},
        ),
        llm_profiles={},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index=skill_index,
        config=config,
        validated_plan=plan,
    )

    assert readiness.ready is False
    assert len(readiness.manual_setup_actions) == 1
    action = readiness.manual_setup_actions[0]
    assert action.reason_code == "invalid_proxy"
    assert action.reason == "The configured provider proxy is invalid."
    payload = readiness.to_dict()
    assert secret not in repr(payload)
    assert proxy_secret not in repr(payload)


def test_workspace_parent_cannot_use_provider_alias_for_bundled_paid_children(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-key-must-not-authorize-parent")
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    meta, plan, image, video, skill_index = _trusted_short_drama_fixture(
        layer=SkillLayer.WORKSPACE,
    )
    provider_resolution_calls = 0

    def forbidden_provider_resolution(*_args, **_kwargs):
        nonlocal provider_resolution_calls
        provider_resolution_calls += 1
        raise AssertionError("untrusted parent must not resolve a capability lease")

    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.resolve_capability_status",
        forbidden_provider_resolution,
    )
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="synthetic-config-key-must-not-authorize-parent",
            api_key_env="",
            base_url="https://openrouter.ai/api/v1",
            proxy="",
            provider_routing={},
        ),
        llm_profiles={},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index=skill_index,
        config=config,
        validated_plan=plan,
    )

    assert readiness.ready is False
    assert readiness.missing_provider_capabilities == ()
    assert readiness.manual_setup_actions == ()
    assert ("OPENROUTER_API_KEY",) in readiness.missing_env_any
    assert ("OPENROUTER_API_KEY", "ARK_API_KEY") in readiness.missing_env_any
    assert provider_resolution_calls == 0


def test_shadowed_review_normalizer_has_actionable_fail_closed_readiness() -> None:
    meta, plan, _image, _video, skill_index = _trusted_short_drama_fixture()
    skill_index["short-drama-review-normalizer"] = replace(
        skill_index["short-drama-review-normalizer"],
        layer=SkillLayer.WORKSPACE,
        base_dir="/synthetic/workspace/short-drama-review-normalizer",
    )
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="openrouter",
            model="synthetic-model",
            api_key="synthetic-config-key",
            api_key_env="",
            base_url="https://openrouter.ai/api/v1",
            proxy="",
            provider_routing={},
        ),
        llm_profiles={},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index=skill_index,
        config=config,
        validated_plan=plan,
    )

    assert readiness.ready is False
    assert readiness.missing_provider_capabilities == ()
    assert readiness.manual_setup_actions == ()
    assert readiness.reasons == (
        "A trusted provider workflow component is overridden by a higher-priority "
        "skill source. Remove or rename that override before running this MetaSkill.",
    )


def test_blank_environment_value_does_not_satisfy_readiness(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "   ")
    meta = _spec("meta-video", kind="meta", env_any=["OPENROUTER_API_KEY"])

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index={meta.name: meta},
        ctx=EligibilityContext.auto(),
    )

    assert readiness.ready is False
    assert readiness.missing_env_any == (("OPENROUTER_API_KEY",),)


def test_config_credential_alias_fails_closed_for_wrong_provider_or_empty_env(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_TEST_EMPTY_OPENROUTER_KEY", raising=False)
    wrong_provider = SimpleNamespace(
        llm=SimpleNamespace(
            provider="tokenrhythm",
            api_key="configured-but-not-for-openrouter",
            api_key_env="",
        )
    )
    missing_custom_env = SimpleNamespace(
        llm=SimpleNamespace(
            provider="openrouter",
            api_key="",
            api_key_env="OPENSQUILLA_TEST_EMPTY_OPENROUTER_KEY",
        )
    )
    meta, plan, image, video, skill_index = _trusted_short_drama_fixture()

    for config in (wrong_provider, missing_custom_env):
        assert configured_meta_readiness_env_aliases(
            config,
            parent_spec=meta,
            plan=plan,
            skill_resolver=skill_index,
        ) == ()
        readiness = assess_meta_skill_readiness(
            meta,
            skill_index=skill_index,
            ctx=meta_readiness_context(
                config=config,
                parent_spec=meta,
                plan=plan,
                skill_resolver=skill_index,
            ),
            config=config,
            validated_plan=plan,
        )
        assert readiness.ready is False
        assert readiness.missing_provider_capabilities == (
            CAPABILITY_IMAGE_REFERENCE,
            CAPABILITY_VIDEO_GENERATE,
        )


def test_readiness_rolls_up_sub_skill_binary_requirements() -> None:
    child = _spec("renderer", bins=["ffmpeg", "ffprobe"])
    meta = _spec(
        "meta-video",
        kind="meta",
        composition={"steps": [{"id": "render", "kind": "skill_exec", "skill": "renderer"}]},
    )
    ctx = EligibilityContext(
        os_name="linux",
        has_bin_cache={"ffmpeg": False, "ffprobe": False},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index={meta.name: meta, child.name: child},
        ctx=ctx,
    )

    assert readiness.ready is False
    assert readiness.status == "needs_setup"
    assert readiness.missing_bins == ("ffmpeg", "ffprobe")


def test_readiness_passes_when_composed_requirements_are_satisfied() -> None:
    child = _spec("compiler", bins=["xelatex", "bibtex"])
    meta = _spec(
        "meta-paper",
        kind="meta",
        composition={"steps": [{"id": "compile", "skill": "compiler"}]},
    )
    ctx = EligibilityContext(
        os_name="linux",
        has_bin_cache={"xelatex": True, "bibtex": True},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index={meta.name: meta, child.name: child},
        ctx=ctx,
    )

    assert readiness.ready is True
    assert readiness.missing_bins == ()


def test_readiness_blocks_missing_composed_skill_reference() -> None:
    meta = _spec(
        "meta-broken",
        kind="meta",
        composition={"steps": [{"id": "missing", "skill": "not-installed"}]},
    )

    readiness = assess_meta_skill_readiness(meta, skill_index={meta.name: meta})

    assert readiness.ready is False
    assert readiness.missing_skills == ("not-installed",)


def test_readiness_recurses_through_conditional_route_targets(monkeypatch) -> None:
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    leaf = _spec("route-leaf", bins=["route-renderer"])
    nested = _spec(
        "nested-meta",
        kind="meta",
        composition={
            "steps": [
                {
                    "id": "select-renderer",
                    "kind": "skill_exec",
                    "skill": "route-leaf",
                    "route": [
                        {"when": "inputs.use_fallback", "to": "fallback-leaf"},
                    ],
                },
            ],
        },
    )
    fallback = _spec("fallback-leaf", bins=["fallback-renderer"])
    root = _spec(
        "root-meta",
        kind="meta",
        composition={
            "steps": [
                {"id": "nested", "kind": "skill_exec", "skill": "nested-meta"},
            ],
        },
    )
    ctx = EligibilityContext(
        os_name="linux",
        has_bin_cache={"fallback-renderer": False, "route-renderer": False},
    )

    readiness = assess_meta_skill_readiness(
        root,
        skill_index={
            root.name: root,
            nested.name: nested,
            leaf.name: leaf,
            fallback.name: fallback,
        },
        ctx=ctx,
    )

    assert readiness.ready is False
    assert readiness.missing_bins == ("fallback-renderer", "route-renderer")
    assert readiness.missing_skills == ()


def test_readiness_reports_missing_conditional_route_targets_deterministically(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    default = _spec("default-renderer")
    meta = _spec(
        "meta-routes",
        kind="meta",
        composition={
            "steps": [
                {
                    "id": "route",
                    "kind": "agent",
                    "skill": "default-renderer",
                    "route": [
                        {"when": "inputs.format == 'video'", "to": "z-video-renderer"},
                        {"when": "inputs.format == 'audio'", "to": "a-audio-renderer"},
                        {"when": "inputs.retry", "to": "z-video-renderer"},
                    ],
                },
            ],
        },
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index={meta.name: meta, default.name: default},
    )

    assert readiness.ready is False
    assert readiness.missing_skills == ("a-audio-renderer", "z-video-renderer")
    assert readiness.reasons == (
        "Unavailable sub-skill: a-audio-renderer",
        "Unavailable sub-skill: z-video-renderer",
    )


def test_readiness_retains_legacy_routes_skill_references(monkeypatch) -> None:
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    legacy_child = _spec("legacy-child", bins=["legacy-tool"])
    meta = _spec(
        "legacy-meta",
        kind="meta",
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "routes": [{"label": "legacy", "skill": "legacy-child"}],
                },
            ],
        },
    )
    ctx = EligibilityContext(
        os_name="linux",
        has_bin_cache={"legacy-tool": False},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index={meta.name: meta, legacy_child.name: legacy_child},
        ctx=ctx,
    )

    assert readiness.ready is False
    assert readiness.missing_bins == ("legacy-tool",)
    assert readiness.missing_skills == ()


def test_readiness_ignores_non_skill_step_destinations(monkeypatch) -> None:
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.is_skill_available_live",
        lambda _name: True,
    )
    meta = _spec(
        "classifier-meta",
        kind="meta",
        composition={
            "steps": [
                {
                    "id": "classify",
                    "kind": "llm_classify",
                    "skill": "informational-classifier-name",
                    "route": [
                        {"when": "inputs.kind == 'report'", "to": "report-label"},
                    ],
                },
            ],
        },
    )

    readiness = assess_meta_skill_readiness(meta, skill_index={meta.name: meta})

    assert readiness.ready is True
    assert readiness.missing_skills == ()


def test_readiness_projects_trusted_setup_action(monkeypatch) -> None:
    monkeypatch.setattr(
        "opensquilla.skills.meta.readiness.shutil.which",
        lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
    )
    meta = _spec(
        "meta-video",
        kind="meta",
        bins=["ffmpeg", "ffprobe"],
        install=[
            SkillInstallSpec(
                kind="brew",
                id="ffmpeg-full-homebrew",
                label="Install FFmpeg Full",
                bins=["ffmpeg", "ffprobe"],
                os=["darwin"],
                formula="ffmpeg-full",
            )
        ],
    )
    ctx = EligibilityContext(
        os_name="darwin",
        has_bin_cache={"ffmpeg": False, "ffprobe": False},
    )

    readiness = assess_meta_skill_readiness(meta, skill_index={meta.name: meta}, ctx=ctx)

    assert readiness.ready is False
    assert len(readiness.setup_actions) == 1
    action = readiness.setup_actions[0]
    assert action.id == "meta-video:ffmpeg-full-homebrew"
    assert action.skill == "meta-video"
    assert action.install_id == "ffmpeg-full-homebrew"
    assert action.available is True
    assert action.bins == ("ffmpeg", "ffprobe")
    assert action.to_dict()["requires_admin"] is False


def test_readiness_deduplicates_shared_managed_component_across_meta_and_child(
    monkeypatch,
) -> None:
    install_parent = SkillInstallSpec(
        kind="toolchain",
        id="media-ffmpeg",
        label="Install verified FFmpeg toolchain",
        bins=["ffmpeg", "ffprobe"],
        os=["darwin", "linux", "windows"],
    )
    install_child = SkillInstallSpec(
        kind="toolchain",
        id="media-ffmpeg",
        label="Install verified FFmpeg toolchain",
        bins=["ffprobe"],
        os=["darwin", "linux", "windows"],
    )
    child = _spec(
        "delivery-audit",
        bins=["ffprobe"],
        install=[install_child],
    )
    meta = _spec(
        "meta-video",
        kind="meta",
        bins=["ffmpeg", "ffprobe"],
        install=[install_parent],
        composition={
            "steps": [{"id": "audit", "kind": "skill_exec", "skill": child.name}],
        },
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.probe_component",
        lambda component_id: SimpleNamespace(
            component_id=component_id,
            ready=False,
            reason="Noto CJK font is missing",
        ),
    )
    ctx = EligibilityContext(
        os_name="darwin",
        has_bin_cache={"ffmpeg": False, "ffprobe": False},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index={meta.name: meta, child.name: child},
        ctx=ctx,
    )

    assert [action.id for action in readiness.setup_actions] == [
        "meta-video:media-ffmpeg"
    ]
    assert readiness.setup_actions[0].bins == ("ffmpeg", "ffprobe")


def test_readiness_marks_wrong_platform_action_unavailable() -> None:
    meta = _spec(
        "meta-video",
        kind="meta",
        bins=["ffmpeg"],
        install=[
            SkillInstallSpec(
                kind="brew",
                id="ffmpeg-full-homebrew",
                bins=["ffmpeg"],
                os=["darwin"],
            )
        ],
    )
    ctx = EligibilityContext(os_name="linux", has_bin_cache={"ffmpeg": False})

    readiness = assess_meta_skill_readiness(meta, skill_index={meta.name: meta}, ctx=ctx)

    assert readiness.setup_actions[0].available is False
    assert "linux" in readiness.setup_actions[0].reason


def test_readiness_blocks_present_binaries_when_toolchain_capability_is_incomplete(
    monkeypatch,
) -> None:
    meta = _spec(
        "meta-video",
        kind="meta",
        bins=["ffmpeg", "ffprobe"],
        install=[
            SkillInstallSpec(
                kind="toolchain",
                id="media-ffmpeg",
                label="Install verified FFmpeg toolchain",
                bins=["ffmpeg", "ffprobe"],
                os=["darwin", "linux", "windows"],
            )
        ],
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.probe_component",
        lambda component_id: SimpleNamespace(
            component_id=component_id,
            ready=False,
            reason="FFmpeg is missing the subtitles filter",
        ),
    )
    ctx = EligibilityContext(
        os_name="darwin",
        has_bin_cache={"ffmpeg": True, "ffprobe": True},
    )

    readiness = assess_meta_skill_readiness(meta, skill_index={meta.name: meta}, ctx=ctx)

    assert readiness.ready is False
    assert readiness.missing_bins == ()
    assert readiness.missing_capabilities == ("media-ffmpeg",)
    assert "subtitles filter" in " ".join(readiness.reasons)
    assert [action.id for action in readiness.setup_actions] == [
        "meta-video:media-ffmpeg"
    ]
    assert readiness.setup_actions[0].bins == ("ffmpeg", "ffprobe")


def test_readiness_accepts_present_binaries_after_capability_probe_passes(monkeypatch) -> None:
    meta = _spec(
        "meta-paper",
        kind="meta",
        bins=["xelatex", "bibtex"],
        install=[
            SkillInstallSpec(
                kind="toolchain",
                id="paper-tex",
                bins=["xelatex", "bibtex"],
            )
        ],
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.probe_component",
        lambda component_id: SimpleNamespace(
            component_id=component_id,
            ready=True,
            reason="ready",
        ),
    )
    ctx = EligibilityContext(
        os_name="darwin",
        has_bin_cache={"xelatex": True, "bibtex": True},
    )

    readiness = assess_meta_skill_readiness(meta, skill_index={meta.name: meta}, ctx=ctx)

    assert readiness.ready is True
    assert readiness.missing_capabilities == ()
    assert readiness.setup_actions == ()


def test_passive_readiness_never_runs_native_capability_probe(monkeypatch) -> None:
    meta = _spec(
        "meta-video",
        kind="meta",
        bins=["ffmpeg", "ffprobe"],
        install=[
            SkillInstallSpec(
                kind="toolchain",
                id="media-ffmpeg",
                bins=["ffmpeg", "ffprobe"],
            )
        ],
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.probe_component",
        lambda _component_id: (_ for _ in ()).throw(
            AssertionError("passive readiness must not execute a native probe")
        ),
    )
    ctx = EligibilityContext(
        os_name="linux",
        has_bin_cache={"ffmpeg": True, "ffprobe": True},
    )

    readiness = assess_meta_skill_readiness(
        meta,
        skill_index={meta.name: meta},
        ctx=ctx,
        verify_capabilities=False,
    )

    assert readiness.ready is True
    assert readiness.missing_capabilities == ()


def test_external_toolchain_discloses_pinned_auxiliary_download_as_minimum(
    monkeypatch,
) -> None:
    meta = _spec(
        "meta-video",
        kind="meta",
        bins=["ffmpeg", "ffprobe"],
        install=[
            SkillInstallSpec(
                kind="toolchain",
                id="media-ffmpeg",
                bins=["ffmpeg", "ffprobe"],
            )
        ],
    )
    descriptor = SimpleNamespace(
        supported=True,
        unsupported_reason=None,
        install_backend="brew",
        version="homebrew-stable",
        total_download_size=None,
        closure_source=None,
        auxiliary_assets=(SimpleNamespace(size=19_484_784), SimpleNamespace(size=4_301)),
        source="https://formulae.brew.sh/formula/ffmpeg-full",
        license="GPL-3.0-or-later",
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.describe_component",
        lambda _component_id: descriptor,
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.probe_component",
        lambda component_id: SimpleNamespace(
            component_id=component_id,
            ready=False,
            reason="Missing runtime capabilities: noto-cjk-font",
        ),
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.trusted_brew_executable",
        lambda: SimpleNamespace(),
    )
    ctx = EligibilityContext(
        os_name="darwin",
        has_bin_cache={"ffmpeg": True, "ffprobe": True},
    )

    readiness = assess_meta_skill_readiness(meta, skill_index={meta.name: meta}, ctx=ctx)

    action = readiness.setup_actions[0]
    assert action.download_size_bytes == 19_489_085
    assert action.download_size_is_minimum is True


def test_external_toolchain_is_unavailable_without_trusted_homebrew(monkeypatch) -> None:
    meta = _spec(
        "meta-video",
        kind="meta",
        bins=["ffmpeg", "ffprobe"],
        install=[
            SkillInstallSpec(
                kind="toolchain",
                id="media-ffmpeg",
                bins=["ffmpeg", "ffprobe"],
            )
        ],
    )
    descriptor = SimpleNamespace(
        supported=True,
        unsupported_reason=None,
        install_backend="brew",
        version="homebrew-stable",
        total_download_size=None,
        closure_source=None,
        auxiliary_assets=(),
        source="https://formulae.brew.sh/formula/ffmpeg-full",
        license="GPL-3.0-or-later",
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.describe_component",
        lambda _component_id: descriptor,
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.trusted_brew_executable",
        lambda: None,
    )
    monkeypatch.setattr(
        "opensquilla.skills.toolchains.probe_component",
        lambda component_id: SimpleNamespace(
            component_id=component_id,
            ready=False,
            reason="FFmpeg is not installed",
        ),
    )
    ctx = EligibilityContext(
        os_name="darwin",
        has_bin_cache={"ffmpeg": False, "ffprobe": False},
    )

    readiness = assess_meta_skill_readiness(meta, skill_index={meta.name: meta}, ctx=ctx)

    assert readiness.ready is False
    action = readiness.setup_actions[0]
    assert action.available is False
    assert "Homebrew" in action.reason
    assert "trusted" in action.reason
