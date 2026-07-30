"""RPC tests for channel status payloads."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import opensquilla.gateway.rpc_channels  # noqa: F401  ensures registration
from opensquilla.channels.contract import (
    ChannelCapabilities,
    ChannelCapabilityProfile,
    ChannelPlatformCapabilityStatus,
    ChannelPlatformCategories,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_channels import _status_for
from opensquilla.onboarding.mutations import upsert_channel


def _read_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
        ),
    )


def _admin_ctx() -> RpcContext:
    return RpcContext(
        conn_id="t-admin",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )


@pytest.mark.parametrize(
    ("connected", "enabled", "dispatch_state", "connection_phase", "expected"),
    [
        (False, False, None, None, "disabled"),
        (False, True, "dead", "reconnecting", "dead"),
        (False, True, "exhausted", "reconnecting", "exhausted"),
        (False, True, "restarting", "open", "restarting"),
        (True, True, None, "open", "connected"),
        (False, True, None, "stopped", "stopped"),
        (False, True, None, "connecting", "restarting"),
        (False, True, None, "reconnecting", "restarting"),
    ],
)
def test_channel_status_preserves_wire_vocabulary_and_dispatch_priority(
    connected: bool,
    enabled: bool,
    dispatch_state: str | None,
    connection_phase: str | None,
    expected: str,
) -> None:
    assert _status_for(
        connected=connected,
        enabled=enabled,
        dispatch_state=dispatch_state,
        connection_phase=connection_phase,
    ) == expected


@pytest.mark.asyncio
async def test_channels_status_surfaces_reconnecting_transport_without_new_wire_status():
    class FakeHealth:
        connected = False
        bot_user_id = None
        extra = {
            "connection_phase": "reconnecting",
            "last_error": {
                "error_class": "transport_unavailable",
                "message": "Feishu websocket is reconnecting",
                "retryable": True,
            },
        }

    class FakeManager:
        _channel_types = {"feishu-main": "feishu"}

        async def health(self):
            return {"feishu-main": FakeHealth()}

        def get(self, name: str):
            assert name == "feishu-main"
            return None

    ctx = _read_ctx()
    ctx.channel_manager = FakeManager()

    rpc_res = await get_dispatcher().dispatch("r-phase", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    row = rpc_res.payload["channels"][0]
    assert row["connected"] is False
    assert row["status"] == "restarting"
    assert row["diagnostics"]["connection_phase"] == "reconnecting"
    assert row["diagnostics"]["last_error"] == {
        "error_class": "transport_unavailable",
        "message": "Feishu websocket is reconnecting",
        "retryable": True,
        "source": "adapter",
    }


@pytest.mark.asyncio
async def test_channels_status_includes_configured_channels_without_manager():
    ctx = _read_ctx()
    res = upsert_channel(
        GatewayConfig(),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "xoxb-secret",
            "signing_secret": "ss",
        },
    )
    ctx.config = res.config

    rpc_res = await get_dispatcher().dispatch("r1", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["channels"] == [
        {
            "name": "work",
            "connected": False,
            "status": "stopped",
            "bot_user_id": None,
            "connected_since": None,
            "restart_attempts": 0,
            "pendingPairings": 0,
            "type": "slack",
            "enabled": True,
            "configured": True,
            "capabilities": [],
            "capability_profile": None,
            "platform_manifest": None,
            "diagnostics": {"network_probe": "not_run"},
        }
    ]


@pytest.mark.asyncio
async def test_channels_status_reports_adapter_capabilities_without_network_probe():
    class FakeHealth:
        connected = True
        bot_user_id = "bot-1"
        extra = {"connected_since": "now", "restart_attempts": 2}

    class FakeAdapter:
        capability_profile = ChannelCapabilityProfile(
            channel_type="discord",
            group_chat=True,
            native_file_upload=True,
            inbound_reactions=True,
            thread_messages=True,
            group_dm=True,
            transports=("websocket",),
        )

    class FakeManager:
        _channel_types = {"discord": "discord"}

        async def health(self):
            return {"discord": FakeHealth()}

        def get(self, name: str):
            assert name == "discord"
            return FakeAdapter()

    ctx = _read_ctx()
    ctx.channel_manager = FakeManager()

    rpc_res = await get_dispatcher().dispatch("r1", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload is not None
    row = rpc_res.payload["channels"][0]
    assert row["name"] == "discord"
    assert row["status"] == "connected"
    assert set(row["capabilities"]) >= {
        ChannelCapabilities.GROUP_CHAT,
        ChannelCapabilities.GROUP_DM,
        ChannelCapabilities.INBOUND_REACTIONS,
        ChannelCapabilities.NATIVE_FILE_UPLOAD,
        ChannelCapabilities.THREAD_MESSAGES,
        ChannelCapabilities.WEBSOCKET,
    }
    assert row["capability_profile"]["channel_type"] == "discord"
    assert row["capability_profile"]["transports"] == ["websocket"]
    assert row["capability_profile"]["maturity"] == "unrated"
    assert (
        row["capability_profile"]["evidence"][ChannelCapabilities.NATIVE_FILE_UPLOAD][
            "implemented"
        ]
        is False
    )
    assert row["platform_manifest"]["channel_type"] == "discord"
    assert row["platform_manifest"]["capabilities"][ChannelPlatformCategories.CHAT][
        "status"
    ] == ChannelPlatformCapabilityStatus.SUPPORTED
    assert row["platform_manifest"]["capabilities"][ChannelPlatformCategories.FILES][
        "status"
    ] == ChannelPlatformCapabilityStatus.CONFIG_REQUIRED
    assert row["platform_manifest"]["capabilities"][ChannelPlatformCategories.DOCS][
        "status"
    ] == ChannelPlatformCapabilityStatus.UNSUPPORTED
    assert row["diagnostics"] == {
        "network_probe": "not_run",
        # A running adapter without an explicit policy is governed by the
        # default access policy; explain-why must show that, not hide it.
        "admission": {
            "dmAccess": "pairing",
            "allowlist": {"configured": False, "entryCount": 0, "blankEntryCount": 0},
        },
    }


@pytest.mark.asyncio
async def test_channels_status_explains_admission_policy_and_denials():
    from opensquilla.channels._util import ChannelAccessPolicy, ChannelDmAccess

    class FakeHealth:
        connected = True
        bot_user_id = "bot-1"
        extra: dict = {}

    class FakeAdapter:
        policy = ChannelAccessPolicy(
            dm_access=ChannelDmAccess.ALLOWLIST,
            allowlist=frozenset({"user-ok", "  "}),
        )

    class FakeStore:
        def admission_reason_counts(self, name: str) -> dict:
            assert name == "telegram-main"
            return {
                "dm_admitted": {"count": 9, "first_at": 1700000000.0, "last_at": 1700000300.0},
                "not_in_allowlist": {"count": 4, "first_at": 1699999000.0, "last_at": 1700000100.0},
                "pairing_required": {"count": 2, "first_at": 1700000150.0, "last_at": 1700000200.0},
            }

    class FakeManager:
        _channel_types = {"telegram-main": "telegram"}
        _delivery_store = FakeStore()

        async def health(self):
            return {"telegram-main": FakeHealth()}

        def get(self, name: str):
            assert name == "telegram-main"
            return FakeAdapter()

    ctx = _read_ctx()
    ctx.channel_manager = FakeManager()

    rpc_res = await get_dispatcher().dispatch("r1", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    admission = rpc_res.payload["channels"][0]["diagnostics"]["admission"]
    assert admission["dmAccess"] == "allowlist"
    # entryCount + blankEntryCount is the honest typo detector: entries are
    # opaque strings, so "invalid" can only mean blank/whitespace.
    assert admission["allowlist"] == {
        "configured": True,
        "entryCount": 2,
        "blankEntryCount": 1,
    }
    assert admission["reasons"]["not_in_allowlist"]["count"] == 4
    assert admission["reasons"]["dm_admitted"]["count"] == 9
    assert admission["reasons"]["pairing_required"]["lastAt"].startswith("2023-11-1")
    # Tallies are lifetime; the horizon label keeps months-old denials under a
    # since-changed policy from reading as a live condition.
    assert admission["since"].startswith("2023-11-1")
    # The newest DENIAL wins, not the newer admitted decision.
    assert admission["lastDenial"]["reason"] == "pairing_required"
    # Reason codes and counts only — no sender identity anywhere in the block.
    assert "user-ok" not in str(admission)


def test_admit_reason_set_tracks_the_admission_vocabulary():
    # _ADMISSION_ADMIT_REASONS decides what counts as a denial for lastDenial;
    # a new admit-outcome reason added to AdmissionReason but not here would be
    # reported as the channel's most recent denial.
    from typing import get_args

    from opensquilla.channels.admission import AdmissionReason
    from opensquilla.gateway.rpc_channels import _ADMISSION_ADMIT_REASONS

    vocabulary = set(get_args(AdmissionReason))
    assert _ADMISSION_ADMIT_REASONS <= vocabulary
    assert _ADMISSION_ADMIT_REASONS == {r for r in vocabulary if r.endswith("_admitted")}


@pytest.mark.asyncio
async def test_channels_status_admission_block_absent_without_adapter_or_history():
    # No running adapter and no recorded decisions: nothing to explain, so the
    # key stays absent instead of implying a policy that is not in force.
    ctx = _read_ctx()
    res = upsert_channel(
        GatewayConfig(),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "xoxb-secret",
            "signing_secret": "ss",
        },
    )
    ctx.config = res.config

    rpc_res = await get_dispatcher().dispatch("r1", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    assert "admission" not in rpc_res.payload["channels"][0]["diagnostics"]


@pytest.mark.asyncio
async def test_channels_status_merges_start_error_diagnostics_for_configured_channel():
    ctx = _read_ctx()
    res = upsert_channel(
        GatewayConfig(),
        entry_payload={
            "type": "dingtalk",
            "name": "dingtalk",
            "client_id": "app-key",
            "client_secret": "app-secret",
        },
    )
    ctx.config = res.config

    class FakeManager:
        _channel_types = {"dingtalk": "dingtalk"}

        async def health(self):
            return {}

        def get(self, name: str):
            assert name == "dingtalk"
            return None

        def start_errors(self):
            return {
                "dingtalk": {
                    "error_type": "DingTalkAuthError",
                    "error": "DingTalk credentials were rejected",
                    "diagnostic": {
                        "error_class": "auth_invalid",
                        "provider_code": "authFailed",
                        "message": "凭证无效：检查 DingTalk AppKey/AppSecret",
                        "retryable": False,
                    },
                }
            }

    ctx.channel_manager = FakeManager()

    rpc_res = await get_dispatcher().dispatch("r1", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    row = rpc_res.payload["channels"][0]
    assert row["name"] == "dingtalk"
    assert row["status"] == "stopped"
    assert row["connected"] is False
    assert row["diagnostics"]["last_error"] == {
        "error_class": "auth_invalid",
        "provider_code": "authFailed",
        "message": "凭证无效：检查 DingTalk AppKey/AppSecret",
        "retryable": False,
        "source": "start_error",
    }


@pytest.mark.asyncio
async def test_channels_get_redacts_configured_secrets() -> None:
    token = "xoxb-get-secret"
    signing_secret = "signing-get-secret"
    ctx = _admin_ctx()
    result = upsert_channel(
        GatewayConfig(),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": token,
            "signing_secret": signing_secret,
        },
    )
    ctx.config = result.config

    rpc_res = await get_dispatcher().dispatch(
        "r-get",
        "channels.get",
        {"name": "work"},
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["entry"]["name"] == "work"
    assert rpc_res.payload["entry"]["token"] == "***"
    assert rpc_res.payload["entry"]["signing_secret"] == "***"
    assert set(rpc_res.payload["secretFields"]) == {"token", "signing_secret"}
    assert token not in repr(rpc_res.payload)
    assert signing_secret not in repr(rpc_res.payload)


@pytest.mark.asyncio
async def test_channels_probe_merges_secrets_and_runs_real_slack_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.channels import registry as channel_registry

    token = "xoxb-stored-probe-secret"
    signing_secret = "stored-signing-secret"
    ctx = _admin_ctx()
    result = upsert_channel(
        GatewayConfig(),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": token,
            "signing_secret": signing_secret,
        },
    )
    ctx.config = result.config

    def handle_auth_test(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth.test"
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(
            200,
            json={"ok": True, "user_id": "U1", "team_id": "T1"},
        )

    client = httpx.AsyncClient(
        base_url="https://slack.test/api",
        headers={"Authorization": f"Bearer {token}"},
        transport=httpx.MockTransport(handle_auth_test),
    )
    real_build = channel_registry.build_managed_channel
    built_adapters: list[object] = []

    def build_with_mock_transport(entry):
        assert entry.token == token
        assert entry.signing_secret == signing_secret
        adapter = real_build(entry)
        assert adapter is not None
        adapter._client = client
        built_adapters.append(adapter)
        return adapter

    monkeypatch.setattr(channel_registry, "build_managed_channel", build_with_mock_transport)

    rpc_res = await get_dispatcher().dispatch(
        "r-probe",
        "channels.probe",
        {
            "entry": {
                "type": "slack",
                "name": "work",
                "token": "",
                "signing_secret": "",
            }
        },
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["status"] == "verified"
    assert rpc_res.payload["connected"] is True
    assert isinstance(rpc_res.payload["latencyMs"], int)
    assert rpc_res.payload["result"] == {
        "authenticated": True,
        "bot_user_id": "U1",
        "team_id": "T1",
    }
    assert token not in repr(rpc_res.payload)
    assert signing_secret not in repr(rpc_res.payload)
    assert len(built_adapters) == 1
    assert client.is_closed is True
    assert built_adapters[0]._client is None


@pytest.mark.asyncio
async def test_channels_probe_reports_unsupported_and_stops_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.channels import registry as channel_registry

    class UnsupportedAdapter:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    adapter = UnsupportedAdapter()
    monkeypatch.setattr(
        channel_registry,
        "build_managed_channel",
        lambda _entry: adapter,
    )
    ctx = _admin_ctx()
    ctx.config = GatewayConfig()

    rpc_res = await get_dispatcher().dispatch(
        "r-unsupported",
        "channels.probe",
        {
            "entry": {
                "type": "dingtalk",
                "name": "dingtalk",
                "client_id": "dummy-client-id",
                "client_secret": "dummy-client-secret",
            }
        },
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload == {
        "status": "unsupported",
        "connected": False,
        "latencyMs": None,
        "detail": "This adapter does not yet expose a safe non-mutating live probe.",
    }
    assert adapter.stopped is True


@pytest.mark.asyncio
async def test_channels_probe_redacts_provider_error_and_result_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.channels import registry as channel_registry

    token = "123:short-secret"

    class FailingAdapter:
        def __init__(self) -> None:
            self.stopped = False

        async def probe_connection(self) -> dict[str, object]:
            raise RuntimeError(f"GET https://api.example/bot{token}/getMe failed")

        async def stop(self) -> None:
            self.stopped = True

    adapter = FailingAdapter()
    monkeypatch.setattr(
        channel_registry,
        "build_managed_channel",
        lambda _entry: adapter,
    )
    ctx = _admin_ctx()
    ctx.config = GatewayConfig()

    rpc_res = await get_dispatcher().dispatch(
        "r-secret-probe",
        "channels.probe",
        {
            "entry": {
                "type": "telegram",
                "name": "telegram",
                "token": token,
            }
        },
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["status"] == "failed"
    assert token not in repr(rpc_res.payload)
    assert "***" in rpc_res.payload["detail"]
    assert adapter.stopped is True


@pytest.mark.asyncio
async def test_channels_status_echoes_boot_id():
    ctx = _read_ctx()
    ctx.config = GatewayConfig()
    rpc_res = await get_dispatcher().dispatch("boot", "channels.status", {}, ctx)
    assert rpc_res.error is None, rpc_res.error
    # bootId is present (a hex string once booted, empty string in a bare test
    # process) so clients can distinguish a config change from a restart.
    assert "bootId" in rpc_res.payload
    assert isinstance(rpc_res.payload["bootId"], str)


@pytest.mark.asyncio
async def test_channels_restart_unloaded_channel_returns_typed_error():
    ctx = _admin_ctx()
    ctx.config = GatewayConfig()
    ctx.channel_manager = None
    rpc_res = await get_dispatcher().dispatch(
        "rs", "channels.restart", {"name": "ghost"}, ctx
    )
    assert rpc_res.error is not None
    assert rpc_res.error.code == "channels.adapter_not_loaded"
    assert "restart the gateway" in rpc_res.error.message


@pytest.mark.asyncio
async def test_channels_status_counts_pending_pairings_per_channel():
    class _Store:
        def list_pairings(self, *, channel_name=None, status=None):
            assert status == "pending"
            mk = type("P", (), {})
            out = []
            for name in ("work", "work", "other"):
                rec = mk()
                rec.channel_name = name
                out.append(rec)
            return out

    class _Manager:
        _delivery_store = _Store()
        _channel_types: dict = {}

        async def health(self):
            return {}

        def get(self, name):
            return None

    ctx = _read_ctx()
    res = upsert_channel(
        GatewayConfig(),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "xoxb-secret",
            "signing_secret": "ss",
        },
    )
    ctx.config = res.config
    ctx.channel_manager = _Manager()

    rpc_res = await get_dispatcher().dispatch("r1", "channels.status", {}, ctx)

    assert rpc_res.error is None, rpc_res.error
    rows = {row["name"]: row for row in rpc_res.payload["channels"]}
    assert rows["work"]["pendingPairings"] == 2


class _NoticeAdapter:
    """Captures what a channel would deliver to an approved sender."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[object] = []
        self._fail = fail

    async def send(self, message):
        if self._fail:
            raise RuntimeError("provider unreachable")
        self.sent.append(message)


