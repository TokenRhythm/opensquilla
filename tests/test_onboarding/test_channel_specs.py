"""Tests for the channel catalog."""

from __future__ import annotations

import pytest

from opensquilla.gateway.config import (
    DingTalkChannelEntry,
    DiscordChannelEntry,
    FeishuChannelEntry,
    MatrixChannelEntry,
    QQChannelEntry,
    SlackChannelEntry,
    TelegramChannelEntry,
    WeComChannelEntry,
)
from opensquilla.onboarding.channel_specs import (
    ChannelSetupSpec,
    channel_catalog_payload,
    get_channel_setup_spec,
    list_channel_setup_specs,
)

# msteams is intentionally absent: the adapter is text-only and hidden
# from runtime catalog surfaces until first-class support lands.
ALL_TYPES = {
    "slack",
    "feishu",
    "discord",
    "dingtalk",
    "wecom",
    "qq",
    "matrix",
    "telegram",
}

ENTRY_MODELS = {
    "slack": SlackChannelEntry,
    "feishu": FeishuChannelEntry,
    "discord": DiscordChannelEntry,
    "dingtalk": DingTalkChannelEntry,
    "wecom": WeComChannelEntry,
    "qq": QQChannelEntry,
    "matrix": MatrixChannelEntry,
    "telegram": TelegramChannelEntry,
}

EXPECTED_PUBLIC_URL: set[str] = set()
CONDITIONAL_PUBLIC_URL = {"feishu", "slack", "telegram", "wecom"}


def test_catalog_includes_all_channels():
    types = {s.type for s in list_channel_setup_specs()}
    assert types == ALL_TYPES


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_each_channel_has_common_fields(type_name: str):
    spec = get_channel_setup_spec(type_name)
    names = {f.name for f in spec.fields}
    assert {
        "name",
        "enabled",
        "agent_id",
        "group_session_scope",
        "busy_input_mode",
    } <= names


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_each_channel_exposes_safe_session_and_busy_policy(type_name: str):
    fields = {field.name: field for field in get_channel_setup_spec(type_name).fields}

    group_scope = fields["group_session_scope"]
    assert group_scope.default == "per_sender"
    assert group_scope.choices == ("per_sender", "shared_room")
    assert group_scope.advanced is True
    assert group_scope.help

    busy_mode = fields["busy_input_mode"]
    assert busy_mode.default == "followup"
    assert busy_mode.choices == ("followup", "queue", "steer", "interrupt")
    assert busy_mode.advanced is True
    assert busy_mode.help

    dm_access = fields["dm_access"]
    assert dm_access.default == "pairing"
    assert dm_access.choices == ("pairing", "open", "allowlist")
    assert dm_access.advanced is True
    assert fields["allowed_senders"].show_when == {"dm_access": "allowlist"}


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_safe_defaulted_identity_fields_fold_into_advanced(type_name: str):
    """The first-time add form is credentials plus a name: the safe-defaulted
    identity plumbing (agent routing, the enable switch) folds into the
    Advanced disclosure on EVERY channel, while the identity key itself stays
    front and required. Positive assertions so the posture cannot silently
    drift back to an eight-row default view."""
    fields = {field.name: field for field in get_channel_setup_spec(type_name).fields}

    assert fields["agent_id"].advanced is True
    assert fields["agent_id"].default == "main"
    assert fields["enabled"].advanced is True
    assert fields["enabled"].default is True

    assert fields["name"].required is True
    assert fields["name"].advanced is False


# Per-channel default-view posture: safe-defaulted optional fields fold into
# Advanced; the fields that stay front are the credentials plus the genuine
# decisions (transports, region). Positive pins per channel so a spec edit
# that re-expands the default view fails loudly.
ADVANCED_FOLDED_FIELDS = {
    "discord": {"application_id", "default_channel_id", "gateway_url", "intents"},
    "feishu": {"default_chat_id", "connection_mode", "webhook_path"},
    "matrix": {"device_id", "encryption"},
    "slack": {"slack_channel_id", "reply_in_thread"},
    "telegram": {
        "default_chat_id",
        "api_base",
        "drop_pending_updates",
        "poll_timeout_s",
        "poll_limit",
        "poll_idle_sleep_s",
    },
    "wecom": {"webhook_path"},
}


