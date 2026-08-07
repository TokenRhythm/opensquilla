"""Usage-scope binder restores a clean ContextVar after exit (#1064)."""

from __future__ import annotations

from unittest.mock import MagicMock

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
    current_usage_accounting_scope,
)


def test_bind_usage_accounting_scope_clears_on_exit() -> None:
    scope = UsageAccountingScope(
        sink=MagicMock(),
        context=UsageExecutionContext(execution_id="s", agent_run_id="run"),
    )
    assert current_usage_accounting_scope() is None
    with bind_usage_accounting_scope(scope):
        assert current_usage_accounting_scope() is scope
    assert current_usage_accounting_scope() is None
