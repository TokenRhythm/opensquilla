"""Process wait retains its ordinary default and explicit timeout bounds."""
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext


def _with_ctx(ctx):
    return shell.current_tool_context.set(ctx)


def test_default_wait_timeout():
    tok = _with_ctx(ToolContext(caller_kind=CallerKind.AGENT))
    try:
        assert shell._resolve_process_wait_timeout(None) == 600.0
    finally:
        shell.current_tool_context.reset(tok)




def test_explicit_timeout_honored_and_clamped():
    tok = _with_ctx(ToolContext(caller_kind=CallerKind.AGENT))
    try:
        assert shell._resolve_process_wait_timeout(120) == 120.0
        assert shell._resolve_process_wait_timeout(99999) == 5400.0  # clamp to max
    finally:
        shell.current_tool_context.reset(tok)