@pytest.mark.parametrize("type_name", sorted(ADVANCED_FOLDED_FIELDS))
def test_safe_defaulted_channel_fields_fold_into_advanced(type_name: str):
    fields = {field.name: field for field in get_channel_setup_spec(type_name).fields}
    for name in ADVANCED_FOLDED_FIELDS[type_name]:
        assert fields[name].advanced is True, f"{type_name}.{name} should fold into Advanced"
        assert fields[name].required is False
        assert fields[name].default is not None


def test_required_fields_never_fold_into_advanced():
    """A required field hidden in the Advanced fold blocks Save with no
    visible blank to fill (the historical slack signing_secret bug: required
    whenever connection_mode=webhook — the default — yet folded away)."""
    for spec in list_channel_setup_specs():
        for field in spec.fields:
            if field.required:
                assert field.advanced is False, (
                    f"{spec.type}.{field.name} is required but folded into Advanced"
                )


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_spec_fields_align_with_pydantic_model(type_name: str):
    spec = get_channel_setup_spec(type_name)
    model = ENTRY_MODELS[type_name]
    pydantic_fields = set(model.model_fields.keys())
    spec_fields = {f.name for f in spec.fields}
    assert "type" not in spec_fields
    extra = spec_fields - pydantic_fields - {"type"}
    assert not extra, f"setup spec exposes unknown field(s): {extra}"


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_required_pydantic_fields_are_required_in_spec(type_name: str):
    spec = get_channel_setup_spec(type_name)
    model = ENTRY_MODELS[type_name]
    spec_required = {f.name for f in spec.fields if f.required}
    for fname, finfo in model.model_fields.items():
        if fname == "type":
            continue
        if finfo.is_required():
            assert fname in spec_required, (
                f"{type_name}.{fname} is required in pydantic but not in setup spec"
            )


def test_slack_secrets_are_marked_secret():
    spec = get_channel_setup_spec("slack")
    secrets = {f.name for f in spec.fields if f.secret}
    assert {"token", "app_token", "signing_secret"} <= secrets


def test_telegram_secrets_are_marked_secret():
    spec = get_channel_setup_spec("telegram")
    secrets = {f.name for f in spec.fields if f.secret}
    assert {"token", "webhook_secret_token"} <= secrets


def test_discord_gateway_auth_fields_do_not_expose_interactions_public_key():
    spec = get_channel_setup_spec("discord")
    fields = {f.name: f for f in spec.fields}

    assert fields["token"].required is True
    assert fields["token"].secret is True
    assert fields["application_id"].secret is False
    assert "public_key" not in fields


def test_dingtalk_stream_credentials_are_marked_correctly():
    spec = get_channel_setup_spec("dingtalk")
    fields = {f.name: f for f in spec.fields}

    assert fields["client_id"].required is True
    assert fields["client_id"].secret is False
    assert fields["client_secret"].required is True
    assert fields["client_secret"].secret is True


def test_feishu_webhook_secrets_are_marked_secret():
    spec = get_channel_setup_spec("feishu")
    secrets = {f.name for f in spec.fields if f.secret}

    assert {"app_secret", "encrypt_key", "verification_token"} <= secrets


def test_feishu_connection_mode_choices():
    spec = get_channel_setup_spec("feishu")
    field = next(f for f in spec.fields if f.name == "connection_mode")
    assert field.field_type == "select"
    assert field.default == "websocket"
    assert field.choices == ("webhook", "websocket")


def test_matrix_transport_matches_client_sync_runtime():
    assert get_channel_setup_spec("matrix").transport == "http_sync"


def test_slack_connection_mode_choices():
    spec = get_channel_setup_spec("slack")
    field = next(f for f in spec.fields if f.name == "connection_mode")
    assert field.field_type == "select"
    assert field.default == "webhook"
    assert field.choices == ("webhook", "socket")
    assert field.advanced is False


def test_wecom_connection_mode_choices():
    spec = get_channel_setup_spec("wecom")
    field = next(f for f in spec.fields if f.name == "connection_mode")
    assert spec.transport == "mixed"
    assert spec.requires_public_url is False
    assert field.field_type == "select"
    # Must mirror WeComChannelEntry.connection_mode: a headless entry that
    # omits connection_mode is validated in the pydantic default mode.
    assert field.default == "webhook"
    assert field.default == WeComChannelEntry.model_fields["connection_mode"].default
    assert field.choices == ("websocket", "webhook")


