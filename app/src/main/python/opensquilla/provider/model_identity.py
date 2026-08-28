"""Canonical model-family identity helpers shared across runtime layers."""

# These endpoints require assistant ``reasoning_content`` to be replayed on
# subsequent requests. Keep aliases here so provider and agent policy agree.
DEEPSEEK_V4_MODEL_IDS = frozenset(
    {
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "deepseek-v4-pro",
        "deepseek-v4-pro-0813",
    }
)


def model_basename(model_id: str | None) -> str:
    """Return a normalized model id without an optional vendor prefix."""

    return (model_id or "").strip().lower().rsplit("/", 1)[-1]


def is_deepseek_v4_model_id(model_id: str | None) -> bool:
    """Whether *model_id* uses the DeepSeek V4 reasoning replay contract."""

    return model_basename(model_id) in DEEPSEEK_V4_MODEL_IDS
