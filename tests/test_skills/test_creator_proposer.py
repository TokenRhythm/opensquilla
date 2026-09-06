"""Tests for creator/proposer.py (fill_slots, assemble) + patterns."""

from __future__ import annotations

import json

import pytest

from opensquilla.skills.creator.patterns.schemas import (
    FanOutMergeSlots,
    SequentialSlots,
)
from opensquilla.skills.creator.proposer import meta_skill_assemble


def test_sequential_slots_min_steps() -> None:
    with pytest.raises(ValueError):
        SequentialSlots(
            name="test-x", description="d" * 30, triggers=["t"],
            steps=[{"id": "a", "skill": "x", "task": "t"}],
        )


def test_sequential_with_keys_default_empty() -> None:
    slots = SequentialSlots(
        name="test-x", description="d" * 30, triggers=["t"],
        steps=[
            {"id": "a", "skill": "summarize", "task": "do thing"},
            {"id": "b", "skill": "memory", "task": "save"},
        ],
    )
    assert slots.steps[0].with_keys == {}


def test_fanout_tail_optional() -> None:
    slots = FanOutMergeSlots(
        name="test-x", description="d" * 30, triggers=["t"],
        branches=[
            {"id": "a", "skill": "weather", "task": "t"},
            {"id": "b", "skill": "summarize", "task": "t"},
        ],
        merge={"id": "m", "skill": "summarize", "task": "t"},
    )
    assert slots.tail is None


def test_meta_skill_assemble_p1() -> None:
    slots = {
        "name": "test-t1", "description": "d" * 30, "triggers": ["go"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "extract", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "store", "with_keys": {}},
        ],
        "meta_priority": 50,
    }
    md = meta_skill_assemble("p1_sequential", json.dumps(slots))
    # N2: tojson wraps values in JSON double-quotes (valid YAML scalars)
    assert 'name: "test-t1"' in md
    assert 'skill: "summarize"' in md
    assert 'skill: "memory"' in md
    assert "depends_on: [a]" in md


def test_meta_skill_assemble_rejects_invalid_slots() -> None:
    with pytest.raises(ValueError):
        meta_skill_assemble("p1_sequential", '{"name": "x"}')


def test_meta_skill_fill_slots_with_stub_llm(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    call_log: list[str] = []
    canned_response = json.dumps({
        "name": "synth-pipeline",
        "description": "Synthetic pipeline that does X then Y. Sample for testing fill_slots flow.",
        "meta_priority": 50,
        "triggers": ["synth test"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "process", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "save", "with_keys": {}},
        ],
    })

    def stub_llm(prompt: str, **_kwargs) -> str:
        call_log.append(prompt)
        return canned_response

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_llm)

    result = proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(no history)",
        user_intent="process docs then save",
    )
    data = json.loads(result)
    assert data["name"] == "synth-pipeline"
    assert len(call_log) == 1
    # Catalog injection: skill names must appear in prompt
    assert "summarize" in call_log[0]


def test_creator_package_import_registers_tools() -> None:
    """C1 regression: importing the creator package must register both tools
    in the default ToolRegistry. Phase 1 cross-task review found that the
    @tool decorators only run when the module is imported — production code
    must import opensquilla.skills.creator somewhere in the meta-skill branch."""
    import importlib

    import opensquilla.skills.creator
    importlib.reload(opensquilla.skills.creator)

    from opensquilla.tools.registry import get_default_registry
    names = get_default_registry().list_names()
    meta_names = sorted(n for n in names if n.startswith("meta"))
    assert "meta_skill_assemble" in names, (
        f"meta_skill_assemble not registered; got: {meta_names}"
    )
    assert "meta_skill_fill_slots" in names, "meta_skill_fill_slots not registered"


def test_meta_skill_fill_slots_retries_once_on_validation_error(monkeypatch) -> None:
    from opensquilla.skills.creator import proposer

    responses = iter([
        '{"name": "bad"}',  # missing fields → ValidationError
        json.dumps({
            "name": "synth-pipeline",
            "description": "Synthetic pipeline that does X then Y. Sample.",
            "meta_priority": 50,
            "triggers": ["synth test"],
            "steps": [
                {"id": "a", "skill": "summarize", "task": "process", "with_keys": {}},
                {"id": "b", "skill": "memory", "task": "save", "with_keys": {}},
            ],
        }),
    ])
    prompts: list[str] = []

    def stub_llm(prompt: str, **_kwargs) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_llm)

    result = proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(no history)",
        user_intent="process docs then save",
    )
    data = json.loads(result)
    assert data["name"] == "synth-pipeline"
    assert len(prompts) == 2
    # Retry prompt must include the ValidationError feedback
    assert "failed schema validation" in prompts[1] or "errors" in prompts[1]


