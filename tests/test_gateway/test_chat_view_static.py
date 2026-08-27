"""Focused Vue chat wiring contracts retained after the vanilla UI removal."""

from pathlib import Path

CHAT_VIEW = Path("opensquilla-webui/src/views/ChatView.vue")
CHAT_SEND = Path("opensquilla-webui/src/composables/chat/useChatSend.ts")
CHAT_MESSAGE_ACTIONS = Path("opensquilla-webui/src/composables/chat/useChatMessageActions.ts")
ROUTER_FX = Path("opensquilla-webui/src/components/chat/RouterFxStrip.vue")


def test_router_fx_cells_expose_only_presentation_names() -> None:
    source = ROUTER_FX.read_text(encoding="utf-8")

    assert '<span class="nm-base">{{ cell.visualName }}</span>' in source
    assert '<span class="nm-win" aria-hidden="true">{{ cell.visualName }}</span>' in source
    assert "visualName: cell.executionKind === 'ensemble'" in source
    assert "? t('chat.routerFx.ensembleModel')" in source
    assert ":data-kind" not in source
    assert ":data-tiers" not in source


def test_chat_view_wires_middle_edit_branch_fork_id() -> None:
    view = CHAT_VIEW.read_text(encoding="utf-8")
    send = CHAT_SEND.read_text(encoding="utf-8")
    actions = CHAT_MESSAGE_ACTIONS.read_text(encoding="utf-8")

    assert "const pendingForkBeforeMessageId = ref<string | null>(null)" in view

    actions_start = view.index("const chatMessageActions = useChatMessageActions({")
    actions_end = view.index("})\nconst {", actions_start)
    assert "pendingForkBeforeMessageId," in view[actions_start:actions_end]

    send_start = view.index("const chatSend = useChatSend({")
    send_end = view.index("\n})", send_start)
    assert "pendingForkBeforeMessageId," in view[send_start:send_end]

    assert "watch(sessionKey, () => {\n  pendingForkBeforeMessageId.value = null" in view

    assert "pendingForkBeforeMessageId: Ref<string | null>" in send
    assert "params.forkBeforeMessageId = forkBeforeMessageId" in send
    assert "options.pendingForkBeforeMessageId.value = null" in send
    regenerate_start = actions.index("function regenerateMessage(")
    regenerate_end = actions.index("function editMessage(", regenerate_start)
    regenerate_body = actions[regenerate_start:regenerate_end]
    assert "const forkBeforeMessageId = userMessage?.messageId || ''" in regenerate_body
    assert "if (!forkBeforeMessageId)" in regenerate_body
    assert "options.pendingForkBeforeMessageId.value = forkBeforeMessageId" in regenerate_body
    assert regenerate_body.index("if (!forkBeforeMessageId)") < regenerate_body.index(
        "options.messages.value = options.messages.value.slice(0, userMsgIndex)"
    )

    edit_start = actions.index("function editMessage(")
    edit_body = actions[edit_start:]
    assert "const forkBeforeMessageId = sourceMessage?.messageId || ''" in edit_body
    assert "if (!forkBeforeMessageId)" in edit_body
    assert "options.pendingForkBeforeMessageId.value = forkBeforeMessageId" in edit_body
    assert edit_body.index("if (!forkBeforeMessageId)") < edit_body.index(
        "options.messages.value = options.messages.value.slice(0, msgIndex)"
    )