class _NoticeStore:
    def __init__(self, status: str = "pending", reply_to: str | None = "dm-chat-1") -> None:
        self.record = SimpleNamespace(
            pairing_id="p1",
            channel_name="work",
            provider="slack",
            account_id="acct",
            sender_id="U-1",
            sender_name="Ada",
            status=status,
            created_at=1.0,
            approved_at=None,
            reply_to=reply_to,
        )

    def list_pairings(self, *, channel_name=None, status=None):
        if status is not None and self.record.status != status:
            return []
        return [self.record]

    def set_pairing_status(self, *, channel_name, pairing_id, status):
        self.record.status = status
        self.record.approved_at = 2.0
        return self.record


def _notice_ctx(adapter, store, *, notice: bool = True):
    ctx = _admin_ctx()
    res = upsert_channel(
        GatewayConfig(),
        entry_payload={
            "type": "slack",
            "name": "work",
            "token": "xoxb-secret",
            "signing_secret": "ss",
            "pairing_approved_notice": notice,
        },
    )
    ctx.config = res.config

    class _Manager:
        _delivery_store = store
        _channel_types: dict = {}

        async def health(self):
            return {}

        def get(self, name):
            return adapter

    ctx.channel_manager = _Manager()
    return ctx


@pytest.mark.asyncio
async def test_pairing_approve_notifies_sender_on_the_address_it_arrived_on():
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore())

    res = await get_dispatcher().dispatch(
        "r1", "channels.pairing.approve", {"channelName": "work", "pairingId": "p1"}, ctx
    )

    assert res.error is None, res.error
    assert len(adapter.sent) == 1
    assert adapter.sent[0].reply_to == "dm-chat-1"
    assert adapter.sent[0].content == "Access approved. Send a message to start chatting."
    assert adapter.sent[0].metadata.get("pairing_approved") is True