def test_creator_tools_hidden_from_owner_default() -> None:
    """N1: meta_skill_{assemble,fill_slots} must NOT appear in the default
    owner tool catalog. They are internal orchestrator-only tools."""
    import importlib

    import opensquilla.skills.creator  # trigger @tool registration
    importlib.reload(opensquilla.skills.creator)

    from opensquilla.tools.registry import ToolContext, get_default_registry

    reg = get_default_registry()
    # Use the default owner context (is_owner=True, no allowed_tools override).
    # _iter_visible_tools with this context filters out default_access="deny".
    ctx = ToolContext(is_owner=True)
    visible_names = {rt.spec.name for rt in reg._iter_visible_tools(ctx)}

    for tool_name in ("meta_skill_assemble", "meta_skill_fill_slots"):
        assert tool_name not in visible_names, (
            f"{tool_name} is visible in the default owner tool catalog; "
            "N1 fix requires default_access=deny so C1 lazy import "
            "does not leak it into normal owner turns."
        )

    # But the tools must still be registered (reachable by name for tool_invoker).
    registered_names = set(reg.list_names())
    assert "meta_skill_assemble" in registered_names
    assert "meta_skill_fill_slots" in registered_names


def test_resolve_provider_config_honors_env_overrides(monkeypatch, tmp_path) -> None:
    """N14: GatewayConfig.load() honours OPENSQUILLA_LLM_* env vars; creator
    must respect the same resolution path."""
    # Point to a non-existent config so the TOML path is empty but env wins.
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "nope.toml"))
    monkeypatch.setenv("OPENSQUILLA_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENSQUILLA_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "test-key")

    from opensquilla.skills.creator.proposer import _resolve_provider_from_config
    provider, model, api_key, base_url = _resolve_provider_from_config()
    assert provider == "openai"
    assert model == "gpt-4o-mini"
    assert api_key == "test-key"


def test_resolve_provider_config_includes_base_url(monkeypatch, tmp_path) -> None:
    """N14: base_url must flow through (vllm/azure require custom endpoint)."""
    toml = tmp_path / "config.toml"
    toml.write_text(
        "[llm]\n"
        'provider = "openai"\n'
        'model = "gpt-4o-mini"\n'
        'api_key = ""\n'
        'base_url = "https://my-vllm.local/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(toml))
    # Ensure no env-LLM override interferes.
    monkeypatch.delenv("OPENSQUILLA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_MODEL", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENSQUILLA_LLM_BASE_URL", raising=False)

    from opensquilla.skills.creator.proposer import _resolve_provider_from_config
    provider, model, api_key, base_url = _resolve_provider_from_config()
    assert provider == "openai"
    assert base_url == "https://my-vllm.local/v1"


def test_slot_filler_rejects_yaml_unsafe_strings() -> None:
    """N2: Pydantic validators reject control chars / quotes that would
    break YAML rendering."""
    import pytest as _pytest

    from opensquilla.skills.creator.patterns.schemas import SequentialStep

    # Acceptable
    SequentialStep(id="ok", skill="summarize", task="simple task")

    # Unacceptable: double quote in task
    with _pytest.raises(ValueError):
        SequentialStep(id="ok", skill="summarize", task='save "summary"')

    # Unacceptable: newline in task
    with _pytest.raises(ValueError):
        SequentialStep(id="ok", skill="summarize", task="step 1\nstep 2")

    # Unacceptable: backslash in task
    with _pytest.raises(ValueError):
        SequentialStep(id="ok", skill="summarize", task="path\\to\\file")

    # Unacceptable: double quote in skill name
    with _pytest.raises(ValueError):
        SequentialStep(id="ok", skill='sum"marize', task="simple task")


def test_fill_slots_retry_no_type_error_on_custom_validator_error(monkeypatch) -> None:
    """N4 regression: Pydantic v2 custom-validator errors put a raw ValueError
    object in ctx.error which is not JSON-serializable. The retry path must use
    default=str so json.dumps(exc.errors()) doesn't TypeError before the retry
    LLM call fires.

    Triggers the N2 validator (double-quote in task) on the first response,
    then returns a clean payload on the second call. Asserts no TypeError is
    raised and the final result is the clean payload.
    """
    import json as _json

    from opensquilla.skills.creator import proposer

    clean_payload = _json.dumps({
        "name": "synth-pipeline",
        "description": "Synthetic pipeline that does X then Y. Sample for N4 regression.",
        "meta_priority": 50,
        "triggers": ["synth test"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "process input", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "save result", "with_keys": {}},
        ],
    })
    # First response: task contains a double-quote — triggers the N2
    # custom validator on SequentialStep and raises ValidationError whose
    # exc.errors() contains a raw ValueError in ctx.error.
    bad_payload = _json.dumps({
        "name": "synth-pipeline",
        "description": "Synthetic pipeline. Sample for N4 regression.",
        "meta_priority": 50,
        "triggers": ["synth test"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": 'save "summary"', "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "save result", "with_keys": {}},
        ],
    })

    responses = iter([bad_payload, clean_payload])

    def stub_llm(prompt: str, **_kwargs) -> str:
        return next(responses)

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_llm)

    # Must not raise TypeError; must return clean payload
    result = proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(no history)",
        user_intent="process docs then save",
    )
    data = _json.loads(result)
    assert data["name"] == "synth-pipeline", f"unexpected result: {data}"