def test_wecom_mode_specific_fields_are_conditional():
    spec = get_channel_setup_spec("wecom")
    fields = {f.name: f for f in spec.fields}

    assert fields["bot_id"].show_when == {"connection_mode": "websocket"}
    assert fields["bot_secret"].show_when == {"connection_mode": "websocket"}
    assert fields["websocket_url"].show_when == {"connection_mode": "websocket"}
    assert fields["corp_id"].show_when == {"connection_mode": "webhook"}
    assert fields["corp_secret"].show_when == {"connection_mode": "webhook"}
    assert fields["token"].show_when == {"connection_mode": "webhook"}
    assert fields["encoding_aes_key"].show_when == {"connection_mode": "webhook"}
    assert fields["bot_secret"].secret is True
    assert fields["corp_secret"].secret is True


def test_slack_mode_specific_fields_are_conditional():
    spec = get_channel_setup_spec("slack")
    fields = {f.name: f for f in spec.fields}
    assert fields["app_token"].show_when == {"connection_mode": "socket"}
    assert fields["signing_secret"].show_when == {"connection_mode": "webhook"}
    assert fields["signing_secret"].required is True
    assert fields["slack_channel_id"].required is False


def test_feishu_status_reactions_are_enabled_by_default():
    entry = FeishuChannelEntry(
        name="feishu",
        app_id="cli_test",
        app_secret="secret",
    )

    assert entry.status_reactions_enabled is True


def test_feishu_status_reactions_are_exposed_in_setup_spec():
    spec = get_channel_setup_spec("feishu")
    field = next(f for f in spec.fields if f.name == "status_reactions_enabled")

    assert field.field_type == "bool"
    assert field.default is True
    assert field.advanced is True


def test_feishu_webhook_fields_are_conditional():
    spec = get_channel_setup_spec("feishu")
    fields = {f.name: f for f in spec.fields}
    assert fields["webhook_path"].show_when == {"connection_mode": "webhook"}
    assert fields["verification_token"].show_when == {"connection_mode": "webhook"}
    assert fields["encrypt_key"].advanced is True


def test_telegram_webhook_fields_are_conditional():
    spec = get_channel_setup_spec("telegram")
    fields = {f.name: f for f in spec.fields}
    assert fields["transport_name"].default == "polling"
    assert fields["webhook_path"].show_when == {"transport_name": "webhook"}
    assert fields["webhook_url"].show_when == {"transport_name": "webhook"}
    assert fields["webhook_secret_token"].show_when == {"transport_name": "webhook"}
    assert fields["poll_timeout_s"].show_when == {"transport_name": "polling"}


def test_channel_catalog_payload_exposes_ui_metadata():
    payload = channel_catalog_payload()
    feishu = next(c for c in payload if c["type"] == "feishu")
    fields = {f["name"]: f for f in feishu["fields"]}
    assert fields["app_secret"]["group"] == "credentials"
    assert fields["app_secret"]["placeholder"]
    assert fields["webhook_path"]["showWhen"] == {"connection_mode": "webhook"}
    assert fields["encrypt_key"]["advanced"] is True
    assert fields["group_session_scope"]["default"] == "per_sender"
    assert fields["group_session_scope"]["help"]
    assert fields["busy_input_mode"]["choices"] == [
        "followup",
        "queue",
        "steer",
        "interrupt",
    ]
    assert feishu["blocking"] is False
    assert feishu["whatYouNeed"]
    slack = next(c for c in payload if c["type"] == "slack")
    assert "public URL" in slack["help"]
    assert slack["transport"] == "mixed"
    assert slack["requiresPublicUrl"] is False
    slack_fields = {f["name"]: f for f in slack["fields"]}
    assert slack_fields["app_token"]["showWhen"] == {"connection_mode": "socket"}
    wecom = next(c for c in payload if c["type"] == "wecom")
    # whatYouNeed follows the spec default mode, which mirrors the pydantic
    # default (webhook): the advertised minimal setup names the fields that
    # webhook-mode validation actually requires.
    assert "Corp id." in wecom["whatYouNeed"]
    assert "Corp secret." in wecom["whatYouNeed"]
    assert "Encoding AES key." in wecom["whatYouNeed"]
    assert "Bot ID." not in wecom["whatYouNeed"]
    assert "Bot secret." not in wecom["whatYouNeed"]


def test_matrix_encryption_choices():
    spec = get_channel_setup_spec("matrix")
    field = next(f for f in spec.fields if f.name == "encryption")
    assert field.choices == ("off", "required", "best_effort")


@pytest.mark.parametrize("type_name", sorted(EXPECTED_PUBLIC_URL))
def test_webhook_channels_require_public_url(type_name: str):
    spec = get_channel_setup_spec(type_name)
    assert spec.requires_public_url is True