@pytest.mark.asyncio
async def test_pairing_approve_uses_the_persisted_gateway_notice_locale():
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore())
    ctx.config.control_ui.default_locale = "zh-Hans"

    res = await get_dispatcher().dispatch(
        "r1", "channels.pairing.approve", {"channelName": "work", "pairingId": "p1"}, ctx
    )

    assert res.error is None, res.error
    assert adapter.sent[0].content == "访问已获批准。请发送一条消息以开始对话。"


@pytest.mark.asyncio
async def test_pairing_approve_skips_notice_when_channel_opts_out():
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore(), notice=False)

    res = await get_dispatcher().dispatch(
        "r1", "channels.pairing.approve", {"channelName": "work", "pairingId": "p1"}, ctx
    )

    assert res.error is None, res.error
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_pairing_reapprove_does_not_notify_again():
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore(status="approved"))

    res = await get_dispatcher().dispatch(
        "r1", "channels.pairing.approve", {"channelName": "work", "pairingId": "p1"}, ctx
    )

    assert res.error is None, res.error
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_pairing_approve_survives_a_failing_notice():
    # The approval already committed; a delivery failure must not surface.
    ctx = _notice_ctx(_NoticeAdapter(fail=True), _NoticeStore())

    res = await get_dispatcher().dispatch(
        "r1", "channels.pairing.approve", {"channelName": "work", "pairingId": "p1"}, ctx
    )

    assert res.error is None, res.error
    assert res.payload["pairing"]["status"] == "approved"


