"""The Feishu setup aids must track what the channel actually uses.

The scope manifest is a paste-once console import; a scope the adapter needs
but the manifest lacks means the operator pastes an incomplete list and the
channel 403s later with no obvious cause. Vendor API surfaces (docs/drive/
wiki) are Feishu's own MCP server and CLI with their own authorization flow,
so their scopes must NOT creep back into this channel manifest.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from opensquilla.onboarding.channel_specs import (
    FEISHU_TENANT_SCOPES,
    channel_catalog_payload,
    get_channel_setup_spec,
)

# The channel's own needs: bot send/reply/edit/recall, attachment up/download,
# and the event read scopes behind im.message.receive_v1 (DM + group mention).
_CHANNEL_SCOPES = {
    "im:message",
    "im:message.group_at_msg:readonly",
    "im:message.p2p_msg:readonly",
    "im:message:readonly",
    "im:message:send_as_bot",
    "im:message:update",
    "im:resource",
}


def _aids_by_id() -> dict[str, object]:
    spec = get_channel_setup_spec("feishu")
    return {aid.id: aid for aid in spec.setup_aids}


def test_scope_manifest_is_exactly_the_channel_needs() -> None:
    assert set(FEISHU_TENANT_SCOPES) == _CHANNEL_SCOPES


def test_scope_manifest_stays_minimum_privilege() -> None:
    # Docs/drive/wiki scopes belong to Feishu's own MCP/CLI authorization
    # flow; the channel manifest asking for them would over-grant the bot.
    leaked = {
        scope
        for scope in FEISHU_TENANT_SCOPES
        if scope.split(":")[0] in {"docx", "drive", "wiki"}
    }
    assert not leaked, f"vendor tool-surface scopes leaked into the channel manifest: {leaked}"


def test_scopes_json_aid_is_the_console_import_shape() -> None:
    aid = _aids_by_id()["scopes_json"]
    manifest = json.loads(aid.content)
    assert manifest == {"scopes": {"tenant": list(FEISHU_TENANT_SCOPES), "user": []}}


def test_quick_apply_link_carries_the_same_scopes() -> None:
    aid = _aids_by_id()["quick_apply_link"]
    assert "{app_id}" in aid.content
    parsed = urlparse(aid.content.replace("{app_id}", "cli_test"))
    query = parse_qs(parsed.query)
    assert query["q"][0].split(",") == list(FEISHU_TENANT_SCOPES)
    assert query["token_type"] == ["tenant"]


def test_catalog_payload_serializes_setup_aids() -> None:
    feishu = next(row for row in channel_catalog_payload() if row["type"] == "feishu")
    aids = {aid["id"]: aid for aid in feishu["setupAids"]}
    assert set(aids) == {"scopes_json", "credentials_link", "quick_apply_link", "ws_order_note"}
    assert aids["ws_order_note"]["kind"] == "note"
    # Channels without aids serialize an empty list, not a missing key.
    slack = next(row for row in channel_catalog_payload() if row["type"] == "slack")
    assert slack["setupAids"] == []


def test_headless_help_treats_zero_ingress_as_guidance_not_proof() -> None:
    help_text = get_channel_setup_spec("feishu").help
    assert "check Event & Callbacks" in help_text
    assert "zero ingress count alone does not prove" in help_text
    assert "only persists that choice" not in help_text