def test_creator_tools_registered_via_meta_invoke_module_import() -> None:
    """N10: importing the meta_invoke soft-path module (agent.py) must also
    ensure creator tools are registered. The lazy import added at the top of
    _run_meta_invoke_streaming fires whenever the method is entered; here we
    verify the underlying registration by importing opensquilla.skills.creator
    directly (the same effect as the lazy import) and asserting the registry
    reflects the tools — mirrors the C1 hard-takeover test but for the
    soft-path entry in agent.py.
    """
    import importlib

    # Simulate what the N10 lazy import does when _run_meta_invoke_streaming fires.
    import opensquilla.skills.creator  # noqa: F401
    importlib.reload(opensquilla.skills.creator)

    from opensquilla.tools.registry import get_default_registry

    reg = get_default_registry()
    names = reg.list_names()
    assert "meta_skill_fill_slots" in names, (
        "N10: meta_skill_fill_slots not registered via soft-path import; "
        f"registered names starting with 'meta': "
        f"{sorted(n for n in names if n.startswith('meta'))}"
    )
    assert "meta_skill_assemble" in names, (
        "N10: meta_skill_assemble not registered via soft-path import"
    )


def test_strip_code_fences_handles_json_lang_tag() -> None:
    from opensquilla.skills.creator.proposer import _strip_code_fences

    assert _strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fences('```JSON\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert _strip_code_fences('{"a": 1}') == '{"a": 1}'
    # Whitespace tolerance
    assert _strip_code_fences('  ```json\n{"a": 1}\n```  ') == '{"a": 1}'


def test_fill_slots_strips_code_fence_before_parsing(monkeypatch) -> None:
    """Fix #A: code-fence-wrapped JSON should parse successfully."""
    from opensquilla.skills.creator import proposer

    canned = json.dumps({
        "name": "fenced-test",
        "description": "test description that meets min length requirement for schema",
        "meta_priority": 50,
        "triggers": ["fenced trigger"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "process", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "save", "with_keys": {}},
        ],
    })
    fenced = f"```json\n{canned}\n```"
    monkeypatch.setattr(proposer, "_call_llm_for_slots", lambda prompt, **_: fenced)

    result = proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(test)",
        user_intent="test intent",
    )
    parsed = json.loads(result)
    assert parsed["name"] == "fenced-test"


def test_fill_slots_validation_error_surfaces_detail(monkeypatch) -> None:
    """Fix #B: ValidationError after retry should include actionable detail
    (response preview + error message), not just generic 'internal error'."""
    from opensquilla.skills.creator import proposer

    monkeypatch.setattr(
        proposer, "_call_llm_for_slots",
        lambda prompt, **_: '{"name": "missing-fields-test"}',  # always invalid
    )

    with pytest.raises((ValueError, Exception)) as exc_info:
        proposer.meta_skill_fill_slots(
            pattern_id="p1_sequential",
            history_summary="(test)",
            user_intent="test",
        )
    err_str = str(exc_info.value)
    # The error message must include the pattern_id and a hint of what failed
    assert "p1_sequential" in err_str or "missing-fields-test" in err_str


def test_fill_slots_prompt_includes_schema_and_example(monkeypatch) -> None:
    """Fix #A: the prompt must include the JSON schema + a concrete example
    so the LLM cannot hallucinate field names like `execution_sequence`.

    Specifically asserts:
    - `triggers` (correct field name) appears in the prompt
    - `steps` (correct field name) appears in the prompt
    - `execution_sequence` appears as an anti-pattern warning (DO NOT use)
    - an example anchors the output (example-pipeline or pdf-toolkit)
    """
    from opensquilla.skills.creator import proposer

    captured: list[str] = []
    canned_resp = json.dumps({
        "name": "ok-pipeline",
        "description": "x" * 50,
        "meta_priority": 50,
        "triggers": ["t"],
        "steps": [
            {"id": "a", "skill": "summarize", "task": "t", "with_keys": {}},
            {"id": "b", "skill": "memory", "task": "t", "with_keys": {}},
        ],
    })

    def stub_with_capture(prompt: str, **_) -> str:
        captured.append(prompt)
        return canned_resp

    monkeypatch.setattr(proposer, "_call_llm_for_slots", stub_with_capture)
    proposer.meta_skill_fill_slots(
        pattern_id="p1_sequential",
        history_summary="(test)",
        user_intent="test",
    )

    assert captured, "stub was never called"
    prompt = captured[0]
    # Schema field names must appear in the prompt
    assert "triggers" in prompt, "prompt missing 'triggers' field name"
    assert '"steps"' in prompt or "'steps'" in prompt or "steps" in prompt, (
        "prompt missing 'steps' field name"
    )
    # Anti-pattern warning must name the wrong field so LLM is explicitly told not to use it
    assert "execution_sequence" in prompt, (
        "prompt must warn against 'execution_sequence' so LLM does not invent it"
    )
    # Example must anchor the output with concrete field values
    assert "example-pipeline" in prompt or "pdf-toolkit" in prompt, (
        "prompt missing example anchor (example-pipeline or pdf-toolkit)"
    )