@pytest.mark.asyncio
async def test_pairing_approve_without_a_stored_address_is_silent():
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore(reply_to=None))

    res = await get_dispatcher().dispatch(
        "r1", "channels.pairing.approve", {"channelName": "work", "pairingId": "p1"}, ctx
    )

    assert res.error is None, res.error
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_pairing_approve_as_admin_grants_and_persists(tmp_path, monkeypatch):
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")

    rpc_res = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.approve",
        {"channelName": "work", "pairingId": "p1", "asAdmin": True},
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["adminGranted"] is True
    # Live config sees the grant immediately (dispatch reads it per message).
    assert ctx.config.channel_admin_senders == {"work": ["U-1"]}
    # And it survived to disk: persist-before-apply wrote the TOML.
    text = (tmp_path / "config.toml").read_text()
    assert "channel_admin_senders" in text
    assert "U-1" in text


@pytest.mark.asyncio
async def test_pairing_approve_as_admin_is_idempotent(tmp_path):
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")
    ctx.config.channel_admin_senders = {"work": ["U-1"], "other": ["Z-9"]}

    rpc_res = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.approve",
        {"channelName": "work", "pairingId": "p1", "asAdmin": True},
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["adminGranted"] is True
    # No duplicate entry; unrelated channels' admins untouched.
    assert ctx.config.channel_admin_senders == {"work": ["U-1"], "other": ["Z-9"]}


