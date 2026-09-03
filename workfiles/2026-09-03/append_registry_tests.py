import io

path = r"D:\AIstudio\Harness\OpenSquilla-QinLuza-Studio\tests\test_onboarding\test_llm_profiles.py"

block = '''

def test_llm_active_provider_with_base_url_registers_without_profile() -> None:
    """An [llm] block whose provider id has no [llm_profiles] entry still
    registers from its own base_url.

    Regression (bailian): the profile was deleted by a later config rewrite
    while [llm] kept provider="bailian" + base_url. Cold boots then lost the
    dynamic registration, so doctor reported provider.active.unknown and the
    ensemble fixed fallback refused the otherwise self-sufficient deployment.
    """
    from opensquilla.provider import registry as provider_registry

    snapshot = dict(provider_registry._PROVIDER_SPECS)
    try:
        provider_registry._PROVIDER_SPECS.pop("bailian", None)
        cfg = GatewayConfig(
            llm={
                "provider": "bailian",
                "model": "qwen3.8-flash",
                "base_url": "https://llm.example/compatible-mode/v1",
                "api_key": "sk-synthetic",
            }
        )
        assert "bailian" not in cfg.llm_profiles

        registered = provider_registry.register_profile_providers(cfg)
        assert registered == 1
        assert provider_registry.get_provider_spec("bailian").runtime_supported

        # Idempotent: a second sweep does not double-count.
        assert provider_registry.register_profile_providers(cfg) == 0
    finally:
        provider_registry._PROVIDER_SPECS.clear()
        provider_registry._PROVIDER_SPECS.update(snapshot)


def test_llm_active_provider_sweep_skips_known_and_urlless() -> None:
    """The sweep must not touch static providers or url-less [llm] blocks."""
    from opensquilla.provider import registry as provider_registry

    snapshot = dict(provider_registry._PROVIDER_SPECS)
    try:
        provider_registry._PROVIDER_SPECS.pop("ghostmodel", None)
        static_cfg = GatewayConfig(
            llm={"provider": "deepseek", "model": "deepseek-v4-flash"}
        )
        assert provider_registry.register_profile_providers(static_cfg) == 0

        # Known dynamic id without any base_url anywhere stays unregistered.
        urlless = GatewayConfig(llm={"provider": "ghostmodel", "model": "x"})
        assert provider_registry.register_profile_providers(urlless) == 0
        with pytest.raises(Exception):
            provider_registry.get_provider_spec("ghostmodel")
    finally:
        provider_registry._PROVIDER_SPECS.clear()
        provider_registry._PROVIDER_SPECS.update(snapshot)
'''

with io.open(path, "r", encoding="utf-8", newline="") as fh:
    src = fh.read()

if "test_llm_active_provider_with_base_url_registers_without_profile" in src:
    print("already appended")
else:
    if not src.endswith("\n"):
        src += "\n"
    src += block
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(src)
    print("appended 2 tests")
