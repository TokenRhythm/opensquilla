from opensquilla.cli.init_cmd import _default_model_for_provider


def test_init_uses_tokenrhythm_0813_model_default() -> None:
    assert _default_model_for_provider("tokenrhythm") == "deepseek-v4-pro-0813"


def test_init_uses_direct_deepseek_model_default() -> None:
    assert _default_model_for_provider("deepseek") == "deepseek-v4-flash"


def test_init_keeps_openrouter_model_default() -> None:
    assert _default_model_for_provider("openrouter") == "deepseek/deepseek-v4-pro"