@pytest.mark.asyncio
async def test_pairing_approve_without_as_admin_changes_no_config(tmp_path):
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")

    rpc_res = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.approve",
        {"channelName": "work", "pairingId": "p1"},
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert "adminGranted" not in rpc_res.payload
    assert ctx.config.channel_admin_senders == {}
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
async def test_admin_set_grants_and_persists(tmp_path):
    ctx = _notice_ctx(_NoticeAdapter(), _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")

    rpc_res = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "work", "senderId": "U-7", "admin": True},
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload == {
        "channelName": "work",
        "senderId": "U-7",
        "admin": True,
        "admins": ["U-7"],
    }
    # Live config sees the grant immediately (dispatch reads it per message).
    assert ctx.config.channel_admin_senders == {"work": ["U-7"]}
    # And it survived to disk: persist-before-apply wrote the TOML.
    text = (tmp_path / "config.toml").read_text()
    assert "channel_admin_senders" in text
    assert "U-7" in text


@pytest.mark.asyncio
async def test_admin_set_revoke_removes_sender_and_drops_empty_channel(tmp_path):
    ctx = _notice_ctx(_NoticeAdapter(), _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")
    ctx.config.channel_admin_senders = {"work": ["U-7"]}

    rpc_res = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "work", "senderId": "U-7", "admin": False},
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["admin"] is False
    assert rpc_res.payload["admins"] == []
    # The now-empty channel key is dropped rather than left as an empty list.
    assert ctx.config.channel_admin_senders == {}


