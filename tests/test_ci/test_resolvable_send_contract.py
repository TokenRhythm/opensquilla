"""The versioned fence must not silently drop ordinary chat features."""

import json
from pathlib import Path


def test_resolvable_send_keeps_the_shared_chat_payload_contract() -> None:
    contracts = Path(__file__).resolve().parents[2] / "contracts/gateway/v4/conversation"
    legacy = json.loads((contracts / "chat-send.schema.json").read_text(encoding="utf-8"))
    resolvable = json.loads(
        (contracts / "chat-send-v2.schema.json").read_text(encoding="utf-8")
    )
    definitions = json.loads(
        json.dumps(resolvable["$defs"])
        .replace("ChatSendV2", "ChatSend")
        .replace("chat.send.v2", "chat.send")
    )
    params = definitions["ChatSendParams"]
    assert params["required"] == ["message", "clientRequestId"]
    assert params["properties"]["clientRequestId"] == {
        "type": "string", "minLength": 1, "maxLength": 256,
    }
    params["required"] = legacy["$defs"]["ChatSendParams"]["required"]
    params["properties"]["clientRequestId"] = (
        legacy["$defs"]["ChatSendParams"]["properties"]["clientRequestId"]
    )
    assert params["properties"].pop("acceptanceEpoch")["type"] == "string"
    # Both routes share the admission implementation; attachments, page context,
    # commands, skills and responses must evolve together. Identity is stricter
    # and the optional process epoch is transport metadata, not message content.
    assert definitions == legacy["$defs"]
