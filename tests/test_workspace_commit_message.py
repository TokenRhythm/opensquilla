"""Tests for the workspace review panel's drafted commit messages.

Covers the response parser, target resolution, the provider-adapter call, and
the orchestrator's refusal paths. The orchestrator never writes to the
repository, so nothing here asserts a side effect on disk.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.gateway.config import (
    CommitMessageConfig,
    GatewayConfig,
    SquillaRouterConfig,
)
from opensquilla.provider.protocol import ProviderConnectionConfig
from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.workspace_commit_message import (
    CommitMessageDraft,
    WorkspaceCommitMessageError,
    build_staged_diff_context,
    call_commit_message_provider,
    draft_workspace_commit_message,
    parse_commit_message,
    resolve_commit_message_target,
)


class _FakeProvider:
    """Provider stub exposing the connection config the drafter reads."""

    def __init__(
        self,
        *,
        api_key: str = "KEY",
        model: str = "",
        base_url: str = "",
        provider_kind: str = "openrouter",
    ):
        self._conn = ProviderConnectionConfig(
            provider_kind=provider_kind,
            model=model,
            api_key=api_key,
            base_url=base_url or "https://openrouter.ai/api/v1",
        )

    def provider_connection_config(self) -> ProviderConnectionConfig:
        return self._conn


def _router(default_tier: str = "c1") -> SimpleNamespace:
    return SimpleNamespace(
        tiers={"c1": {"model": "deepseek/deepseek-v4-pro"}},
        default_tier=default_tier,
    )


# ── parse_commit_message ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "subject", "body"),
    [
        ("Add the retry budget", "Add the retry budget", ""),
        ('"Add the retry budget"', "Add the retry budget", ""),
        ("Add the retry budget.", "Add the retry budget", ""),
        (
            "Add the retry budget\n\nCap the attempts so a stalled host fails fast.",
            "Add the retry budget",
            "Cap the attempts so a stalled host fails fast.",
        ),
        (
            "Commit message: Add the retry budget\n\nWhy: a stalled host hung.",
            "Add the retry budget",
            "Why: a stalled host hung.",
        ),
        # A heading marker is a wrapper; an issue reference is an identifier.
        ("## Fix the login redirect", "Fix the login redirect", ""),
        ("#123 fix the login redirect", "#123 fix the login redirect", ""),
        ("fix(#123): login redirect", "fix(#123): login redirect", ""),
        ("Fix the login redirect ##", "Fix the login redirect", ""),
        (
            "```text\nAdd the retry budget\n\nBody line.\n```",
            "Add the retry budget",
            "Body line.",
        ),
        ("\n\n  Add the retry budget  \n\nBody.\n", "Add the retry budget", "Body."),
    ],
)
def test_parse_commit_message_splits_subject_and_body(raw, subject, body):
    assert parse_commit_message(raw, 2000) == CommitMessageDraft(
        subject=subject,
        body=body,
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "```",
        "commit message",
        "Commit Message",
        "Here is your commit message",
        "Subject",
    ],
)
def test_parse_commit_message_refuses_an_unusable_answer(raw):
    assert parse_commit_message(raw, 2000) is None


def test_parse_commit_message_trims_a_trailing_body_to_the_char_ceiling():
    draft = parse_commit_message("Add the retry budget\n\n" + "x" * 500, 40)

    assert draft is not None
    assert draft.subject == "Add the retry budget"
    assert len(draft.subject) + len(draft.body) <= 40


def test_parse_commit_message_caps_a_runaway_subject():
    draft = parse_commit_message("y" * 400, 5000)

    assert draft is not None
    assert len(draft.subject) == 100


# ── build_staged_diff_context ───────────────────────────────────────────────


def _patch(*files: tuple[str, str]) -> str:
    """A staged patch with one section per ``(path, body)`` pair."""

    parts = []
    for path, body in files:
        parts.append(
            f"diff --git a/{path} b/{path}\n"
            f"index 1111111..2222222 100644\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -1,1 +1,2 @@\n"
            f" context\n"
            f"{body}\n"
        )
    return "".join(parts)


def test_context_lists_every_file_even_when_the_patch_is_abbreviated():
    """The reported defect: 79 files changed, the model was told about one.

    A bare prefix of a wide patch filled the whole budget with the first files,
    so the drafted subject named a single file while seventy-nine had changed.
    The list is the cheap half and must therefore be complete, with only the
    patch excerpts bounded.
    """

    paths = [f"src/mod{index:02d}/file{index}.ts" for index in range(79)]
    context = build_staged_diff_context(
        _patch(*[(path, "+added line") for path in paths])
    )

    assert "Staged changes: 79 file(s)" in context
    missing = [path for path in paths if path not in context]
    assert missing == [], missing
    # Excerpts are bounded, and how many files they cover is stated in the
    # first line, which is the only line the budget fit cannot reach.
    assert "Patch excerpts:" in context
    assert "patch excerpts cover" in context.splitlines()[0]
    assert "patch excerpts cover all of them" not in context.splitlines()[0]


def test_context_says_when_the_list_itself_was_cut():
    """A cap on the list must not be reported as a complete list."""

    paths = [f"src/mod{index // 40:02d}/deep/nested/file{index}.ts" for index in range(400)]
    context = build_staged_diff_context(
        _patch(*[(path, "+added line") for path in paths])
    )

    assert "Staged changes: 400 file(s)" in context
    assert "file list below is truncated" in context
    assert "more file(s)" in context
    assert "file list below is complete" not in context


def test_context_reads_the_status_from_the_header_not_from_the_hunk():
    """A hunk may spell Git's own markers; the status is still 'M'.

    Scanning the whole section matched hunk content, so a repository that
    documents Git output — this project's own tests, for one — reported a
    modified file as added, deleted, renamed or binary, and the prompt tells
    the model to trust that list.
    """

    patch = (
        "diff --git a/src/hint.ts b/src/hint.ts\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/hint.ts\n"
        "+++ b/src/hint.ts\n"
        "@@ -1,1 +1,2 @@\n"
        " // what Git prints for a new file\n"
        "+new file mode 100644\n"
    )

    context = build_staged_diff_context(patch)

    assert "M  src/hint.ts" in context
    assert "A  src/hint.ts" not in context

    for marker, letter in (
        ("deleted file mode 100644", "D"),
        ("rename from old.ts", "R"),
        ("copy from old.ts", "C"),
    ):
        content = patch.replace("new file mode 100644", marker)
        assert "M  src/hint.ts" in build_staged_diff_context(content), marker
        assert f"{letter}  src/hint.ts" not in build_staged_diff_context(content), marker

    binary_content = patch.replace(
        "new file mode 100644", "Binary files a/x and b/x differ"
    )
    assert "binary" not in build_staged_diff_context(binary_content)


def test_context_admits_a_patch_the_transport_already_cut():
    """A patch cut at the transport bound cannot claim a complete file list.

    The reader computes `truncated`; without it the header asserted
    completeness for a patch whose later sections were never read, which is the
    exact failure the complete-file-list design exists to prevent.
    """

    patch = _patch(("src/a.ts", "+one"), ("src/b.ts", "+two"))

    intact = build_staged_diff_context(patch)
    assert "file list below is complete" in intact.splitlines()[0]

    cut = build_staged_diff_context(patch, truncated=True)
    header = cut.splitlines()[0]
    assert "file list below is incomplete" in header
    assert "file list below is complete" not in header


def test_context_counts_each_file_and_keeps_the_status_letters():
    patch = (
        "diff --git a/keep.ts b/keep.ts\nindex 1..2 100644\n--- a/keep.ts\n"
        "+++ b/keep.ts\n@@ -1 +1,2 @@\n context\n+added\n-removed\n"
        "diff --git a/new.ts b/new.ts\nnew file mode 100644\nindex 1..2\n"
        "--- /dev/null\n+++ b/new.ts\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        "diff --git a/gone.ts b/gone.ts\ndeleted file mode 100644\nindex 1..2\n"
        "--- a/gone.ts\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
        "diff --git a/old.ts b/renamed.ts\nsimilarity index 90%\nrename from old.ts\n"
        "rename to renamed.ts\n"
    )

    context = build_staged_diff_context(patch)

    assert "M  keep.ts  (+1 -1)" in context
    assert "A  new.ts  (+2 -0)" in context
    assert "D  gone.ts  (+0 -1)" in context
    assert "R  old.ts -> renamed.ts" in context


def test_context_states_a_binary_file_instead_of_counting_it():
    patch = (
        "diff --git a/logo.png b/logo.png\nindex 1..2 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )

    assert "binary" in build_staged_diff_context(patch)


def test_context_passes_a_patch_through_when_it_has_no_sections():
    assert build_staged_diff_context("not a patch") == "not a patch"
    assert build_staged_diff_context("") == ""


def test_context_is_what_a_bounded_request_keeps():
    """Truncation must cost detail, never the scope of the change."""

    from dataclasses import replace

    from opensquilla.provider.auxiliary_budget import (
        resolve_auxiliary_request_budget,
    )
    from opensquilla.workspace_commit_message import _fit_diff_content

    paths = [f"src/mod{index:02d}/file{index}.ts" for index in range(40)]
    patch = build_staged_diff_context(
        _patch(*[(path, "+added line") for path in paths])
    )
    # The real budget, tightened so the fit actually has to truncate.
    budget = replace(
        resolve_auxiliary_request_budget(
            None,
            provider_id="openai_compat",
            model="test-model",
            max_output_tokens=512,
        ),
        provider_request_max_chars=6_000,
    )

    fitted = _fit_diff_content(patch, system_prompt="system", budget=budget)

    assert fitted is not None
    assert len(fitted) <= 6_000
    assert len(fitted) < len(patch), "the case is only interesting when it truncates"
    # The summary and the file list come first, so they survive a short budget.
    assert "Staged changes: 40 file(s)" in fitted
    assert "src/mod39/file39.ts" in fitted
    assert "patch excerpts cover" in fitted.splitlines()[0]


# ── resolve_commit_message_target ───────────────────────────────────────────


def test_resolve_target_falls_back_to_the_connection_model():
    cfg = SimpleNamespace(tier=None, model=None, timeout_seconds=30.0)

    target = resolve_commit_message_target(
        cfg, _router(), _FakeProvider(model="relay:model"), None
    )

    assert target is not None
    assert target.model == "relay:model"
    assert target.api_key == "KEY"


def test_resolve_target_prefers_an_explicit_model():
    cfg = SimpleNamespace(tier=None, model="explicit/model", timeout_seconds=5.0)

    target = resolve_commit_message_target(cfg, _router(), _FakeProvider(), None)

    assert target is not None
    assert target.model == "explicit/model"
    assert target.timeout == 5.0


def _routed_config(**commit_message: object) -> GatewayConfig:
    """Router mode whose default tier names a *different* model on the same
    provider — the shape that made the draft send an id the relay rejected."""

    return GatewayConfig(
        squilla_router=SquillaRouterConfig(
            enabled=True,
            default_tier="c1",
            tiers={"c1": {"provider": "openai", "model": "gpt-5.4-mini"}},
        ),
        commit_message=CommitMessageConfig(**commit_message),
    )


def test_resolve_target_prefers_the_connected_model_over_the_router_default_tier():
    """The draft sends the model the operator connected, not a catalog id.

    A tier table is spelled in its own provider's catalogue, and a relay or
    proxy in front of that provider need not serve those ids at all: preferring
    the tier sent `gpt-5.4-mini` to a relay that only answers
    `providerId:apiModelId` and got a 400 for a model the operator had already
    verified.
    """

    cfg = SimpleNamespace(tier=None, model=None, timeout_seconds=30.0)
    router = _routed_config().squilla_router

    target = resolve_commit_message_target(
        cfg, router, _FakeProvider(model="relay:glm-5.3"), None
    )

    assert target is not None
    assert target.model == "relay:glm-5.3"


def test_resolve_target_keeps_an_explicit_router_tier():
    cfg = SimpleNamespace(tier="c1", model=None, timeout_seconds=30.0)
    router = _routed_config().squilla_router

    target = resolve_commit_message_target(
        cfg,
        router,
        _FakeProvider(provider_kind="openai", model="relay:glm-5.3"),
        None,
    )

    assert target is not None
    assert target.model == "gpt-5.4-mini"


def test_resolve_target_requires_credentials():
    cfg = SimpleNamespace(tier=None, model=None, timeout_seconds=30.0)

    assert resolve_commit_message_target(
        cfg, _router(), _FakeProvider(api_key=""), None
    ) is None


# ── call_commit_message_provider (adapter transport) ────────────────────────


class _ProviderStream:
    """Async iterator of provider stream events, recording close."""

    def __init__(self, events):
        self._events = iter(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self):
        self.closed = True


class _AdapterProvider:
    """Adapter-shaped provider stub: metadata, connection config, and chat()."""

    provider_name = "openai"

    def __init__(
        self,
        stream_factory,
        *,
        accounts_physical_usage=False,
        model="provider/model",
    ):
        self._stream_factory = stream_factory
        self._model = model
        self.calls = []
        self.streams = []
        self.accounts_physical_usage = accounts_physical_usage

    def provider_metadata(self):
        from opensquilla.provider.protocol import ProviderMetadata

        return ProviderMetadata(
            provider_name="openai",
            provider_kind="openrouter",
            provider_id="openrouter",
            model=self._model,
            base_url="https://openrouter.ai/api/v1",
        )

    def provider_connection_config(self):
        return ProviderConnectionConfig(
            provider_kind="openrouter",
            model=self._model,
            api_key="KEY",
            base_url="https://openrouter.ai/api/v1",
        )

    def chat(self, messages, tools=None, config=None):
        self.calls.append((messages, tools, config))
        stream = self._stream_factory()
        self.streams.append(stream)
        return stream

    async def list_models(self):
        return []


def _delta(text):
    return TextDeltaEvent(text=text)


def _done(output_tokens=5, stop_reason="end_turn"):
    return DoneEvent(output_tokens=output_tokens, stop_reason=stop_reason)


def _drafting_provider(events, **kwargs):
    return _AdapterProvider(lambda: _ProviderStream(list(events)), **kwargs)


@pytest.mark.asyncio
async def test_call_provider_builds_a_message_shaped_request():
    provider = _drafting_provider([
        _delta("Add the retry budget"),
        _delta("\n\nCap the attempts so a stalled host fails fast."),
        _done(),
    ])

    draft = await call_commit_message_provider(
        "diff --git a/a.py b/a.py\n+retries = 3\n",
        provider=provider,
        model="provider/model",
        timeout=10.0,
    )

    assert draft == CommitMessageDraft(
        subject="Add the retry budget",
        body="Cap the attempts so a stalled host fails fast.",
    )
    messages, tools, config = provider.calls[0]
    assert tools is None
    # The budget has to cover a reasoning model's thinking before the message:
    # a 1024-token cap spent itself on deliberation and returned null content.
    assert config.max_tokens == 4096
    assert config.temperature == 0
    assert config.thinking is False
    assert config.timeout == 10.0
    # The patch is the user turn; the rules are the system turn.
    assert messages[0].role == "user"
    assert "+retries = 3" in messages[0].content
    assert "You write Git commit messages" in config.system
    # The stream is closed even though it reached its own terminal event.
    assert provider.streams[0].closed is True


@pytest.mark.asyncio
async def test_call_provider_carries_the_configured_rule():
    provider = _drafting_provider([_delta("Subject"), _done()])

    await call_commit_message_provider(
        "+line\n",
        provider=provider,
        model="provider/model",
        instructions="Use Conventional Commits prefixes.",
    )

    system = provider.calls[0][2].system
    assert "Use Conventional Commits prefixes." in system
    # The built-in guidance survives, so a rule adds to it rather than
    # replacing the guarantees (truthfulness, no invented change).
    assert "never invent a change" in system
    assert "untrusted content" in system


@pytest.mark.asyncio
async def test_call_provider_reports_an_answer_cut_by_the_token_limit():
    """A reasoning model that never got to the message is not a silent nothing."""

    provider = _drafting_provider([_done(output_tokens=0, stop_reason="length")])

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await call_commit_message_provider(
            "+line\n", provider=provider, model="provider/model"
        )

    assert raised.value.reason == "answer_truncated"
    assert "output limit" in raised.value.message


@pytest.mark.asyncio
async def test_call_provider_keeps_a_usable_answer_that_hit_the_limit():
    """A subject that arrived before the cap is still a usable draft."""

    provider = _drafting_provider([
        _delta("Add the retry budget"),
        _done(stop_reason="length"),
    ])

    draft = await call_commit_message_provider(
        "+line\n", provider=provider, model="provider/model"
    )

    assert draft == CommitMessageDraft(subject="Add the retry budget", body="")


@pytest.mark.asyncio
async def test_call_provider_refuses_without_a_provider_or_a_patch():
    provider = _drafting_provider([_delta("Subject"), _done()])

    assert await call_commit_message_provider(
        "+line\n", provider=None, model="provider/model"
    ) is None
    assert await call_commit_message_provider(
        "   ", provider=provider, model="provider/model"
    ) is None
    assert provider.calls == []


@pytest.mark.asyncio
async def test_call_provider_returns_none_for_an_unusable_answer():
    """An answer with no usable subject is "no message", not a failure."""

    provider = _drafting_provider([_delta("commit message"), _done()])

    assert await call_commit_message_provider(
        "+line\n", provider=provider, model="provider/model"
    ) is None


@pytest.mark.asyncio
async def test_call_provider_reports_a_failed_call_with_its_cause():
    """A transport failure names what failed; `None` means an unusable answer."""

    provider = _drafting_provider([
        ErrorEvent(message="401 Unauthorized: invalid_api_key", code="401"),
    ])

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await call_commit_message_provider(
            "+line\n", provider=provider, model="provider/model"
        )

    assert raised.value.reason == "call_failed"
    assert "401 Unauthorized" in raised.value.message


@pytest.mark.asyncio
async def test_call_provider_reports_a_stream_without_a_terminal_event():
    provider = _drafting_provider([_delta("Partial")])

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await call_commit_message_provider(
            "+line\n", provider=provider, model="provider/model"
        )

    assert raised.value.reason == "call_failed"
    assert "terminal" in raised.value.message


@pytest.mark.asyncio
async def test_call_provider_reports_a_timeout_with_a_readable_cause():
    async def _never():
        yield _delta("stuck")
        await asyncio.Event().wait()

    provider = _AdapterProvider(lambda: _never())

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await call_commit_message_provider(
            "+line\n", provider=provider, model="provider/model", timeout=0.01
        )

    assert raised.value.reason == "call_failed"
    assert "0.01 seconds" in raised.value.message


# ── draft_workspace_commit_message ──────────────────────────────────────────


def _ctx(config: GatewayConfig) -> SimpleNamespace:
    return SimpleNamespace(config=config, provider_selector=None, usage_event_sink=None)


@pytest.mark.asyncio
async def test_draft_refuses_when_the_setting_is_disabled():
    ctx = _ctx(GatewayConfig(commit_message=CommitMessageConfig(enabled=False)))

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await draft_workspace_commit_message(ctx, "+line\n")

    assert raised.value.reason == "disabled"


@pytest.mark.asyncio
async def test_draft_refuses_without_a_resolvable_target():
    ctx = _ctx(GatewayConfig())

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await draft_workspace_commit_message(ctx, "+line\n")

    assert raised.value.reason == "no_target"


@pytest.mark.asyncio
async def test_draft_sends_the_connected_model_rather_than_the_router_tier(monkeypatch):
    """Regression: router mode must not override the model that was verified."""

    ctx = _ctx(_routed_config())
    monkeypatch.setattr(
        "opensquilla.gateway.compaction_target.resolve_selected_compaction_provider",
        lambda *_args, **_kwargs: _FakeProvider(model="relay:glm-5.3"),
    )
    seen: dict = {}

    async def _capture(_diff, **kwargs):
        seen.update(kwargs)
        return CommitMessageDraft(subject="Subject", body="")

    monkeypatch.setattr(
        "opensquilla.workspace_commit_message.call_commit_message_provider",
        _capture,
    )

    await draft_workspace_commit_message(ctx, "+line\n")

    assert seen["model"] == "relay:glm-5.3"
    # The connected deployment already serves that model, so nothing rebuilds.
    assert seen["provider"].provider_connection_config().model == "relay:glm-5.3"


@pytest.mark.asyncio
async def test_draft_rebuilds_a_clone_for_an_explicit_model(monkeypatch):
    """An explicit model reaches the adapter without rebinding the session's.

    The clone is what makes ``commit_message.model`` work at all once the call
    goes through the provider adapter: the selector's current deployment serves
    its own model, and only a clone can carry a different one.
    """

    ctx = _ctx(GatewayConfig(commit_message=CommitMessageConfig(model="explicit/model")))
    overrides: list[object] = []

    def _resolver(_ctx, _session, *, model_override=None):
        overrides.append(model_override)
        return _FakeProvider(model=model_override or "connected/model")

    monkeypatch.setattr(
        "opensquilla.gateway.compaction_target.resolve_selected_compaction_provider",
        _resolver,
    )
    seen: dict = {}

    async def _capture(_diff, **kwargs):
        seen.update(kwargs)
        return CommitMessageDraft(subject="Subject", body="")

    monkeypatch.setattr(
        "opensquilla.workspace_commit_message.call_commit_message_provider",
        _capture,
    )

    await draft_workspace_commit_message(ctx, "+line\n")

    assert overrides == [None, "explicit/model"]
    assert seen["model"] == "explicit/model"


@pytest.mark.asyncio
async def test_draft_refuses_when_the_explicit_model_is_not_on_the_connection(monkeypatch):
    """A clone that still cannot serve the model is a missing target."""

    ctx = _ctx(GatewayConfig(commit_message=CommitMessageConfig(model="explicit/model")))
    monkeypatch.setattr(
        "opensquilla.gateway.compaction_target.resolve_selected_compaction_provider",
        lambda *_args, **_kwargs: _FakeProvider(model="connected/model"),
    )

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await draft_workspace_commit_message(ctx, "+line\n")

    assert raised.value.reason == "no_target"


@pytest.mark.asyncio
async def test_draft_reports_an_unusable_model_answer(monkeypatch):
    # An explicit model keeps this test about the model's answer rather than
    # about which tier the default routing profile happens to name.
    ctx = _ctx(GatewayConfig(commit_message=CommitMessageConfig(model="test/model")))

    monkeypatch.setattr(
        "opensquilla.gateway.compaction_target.resolve_selected_compaction_provider",
        lambda *_args, **_kwargs: _FakeProvider(model="test/model"),
    )

    async def _no_message(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "opensquilla.workspace_commit_message.call_commit_message_provider",
        _no_message,
    )

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await draft_workspace_commit_message(ctx, "+line\n")

    assert raised.value.reason == "no_message"


@pytest.mark.asyncio
async def test_draft_reports_a_failed_call_rather_than_a_silent_no_message(monkeypatch):
    ctx = _ctx(GatewayConfig(commit_message=CommitMessageConfig(model="test/model")))
    monkeypatch.setattr(
        "opensquilla.gateway.compaction_target.resolve_selected_compaction_provider",
        lambda *_args, **_kwargs: _FakeProvider(model="test/model"),
    )

    async def _fail(*_args, **_kwargs):
        raise WorkspaceCommitMessageError("call_failed", "The model call failed: 401")

    monkeypatch.setattr(
        "opensquilla.workspace_commit_message.call_commit_message_provider",
        _fail,
    )

    with pytest.raises(WorkspaceCommitMessageError) as raised:
        await draft_workspace_commit_message(ctx, "+line\n")

    # The reason the call failed survives to the caller instead of being
    # flattened into "the model returned nothing useful".
    assert raised.value.reason == "call_failed"
    assert "401" in raised.value.message


@pytest.mark.asyncio
async def test_draft_sends_the_configured_rule_and_nothing_else(monkeypatch):
    """The application setting is the only source of a rule.

    A per-call override existed here and had no caller; it is gone from the
    contract, the handler and this function, so an unset setting is what leaves
    the built-in guidance on its own.
    """

    monkeypatch.setattr(
        "opensquilla.gateway.compaction_target.resolve_selected_compaction_provider",
        lambda *_args, **_kwargs: _FakeProvider(model="test/model"),
    )
    seen: dict = {}

    async def _capture(_diff, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return CommitMessageDraft(subject="Subject", body="")

    monkeypatch.setattr(
        "opensquilla.workspace_commit_message.call_commit_message_provider",
        _capture,
    )

    configured = _ctx(
        GatewayConfig(
            commit_message=CommitMessageConfig(
                model="test/model",
                instructions="Configured rule.",
            ),
        )
    )
    draft = await draft_workspace_commit_message(configured, "+line\n")
    assert draft.subject == "Subject"
    assert seen["instructions"] == "Configured rule."

    unset = _ctx(GatewayConfig(commit_message=CommitMessageConfig(model="test/model")))
    await draft_workspace_commit_message(unset, "+line\n")
    assert seen["instructions"] is None