def test_resolve_provider_config_accepts_empty_api_key(tmp_path, monkeypatch) -> None:
    """N11: _resolve_provider_from_config must return a valid triple when
    provider and model are set but api_key is absent (keyless local providers
    such as ollama / lm_studio). Previously the `and api_key` truthy guard
    returned (None, None, None), causing the resolution to fall through to
    env-var scan and ultimately raise RuntimeError on keyless deployments."""
    config_toml = tmp_path / "opensquilla.toml"
    config_toml.write_text(
        '[llm]\nprovider = "ollama"\nmodel = "llama3"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_toml))

    from opensquilla.skills.creator.proposer import _resolve_provider_from_config

    provider, model, api_key, base_url = _resolve_provider_from_config()
    assert provider == "ollama", (
        f"N11: expected 'ollama', got {provider!r}; "
        "keyless provider must not be rejected by _resolve_provider_from_config"
    )
    assert model == "llama3"
    assert api_key == ""  # empty string is correct for ollama


def test_resolve_provider_config_env_override_beats_toml(tmp_path, monkeypatch) -> None:
    """Fix #C: OPENSQUILLA_LLM_MODEL env var must win over a TOML [llm] section.

    When a config.toml has [llm] provider/model values, pydantic-settings'
    nested env binding is bypassed (the parent passes the TOML dict directly).
    _resolve_provider_from_config must apply a post-override so the env vars
    always beat TOML content.
    """
    config_toml = tmp_path / "opensquilla.toml"
    config_toml.write_text(
        '[llm]\nprovider = "openrouter"\nmodel = "deepseek/deepseek-v3.1-terminus"\n'
        'api_key = "toml-key"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_toml))
    monkeypatch.setenv("OPENSQUILLA_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENSQUILLA_LLM_MODEL", "claude-3-5-haiku-20241022")
    monkeypatch.setenv("OPENSQUILLA_LLM_API_KEY", "env-key")

    from opensquilla.skills.creator.proposer import _resolve_provider_from_config

    provider, model, api_key, base_url = _resolve_provider_from_config()
    assert provider == "anthropic", (
        f"Fix #C: env var OPENSQUILLA_LLM_PROVIDER must beat TOML; got {provider!r}"
    )
    assert model == "claude-3-5-haiku-20241022", (
        f"Fix #C: env var OPENSQUILLA_LLM_MODEL must beat TOML; got {model!r}"
    )
    assert api_key == "env-key", (
        f"Fix #C: env var OPENSQUILLA_LLM_API_KEY must beat TOML; got {api_key!r}"
    )


@pytest.mark.asyncio
async def test_fill_slots_tool_validation_error_returns_structured_json(monkeypatch) -> None:
    """Fix #B (Option B1): when fill_slots raises _FillSlotsValidationError after
    exhausting all retries, the @tool wrapper must catch it and return a structured
    JSON error dict rather than letting it propagate as a generic 'internal error'
    through the envelope layer."""
    from opensquilla.skills.creator import proposer

    # Always return invalid JSON to exhaust retries
    monkeypatch.setattr(
        proposer, "_call_llm_for_slots",
        lambda prompt, **_: '{"name": "x"}',  # missing required fields
    )

    result = await proposer.meta_skill_fill_slots_tool(
        pattern_id="p1_sequential",
        history_summary="(test)",
        user_intent="test",
    )
    # The tool must not raise; it returns JSON with _creator_error key
    payload = json.loads(result)
    assert payload.get("_creator_error") == "validation_failed_after_retry", (
        f"Fix #B: expected _creator_error='validation_failed_after_retry', got: {payload}"
    )
    assert "p1_sequential" in payload.get("pattern_id", ""), (
        f"Fix #B: pattern_id missing from error payload: {payload}"
    )
    assert "detail" in payload, f"Fix #B: 'detail' key missing from error payload: {payload}"
