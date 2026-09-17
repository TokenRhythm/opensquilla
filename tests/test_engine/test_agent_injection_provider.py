from opensquilla.engine.agent_injection import (
    ListPendingInputProvider,
    PendingInputProvider,
)


def test_append_then_drain_returns_pending_inputs_in_order() -> None:
    provider = ListPendingInputProvider()

    provider.append("first")
    provider.append("second")

    assert provider.peek_pending() == ["first", "second"]
    assert provider.drain_pending() == ["first", "second"]
    assert provider.peek_pending() == []
    assert provider.drain_pending() == []
    provider.mark_applied(iteration=2, model_call_id="2.0")
    assert provider.applications[0].texts == ("first", "second")


def test_append_ignores_empty_or_whitespace_text() -> None:
    provider = ListPendingInputProvider()

    provider.append("")
    provider.append("   \n\t")
    provider.append("keep")

    assert provider.drain_pending() == ["keep"]


def test_len_tracks_pending_inputs_until_drain() -> None:
    provider = ListPendingInputProvider()

    provider.append("first")
    provider.append("second")

    assert len(provider) == 2
    provider.drain_pending()
    assert len(provider) == 2
    provider.mark_applied(iteration=2, model_call_id="2.0")
    assert len(provider) == 0


def test_drain_claims_only_one_batch_until_it_is_applied() -> None:
    provider = ListPendingInputProvider()
    provider.append("first")

    drained = provider.drain_pending()
    provider.append("second")

    assert drained == ["first"]
    assert provider.drain_pending() == []
    provider.mark_applied(iteration=2, model_call_id="2.0")
    assert provider.drain_pending() == ["second"]
    assert drained == ["first"]


def test_reclaim_pending_includes_claimed_and_not_yet_claimed_inputs() -> None:
    provider = ListPendingInputProvider()
    provider.append("first")
    assert provider.drain_pending() == ["first"]
    provider.append("second")

    assert provider.reclaim_pending() == ["first", "second"]
    assert len(provider) == 0
    assert provider.applications == ()


def test_list_pending_input_provider_satisfies_protocol_at_runtime() -> None:
    assert isinstance(ListPendingInputProvider(), PendingInputProvider)