@pytest.mark.parametrize("type_name", sorted(CONDITIONAL_PUBLIC_URL))
def test_conditional_webhook_channels_flagged(type_name: str):
    spec = get_channel_setup_spec(type_name)
    assert spec.transport in {"mixed", "webhook"}


def test_base_channel_specs_do_not_advertise_legacy_extras():
    for type_name in ("feishu", "telegram", "dingtalk", "wecom", "qq"):
        spec = get_channel_setup_spec(type_name)
        assert spec.dependency_extra is None


def test_matrix_advertises_its_real_optional_extra():
    spec = get_channel_setup_spec("matrix")
    assert spec.dependency_extra == "matrix"


def test_channel_catalog_payload_only_advertises_real_install_extras():
    payload = {entry["type"]: entry for entry in channel_catalog_payload()}
    for type_name in ("feishu", "telegram", "dingtalk", "wecom", "qq"):
        assert payload[type_name]["dependencyExtra"] is None
    assert payload["matrix"]["dependencyExtra"] == "matrix"


def test_unknown_channel_raises():
    with pytest.raises(KeyError):
        get_channel_setup_spec("not-a-channel")


def test_msteams_is_hidden_from_catalog():
    """msteams must not be advertised via the onboarding catalog."""
    types = {s.type for s in list_channel_setup_specs()}
    assert "msteams" not in types
    with pytest.raises(KeyError):
        get_channel_setup_spec("msteams")


def test_payload_redacts_secret_defaults():
    payload = channel_catalog_payload()
    for entry in payload:
        for f in entry["fields"]:
            if f.get("secret"):
                assert f["default"] in (None, "", False)


def test_catalog_is_sorted():
    types = [s.type for s in list_channel_setup_specs()]
    assert types == sorted(types)


def test_returns_setup_spec_instance():
    assert isinstance(get_channel_setup_spec("slack"), ChannelSetupSpec)


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_spec_field_defaults_match_pydantic_defaults(type_name: str):
    """Catalog defaults must mirror the gateway pydantic defaults.

    A spec default that diverges from the model default (as wecom's
    connection_mode once did) advertises a minimal setup that the headless
    path then validates in a *different* mode, guaranteeing failure.
    """
    from pydantic_core import PydanticUndefined

    spec = get_channel_setup_spec(type_name)
    model = ENTRY_MODELS[type_name]
    for field in spec.fields:
        finfo = model.model_fields.get(field.name)
        if finfo is None or finfo.default is PydanticUndefined or field.default is None:
            continue
        assert field.default == finfo.default, (
            f"{type_name}.{field.name}: spec default {field.default!r} "
            f"diverges from pydantic default {finfo.default!r}"
        )


_DUMMY_FIELD_VALUES = {
    "text": "dummy-value",
    "password": "dummy-secret",
    "int": 1,
    "float": 1.0,
    "bool": True,
}


@pytest.mark.parametrize("type_name", sorted(ALL_TYPES))
def test_advertised_minimal_setup_passes_model_validation(type_name: str):
    """The catalog's minimal recipe must satisfy the pydantic validators.

    Mirrors the headless path: fill exactly the required fields that are
    visible under the spec defaults (what the catalog Try command asks for),
    leave everything else to model defaults, and expect a valid entry.
    """
    spec = get_channel_setup_spec(type_name)
    defaults = {field.name: field.default for field in spec.fields}
    entry: dict[str, object] = {"name": "test-entry"}
    for field in spec.fields:
        if not field.required or field.name == "name":
            continue
        if field.show_when and any(
            str(defaults.get(key, "")) != expected for key, expected in field.show_when.items()
        ):
            continue
        if field.field_type == "select":
            entry[field.name] = field.default
        else:
            entry[field.name] = _DUMMY_FIELD_VALUES[field.field_type]

    model = ENTRY_MODELS[type_name]
    validated = model(**entry)
    assert validated.name == "test-entry"


def test_can_probe_reflects_adapter_probe_support():
    catalog = {c["type"]: c for c in channel_catalog_payload()}
    # Adapters without a non-mutating probe_connection are honestly flagged.
    assert catalog["matrix"]["canProbe"] is False
    assert catalog["qq"]["canProbe"] is False
    # Adapters that implement probe_connection stay probeable.
    assert catalog["slack"]["canProbe"] is True
    assert catalog["telegram"]["canProbe"] is True
