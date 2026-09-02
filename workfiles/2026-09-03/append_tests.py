from pathlib import Path

target = Path(r"D:\AIstudio\Harness\OpenSquilla-QinLuza-Studio\tests\test_provider_model_catalog.py")
src = target.read_text(encoding="utf-8")

# Idempotency guard: skip if already appended.
if "test_inferred_source_labelled_distinctly" in src:
    print("tests already present, skip")
    raise SystemExit(0)

addition = '''

def test_inferred_source_labelled_distinctly() -> None:
    """Inference returns the dedicated "inferred" source, not "default".

    Ensemble member budget rebinding trusts explicit sources
    (override/config/catalog/inferred) but historically skipped "default";
    relabelling inference as "inferred" lets it join that trust set while
    the hardcoded fallback stays excluded.
    """
    catalog = ModelCatalog()
    window, source = catalog.resolve_context_window_with_source(
        "glm-5.3-flash", provider="tokenrhythm"
    )
    assert window == 1_000_000
    assert source == "inferred"


def test_local_runtime_window_wins_over_inference() -> None:
    """Local runtimes (ollama etc.) keep the conservative runtime window.

    Unqualified local ids like "llama3:3b" and "qwen3:8b" carry patterns
    inference would match, but local deployments must never inherit a
    cloud-scale guessed window; the runtime's own conservative default
    stays authoritative.
    """
    from opensquilla.provider.model_catalog import _LOCAL_CONTEXT_WINDOW

    catalog = ModelCatalog()
    for local_id in ("llama3:3b", "qwen3:8b"):
        window, source = catalog.resolve_context_window_with_source(
            local_id, provider="ollama"
        )
        assert window == _LOCAL_CONTEXT_WINDOW
        assert source == "default"


def test_profile_default_window_layer_resolves_as_config() -> None:
    """[llm_profiles.<id>].context_window_tokens activates as "config" source."""
    catalog = ModelCatalog()
    catalog.set_profile_default_windows({"bailian": 1_000_000})
    # Profile window applies to any model under that provider id.
    window, source = catalog.resolve_context_window_with_source(
        "any-unknown-model", provider="bailian"
    )
    assert window == 1_000_000
    assert source == "config"
    # Profile window outranks inference for the same model.
    catalog.set_profile_default_windows({"bailian": 500_000})
    window, source = catalog.resolve_context_window_with_source(
        "qwen3.8-flash", provider="bailian"
    )
    assert window == 500_000
    assert source == "config"
'''

target.write_text(src + addition, encoding="utf-8", newline="")
print("appended 3 tests")