@pytest.mark.asyncio
async def test_admin_set_grant_is_idempotent(tmp_path):
    ctx = _notice_ctx(_NoticeAdapter(), _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")
    ctx.config.channel_admin_senders = {"work": ["U-7"]}

    rpc_res = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "work", "senderId": "U-7", "admin": True},
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["admin"] is True
    # No duplicate entry appended.
    assert ctx.config.channel_admin_senders == {"work": ["U-7"]}


@pytest.mark.asyncio
async def test_admin_set_leaves_unrelated_channels_untouched(tmp_path):
    ctx = _notice_ctx(_NoticeAdapter(), _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")
    ctx.config.channel_admin_senders = {"work": ["U-7"], "other": ["Z-9"]}

    rpc_res = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "work", "senderId": "U-8", "admin": True},
        ctx,
    )

    assert rpc_res.error is None, rpc_res.error
    assert rpc_res.payload["admins"] == ["U-7", "U-8"]
    assert ctx.config.channel_admin_senders == {"work": ["U-7", "U-8"], "other": ["Z-9"]}


@pytest.mark.asyncio
async def test_admin_set_requires_channel_and_sender(tmp_path):
    ctx = _notice_ctx(_NoticeAdapter(), _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")

    missing_sender = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "work", "admin": True},
        ctx,
    )
    assert missing_sender.error is not None

    missing_channel = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"senderId": "U-7", "admin": True},
        ctx,
    )
    assert missing_channel.error is not None


