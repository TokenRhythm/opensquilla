"""Focused Vue chat wiring contracts retained after the vanilla UI removal."""

import re
from pathlib import Path

CHAT_VIEW = Path("opensquilla-webui/src/views/ChatView.vue")
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

    assert "const pendingForkBeforeMessageId = ref<string | null>(null)" in view

    actions_start = view.index("const chatMessageActions = useChatMessageActions({")
    actions_end = view.index("})\nconst {", actions_start)
    assert "pendingForkBeforeMessageId," in view[actions_start:actions_end]

    send_start = view.index("const chatSend = useChatSend({")
    send_end = view.index("\n})", send_start)
    assert "pendingForkBeforeMessageId," in view[send_start:send_end]

    assert re.search(
        r"watch\(sessionKey, \(\) => \{[^}]*\n  pendingForkBeforeMessageId\.value = null",
        view,
    )
