"""Onboarding-friendly channel catalog aligned with gateway config models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

FieldType = Literal["text", "password", "select", "bool", "int", "float"]
Transport = Literal["polling", "webhook", "websocket", "http_sync", "mixed", "unknown"]


@dataclass(frozen=True)
class ChannelSetupField:
    name: str
    label: str
    field_type: FieldType
    required: bool
    default: str | int | float | bool | None = None
    choices: tuple[str, ...] = ()
    description: str = ""
    secret: bool = False
    group: str = "basic"
    advanced: bool = False
    show_when: dict[str, str] | None = None
    help: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class ChannelSetupAid:
    """A provider-console shortcut surfaced next to the setup form.

    ``content`` is machine material only — a copyable blob or a URL template
    with ``{app_id}`` placeholders; all display text is localized by the Web
    UI from ``id``. Kinds: ``copy`` (clipboard blob), ``link`` (console deep
    link), ``note`` (localized guidance with no machine content).
    """

    id: str
    kind: Literal["copy", "link", "note"]
    content: str = ""


@dataclass(frozen=True)
class ChannelSetupSpec:
    type: str
    label: str
    description: str
    transport: Transport
    requires_public_url: bool
    dependency_extra: str | None
    restart_required: bool
    docs_hint: str
    fields: tuple[ChannelSetupField, ...]
    help: str = ""
    blocking: bool = False
    can_probe: bool = True
    readme_scenarios: tuple[str, ...] = ("chat channels", "first-run setup")
    setup_aids: tuple[ChannelSetupAid, ...] = ()


def _common_fields() -> tuple[ChannelSetupField, ...]:
    return (
        ChannelSetupField(
            "name", "Channel name", "text", required=True,
            description="Unique identifier for this channel entry.",
        ),
        # Safe-defaulted identity plumbing: folded into the Advanced
        # disclosure on every channel form so a first-time add is credentials
        # plus a name, nothing else. Routing to a non-default agent and
        # creating a channel disabled are deliberate, rare actions.
        ChannelSetupField(
            "agent_id", "Agent id", "text", required=False, default="main",
            advanced=True,
        ),
        ChannelSetupField(
            "enabled", "Enabled", "bool", required=False, default=True,
            advanced=True,
        ),
        ChannelSetupField(
            "group_session_scope",
            "Group session scope",
            "select",
            required=False,
            default="per_sender",
            choices=("per_sender", "shared_room"),
            description="Choose whether group history is isolated per participant.",
            help=(
                "per_sender keeps each participant's context separate inside a room. "
                "shared_room intentionally gives the whole room one transcript."
            ),
            group="behavior",
            advanced=True,
        ),
        ChannelSetupField(
            "busy_input_mode",
            "Input while busy",
            "select",
            required=False,
            default="followup",
            choices=("followup", "queue", "steer", "interrupt"),
            description="Control how new messages enter a session that is already running.",
            help=(
                "followup preserves the default FIFO behavior; queue records an explicit "
                "FIFO request. steer and interrupt cancel existing session work before the "
                "new message is enqueued. Unsupported runtime modes fall back to followup."
            ),
            group="behavior",
            advanced=True,
        ),
        ChannelSetupField(
            "dm_access",
            "Direct-message access",
            "select",
            required=False,
            default="pairing",
            choices=("pairing", "open", "allowlist"),
            description=(
                "Pairing safely holds unknown authenticated senders for operator approval. "
                "Open admits every authenticated sender; allowlist admits only listed ids."
            ),
            help="Use pairing unless this bot is intentionally public.",
            group="access",
            advanced=True,
        ),
        ChannelSetupField(
            "pairing_approved_notice",
            "Tell senders when approved",
            "bool",
            required=False,
            default=True,
            description=(
                "When you approve a pairing request, the bot replies to that sender so "
                "they know to send a message. Turn off to approve silently."
            ),
            group="access",
            advanced=True,
            show_when={"dm_access": "pairing"},
        ),
        ChannelSetupField(
            "allowed_senders",
            "Allowed sender ids",
            "text",
            required=False,
            default="",
            description=(
                "Comma-separated provider sender ids. In allowlist mode these admit "
                "direct messages; when populated they also constrain group senders."
            ),
            placeholder="user-id-1, user-id-2",
            show_when={"dm_access": "allowlist"},
            group="access",
        ),
    )


def _slack_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="slack",
        label="Slack",
        description="Slack workspace bot - Socket Mode (websocket) or Events API webhook.",
        transport="mixed",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://api.slack.com/apps",
        help=(
            "connection_mode=socket uses Slack Socket Mode (an outbound websocket) and "
            "needs no public URL - set app_token (xapp-...). connection_mode=webhook uses "
            "the Events API and needs a public Request URL reachable by Slack."
        ),
        fields=(
            *_common_fields(),
            ChannelSetupField("token", "Bot token (xoxb-...)", "password",
                              required=True, secret=True, group="credentials",
                              placeholder="xoxb-..."),
            ChannelSetupField("app_token", "App-level token (xapp-...)", "password",
                              required=False, secret=True, group="credentials",
                              placeholder="xapp-...",
                              show_when={"connection_mode": "socket"}),
            ChannelSetupField("slack_channel_id", "Default channel id", "text",
                              required=False, default="", advanced=True,
                              description="Optional; replies auto-target the incoming "
                              "conversation when unset."),
            # Required whenever connection_mode=webhook (the default), so it
            # must NOT fold into Advanced — a hidden required field blocks
            # Save with no visible blank to fill.
            ChannelSetupField("signing_secret", "Signing secret", "password",
                              required=True, secret=True, group="credentials",
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("reply_in_thread", "Reply in thread", "bool",
                              required=False, default=False, advanced=True),
            ChannelSetupField("connection_mode", "Connection mode", "select",
                              required=False, default="webhook",
                              choices=("webhook", "socket")),
        ),
    )


# Every tenant scope the Feishu CHANNEL itself exercises — minimum privilege
# for the conversation surface (send/reply/edit/recall as bot, attachment
# up/download, and the inbound read scopes behind im.message.receive_v1 for
# DMs and group @-mentions). Vendor API surfaces (docs/drive/wiki) are
# Feishu's own MCP server and CLI with their own authorization flow, so
# their scopes are deliberately NOT in this paste-once manifest. The
# status-reaction feature self-disables when unauthorized and is likewise
# excluded.
FEISHU_TENANT_SCOPES: tuple[str, ...] = (
    "im:message",
    "im:message.group_at_msg:readonly",
    "im:message.p2p_msg:readonly",
    "im:message:readonly",
    "im:message:send_as_bot",
    "im:message:update",
    "im:resource",
)


def _feishu_setup_aids() -> tuple[ChannelSetupAid, ...]:
    import json as _json

    scopes_json = _json.dumps(
        {"scopes": {"tenant": list(FEISHU_TENANT_SCOPES), "user": []}},
        indent=2,
    )
    quick_apply = (
        "https://open.feishu.cn/app/{app_id}/auth?q="
        + ",".join(FEISHU_TENANT_SCOPES)
        + "&op_from=openapi&token_type=tenant"
    )
    return (
        ChannelSetupAid(id="scopes_json", kind="copy", content=scopes_json),
        ChannelSetupAid(
            id="credentials_link",
            kind="link",
            content="https://open.feishu.cn/app/{app_id}/baseinfo",
        ),
        ChannelSetupAid(id="quick_apply_link", kind="link", content=quick_apply),
        ChannelSetupAid(id="ws_order_note", kind="note"),
    )


def _feishu_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="feishu",
        label="Feishu / Lark",
        description="Feishu (or Lark) bot — webhook or websocket connection.",
        setup_aids=_feishu_setup_aids(),
        transport="mixed",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://open.feishu.cn/document/",
        help=(
            "Default websocket mode only needs App id and App secret. "
            "Webhook verification fields are only needed when connection_mode=webhook. "
            "After the channel reports an open websocket connection, check Event & "
            "Callbacks in the Feishu console and confirm long-connection event delivery "
            "if expected events do not arrive. A zero ingress count alone does not prove "
            "the console is misconfigured."
        ),
        fields=(
            *_common_fields(),
            ChannelSetupField("app_id", "App id", "text", required=True,
                              group="credentials", placeholder="cli_..."),
            ChannelSetupField("app_secret", "App secret", "password",
                              required=True, secret=True, group="credentials",
                              placeholder="app secret"),
            # Folded: websocket is the default and right for almost everyone;
            # switching to webhook happens inside Advanced, where the mode
            # select is declared FIRST so the webhook fields it reveals appear
            # below it. Websocket subscription guidance lives post-save on the
            # channel page plus in the spec help above for headless clients.
            ChannelSetupField("connection_mode", "Connection mode", "select",
                              required=False, default="websocket",
                              choices=("webhook", "websocket"),
                              advanced=True),
            ChannelSetupField("encrypt_key", "Encrypt key", "password",
                              required=False, secret=True, default="",
                              group="webhook", advanced=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("verification_token", "Verification token", "password",
                              required=False, secret=True, default="",
                              group="webhook", advanced=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("webhook_path", "Webhook path", "text",
                              required=False, default="/feishu/events",
                              group="webhook", advanced=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("default_chat_id", "Default chat id", "text",
                              required=False, default="", advanced=True),
            ChannelSetupField(
                "status_reactions_enabled",
                "Message status reactions",
                "bool",
                required=False,
                default=True,
                advanced=True,
                help="Adds short-lived Feishu reactions while a message is received and processed.",
            ),
            ChannelSetupField("api_base", "API base", "text",
                              required=False,
                              default="https://open.feishu.cn/open-apis",
                              advanced=True),
            # The one genuine decision besides credentials: which console
            # issued the app. Stays front — a Lark operator on the feishu
            # endpoint gets a broken connection with no signal.
            ChannelSetupField("domain", "Domain", "select",
                              required=False, default="feishu",
                              choices=("feishu", "lark"),
                              description="feishu = China mainland · "
                                          "lark = Lark / international"),
        ),
    )


def _discord_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="discord",
        label="Discord",
        description="Discord bot using gateway websocket.",
        transport="websocket",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://discord.com/developers/applications",
        fields=(
            *_common_fields(),
            ChannelSetupField("token", "Bot token", "password",
                              required=True, secret=True),
            ChannelSetupField("application_id", "Application id", "text",
                              required=False, default="", advanced=True),
            ChannelSetupField("default_channel_id", "Default channel id", "text",
                              required=False, default="", advanced=True),
            ChannelSetupField("gateway_url", "Gateway URL", "text",
                              required=False, advanced=True,
                              default="wss://gateway.discord.gg/?v=10&encoding=json"),
            ChannelSetupField("intents", "Intents bitmask", "int",
                              required=False, default=46593, advanced=True),
        ),
    )


def _dingtalk_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="dingtalk",
        label="DingTalk",
        description="DingTalk corp robot via stream connection.",
        transport="websocket",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://open.dingtalk.com/document/",
        fields=(
            *_common_fields(),
            ChannelSetupField("client_id", "Client id", "text", required=True,
                              group="credentials"),
            ChannelSetupField("client_secret", "Client secret", "password",
                              required=True, secret=True, group="credentials"),
        ),
    )


def _wecom_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="wecom",
        label="WeCom",
        description="Enterprise WeChat AI Bot over websocket, or corp-app webhook callback.",
        transport="mixed",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://developer.work.weixin.qq.com/document/path/101463",
        help=(
            "connection_mode=websocket uses the WeCom AI Bot long connection "
            "with Bot ID and Secret; no public URL is required. "
            "connection_mode=webhook uses a corp app callback and requires a public URL."
        ),
        fields=(
            *_common_fields(),
            # The default must mirror ``WeComChannelEntry.connection_mode``:
            # a headless entry that omits connection_mode is validated in the
            # pydantic default mode, so an advertised minimal setup seeded
            # from a diverging spec default would always fail validation.
            ChannelSetupField("connection_mode", "Connection mode", "select",
                              required=False, default="webhook",
                              choices=("websocket", "webhook")),
            ChannelSetupField("bot_id", "Bot ID", "text", required=True,
                              group="credentials",
                              show_when={"connection_mode": "websocket"}),
            ChannelSetupField("bot_secret", "Bot secret", "password",
                              required=True, secret=True, group="credentials",
                              show_when={"connection_mode": "websocket"}),
            ChannelSetupField("websocket_url", "WebSocket URL", "text",
                              required=False, default="wss://openws.work.weixin.qq.com",
                              advanced=True,
                              show_when={"connection_mode": "websocket"}),
            ChannelSetupField("corp_id", "Corp id", "text", required=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("corp_secret", "Corp secret", "password",
                              required=True, secret=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("agent_id_int", "Agent id (int)", "int",
                              required=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("token", "Token", "password",
                              required=True, secret=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("encoding_aes_key", "Encoding AES key", "password",
                              required=True, secret=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("webhook_path", "Webhook path", "text",
                              required=False, default="/wecom/events",
                              advanced=True,
                              show_when={"connection_mode": "webhook"}),
            ChannelSetupField("api_base", "API base", "text",
                              required=False,
                              default="https://qyapi.weixin.qq.com",
                              advanced=True,
                              show_when={"connection_mode": "webhook"}),
        ),
    )


def _qq_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="qq",
        label="QQ Bot",
        description="Tencent QQ Bot via websocket.",
        transport="websocket",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://bot.q.qq.com/wiki/",
        # No non-mutating probe_connection on the QQ adapter yet.
        can_probe=False,
        fields=(
            *_common_fields(),
            ChannelSetupField("app_id", "App id", "text", required=True,
                              group="credentials"),
            ChannelSetupField("app_secret", "App secret", "password",
                              required=True, secret=True, group="credentials"),
        ),
    )


def _msteams_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="msteams",
        label="Microsoft Teams",
        description="Microsoft Teams via Bot Framework webhook.",
        transport="webhook",
        requires_public_url=True,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://learn.microsoft.com/microsoftteams/platform/",
        help="Microsoft Teams Bot Framework webhooks require a public HTTPS URL.",
        fields=(
            *_common_fields(),
            ChannelSetupField("app_id", "App id", "text", required=True),
            ChannelSetupField("app_password", "App password", "password",
                              required=True, secret=True),
            ChannelSetupField("webhook_path", "Webhook path", "text",
                              required=False, default="/msteams/messages"),
        ),
    )


def _matrix_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="matrix",
        label="Matrix",
        description="Matrix homeserver client (sync long-poll).",
        transport="http_sync",
        requires_public_url=False,
        dependency_extra="matrix",
        restart_required=True,
        docs_hint="https://matrix.org/docs/",
        # No non-mutating probe_connection on the Matrix adapter yet.
        can_probe=False,
        fields=(
            *_common_fields(),
            ChannelSetupField("homeserver_url", "Homeserver URL", "text",
                              required=True),
            ChannelSetupField("user_id", "User id (@user:server)", "text",
                              required=True),
            ChannelSetupField("password", "Password", "password",
                              required=False, secret=True, default=""),
            ChannelSetupField("access_token", "Access token", "password",
                              required=False, secret=True, default=""),
            ChannelSetupField("device_id", "Device id", "text",
                              required=False, default="", advanced=True),
            ChannelSetupField("encryption", "Encryption", "select",
                              required=False, default="off",
                              choices=("off", "required", "best_effort"),
                              advanced=True),
        ),
    )


def _telegram_spec() -> ChannelSetupSpec:
    return ChannelSetupSpec(
        type="telegram",
        label="Telegram",
        description="Telegram Bot API — polling or webhook transport.",
        transport="mixed",
        requires_public_url=False,
        dependency_extra=None,
        restart_required=True,
        docs_hint="https://core.telegram.org/bots/api",
        fields=(
            *_common_fields(),
            ChannelSetupField("token", "Bot token", "password",
                              required=True, secret=True, group="credentials",
                              placeholder="123456:ABC..."),
            ChannelSetupField("default_chat_id", "Default chat id", "text",
                              required=False, default="", advanced=True),
            ChannelSetupField("api_base", "API base", "text",
                              required=False, default="https://api.telegram.org",
                              advanced=True),
            ChannelSetupField("transport_name", "Transport", "select",
                              required=False, default="polling",
                              choices=("polling", "webhook")),
            ChannelSetupField("webhook_path", "Webhook path", "text",
                              required=False, default="/telegram/events",
                              group="webhook",
                              show_when={"transport_name": "webhook"}),
            ChannelSetupField("webhook_url", "Webhook URL (webhook only)", "text",
                              required=False, default="", group="webhook",
                              show_when={"transport_name": "webhook"},
                              placeholder="https://example.com/telegram/events"),
            ChannelSetupField("webhook_secret_token", "Webhook secret token",
                              "password", required=False, secret=True, default="",
                              group="webhook",
                              show_when={"transport_name": "webhook"}),
            ChannelSetupField("drop_pending_updates", "Drop pending updates",
                              "bool", required=False, default=False, advanced=True),
            ChannelSetupField("poll_timeout_s", "Polling timeout (s)", "int",
                              required=False, default=30, group="polling",
                              advanced=True,
                              show_when={"transport_name": "polling"}),
            ChannelSetupField("poll_limit", "Poll limit", "int",
                              required=False, default=100, group="polling",
                              advanced=True,
                              show_when={"transport_name": "polling"}),
            ChannelSetupField("poll_idle_sleep_s", "Poll idle sleep (s)", "float",
                              required=False, default=0.1, group="polling",
                              advanced=True,
                              show_when={"transport_name": "polling"}),
        ),
    )


_BUILDERS = {
    "dingtalk": _dingtalk_spec,
    "discord": _discord_spec,
    "feishu": _feishu_spec,
    "matrix": _matrix_spec,
    # msteams is intentionally absent: the adapter is text-only and hidden
    # from runtime catalog surfaces until first-class support lands. The
    # _msteams_spec helper is retained for future restoration.
    "qq": _qq_spec,
    "slack": _slack_spec,
    "telegram": _telegram_spec,
    "wecom": _wecom_spec,
}


def list_channel_setup_specs() -> list[ChannelSetupSpec]:
    return [_BUILDERS[t]() for t in sorted(_BUILDERS)]


def get_channel_setup_spec(type_name: str) -> ChannelSetupSpec:
    if type_name not in _BUILDERS:
        raise KeyError(f"unknown channel type: {type_name!r}")
    return _BUILDERS[type_name]()


def channel_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "type": s.type,
            "label": s.label,
            "description": s.description,
            "transport": s.transport,
            "requiresPublicUrl": s.requires_public_url,
            "dependencyExtra": s.dependency_extra,
            "restartRequired": s.restart_required,
            "docsHint": s.docs_hint,
            "help": s.help,
            "blocking": s.blocking,
            "canProbe": s.can_probe,
            "readmeScenarios": list(s.readme_scenarios),
            "whatYouNeed": _what_you_need(s),
            "setupAids": [
                {"id": aid.id, "kind": aid.kind, "content": aid.content}
                for aid in s.setup_aids
            ],
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.field_type,
                    "required": f.required,
                    "default": f.default,
                    "choices": list(f.choices),
                    "description": f.description,
                    "secret": f.secret,
                    "group": f.group,
                    "advanced": f.advanced,
                    "showWhen": dict(f.show_when or {}),
                    "help": f.help,
                    "placeholder": f.placeholder,
                }
                for f in s.fields
            ],
        }
        for s in list_channel_setup_specs()
    ]


def _what_you_need(spec: ChannelSetupSpec) -> list[str]:
    defaults = {field.name: field.default for field in spec.fields}
    needs = [
        f"{field.label}."
        for field in spec.fields
        if field.required
        and field.name not in {"name", "enabled", "agent_id"}
        and _field_visible_by_default(field, defaults)
    ]
    if spec.requires_public_url:
        needs.append("A public URL reachable by the channel provider.")
    if spec.dependency_extra:
        needs.append(f"Install the `{spec.dependency_extra}` optional extra.")
    if not needs:
        needs.append("A channel entry name and provider-side bot/app setup.")
    return needs


def _field_visible_by_default(
    field: ChannelSetupField,
    defaults: dict[str, str | int | float | bool | None],
) -> bool:
    if not field.show_when:
        return True
    return all(str(defaults.get(key, "")) == expected for key, expected in field.show_when.items())