@pytest.mark.asyncio
async def test_admin_set_requires_an_explicit_boolean_admin(tmp_path):
    # An omitted (or non-boolean) admin flag must be rejected, never coerced
    # into a silent revoke that returns a success payload.
    ctx = _notice_ctx(_NoticeAdapter(), _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")
    ctx.config.channel_admin_senders = {"work": ["U-7"]}

    omitted = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "work", "senderId": "U-7"},
        ctx,
    )
    assert omitted.error is not None
    assert ctx.config.channel_admin_senders == {"work": ["U-7"]}

    stringly = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "work", "senderId": "U-7", "admin": "false"},
        ctx,
    )
    assert stringly.error is not None
    assert ctx.config.channel_admin_senders == {"work": ["U-7"]}


@pytest.mark.asyncio
async def test_admin_set_grant_rejects_unknown_channel_but_revoke_still_works(tmp_path):
    ctx = _notice_ctx(_NoticeAdapter(), _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")
    # A stale admin entry for a channel that no longer exists in config.
    ctx.config.channel_admin_senders = {"gone": ["U-9"]}

    grant = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "not-a-channel", "senderId": "U-7", "admin": True},
        ctx,
    )
    assert grant.error is not None
    assert "not-a-channel" in str(grant.error)
    assert "not-a-channel" not in ctx.config.channel_admin_senders

    # Revokes stay unvalidated: TOML-added admins on removed channels must
    # remain demotable.
    revoke = await get_dispatcher().dispatch(
        "r1",
        "channels.admin.set",
        {"channelName": "gone", "senderId": "U-9", "admin": False},
        ctx,
    )
    assert revoke.error is None, revoke.error
    assert ctx.config.channel_admin_senders == {}


