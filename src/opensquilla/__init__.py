"""OpenSquilla — multi-channel LLM gateway and agent runtime."""

from __future__ import annotations

__all__ = ["__version__"]

# Keep package initialization cheap for the Windows controlled-launch helper.
# Reloading the package invalidates the cached value, as eager resolution did.
globals().pop("__version__", None)


def __getattr__(name: str) -> str:
    if name != "__version__":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib.metadata import PackageNotFoundError, version

    try:
        resolved = version("opensquilla")
    except PackageNotFoundError:
        # Source checkouts without distribution metadata retain the sentinel.
        resolved = "0.0.0+unknown"
    globals()[name] = resolved
    return resolved


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