@pytest.mark.asyncio
async def test_pairing_revoke_drops_admin_standing_and_reapprove_does_not_restore(tmp_path):
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore(status="approved"))
    ctx.config.config_path = str(tmp_path / "config.toml")
    ctx.config.channel_admin_senders = {"work": ["U-1"], "other": ["Z-9"]}

    revoked = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.revoke",
        {"channelName": "work", "pairingId": "p1"},
        ctx,
    )
    assert revoked.error is None, revoked.error
    assert revoked.payload["adminRemoved"] is True
    # The withdrawn standing is gone from live config; other channels intact.
    assert ctx.config.channel_admin_senders == {"other": ["Z-9"]}

    # A plain Reapprove must NOT silently restore the admin surface.
    reapproved = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.approve",
        {"channelName": "work", "pairingId": "p1"},
        ctx,
    )
    assert reapproved.error is None, reapproved.error
    assert reapproved.payload["pairing"]["status"] == "approved"
    assert ctx.config.channel_admin_senders == {"other": ["Z-9"]}


@pytest.mark.asyncio
async def test_pairing_revoke_without_admin_standing_touches_no_config(tmp_path):
    ctx = _notice_ctx(_NoticeAdapter(), _NoticeStore(status="approved"))
    ctx.config.config_path = str(tmp_path / "config.toml")

    revoked = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.revoke",
        {"channelName": "work", "pairingId": "p1"},
        ctx,
    )
    assert revoked.error is None, revoked.error
    assert "adminRemoved" not in revoked.payload
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
async def test_pairing_approve_as_admin_survives_a_failed_grant_and_still_notifies(
    tmp_path, monkeypatch
):
    # The approval commits before the grant; a failing config persist must
    # surface as adminGranted=false + warning — NOT as an RPC error that
    # suppresses the approved-sender notice forever (a retry would see the
    # pairing as already approved and never notify).
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore())
    ctx.config.config_path = str(tmp_path / "config.toml")

    from opensquilla.gateway import rpc_config

    def _boom(_config):
        raise OSError("read-only config")

    monkeypatch.setattr(rpc_config, "_persist_config", _boom)

    res = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.approve",
        {"channelName": "work", "pairingId": "p1", "asAdmin": True},
        ctx,
    )

    assert res.error is None, res.error
    assert res.payload["pairing"]["status"] == "approved"
    assert res.payload["adminGranted"] is False
    assert res.payload["warnings"]
    # The transition happened in THIS call, so the sender still hears about it.
    assert len(adapter.sent) == 1
    # Nothing half-applied: the failed grant left live config untouched.
    assert ctx.config.channel_admin_senders == {}


@pytest.mark.asyncio
async def test_pairing_approve_as_admin_skips_grant_for_removed_channel(tmp_path):
    # Same guard channels.admin.set applies to grants: approving a stale
    # pairing asAdmin on a since-removed channel must not persist a dormant
    # admin entry that re-arms for a future channel with the same name.
    adapter = _NoticeAdapter()
    ctx = _notice_ctx(adapter, _NoticeStore())
    ctx.config = GatewayConfig()  # the "work" channel is no longer configured
    ctx.config.config_path = str(tmp_path / "config.toml")

    res = await get_dispatcher().dispatch(
        "r1",
        "channels.pairing.approve",
        {"channelName": "work", "pairingId": "p1", "asAdmin": True},
        ctx,
    )

    assert res.error is None, res.error
    assert res.payload["pairing"]["status"] == "approved"
    assert res.payload["adminGranted"] is False
    assert res.payload["warnings"]
    assert ctx.config.channel_admin_senders == {}
    assert not (tmp_path / "config.toml").exists()
