"""Static contract for the web ``/meta`` slash-command wiring.

The served web UI is the Vite SPA under ``opensquilla-webui/``; its slash
dispatch lives in ``composables/chat/useChatSlashCommands.ts``. JS/TS is not
unit-tested with a JS runner here, so we lock the SPA source text:

- meta-skills are offered as Tab-completable **argument candidates** in the
  slash menu (not a toast), via the command's ``argumentChoices``;
- selecting ``/meta <skill>`` runs it through ``meta.run`` + a hidden turn.

The contract intentionally targets the maintained Vue source rather than a
generated browser bundle.
"""

from pathlib import Path

SPA_SLASH = Path("opensquilla-webui/src/composables/chat/useChatSlashCommands.ts")


def _read() -> str:
    return SPA_SLASH.read_text(encoding="utf-8")


def test_slash_menu_supports_argument_completion() -> None:
    text = _read()
    # The menu offers a command's argument choices as selectable candidates.
    assert "argumentChoices" in text, "slash menu must read per-command argumentChoices"
    assert "makeArgCandidate" in text, "argument choices must become selectable menu candidates"
    assert "argValue" in text, "selecting an argument candidate must complete it into the composer"


def test_meta_run_path_uses_meta_run_rpc() -> None:
    text = _read()
    case_marker = "case 'meta.menu':"
    helper_marker = "async function runMetaInvocation("
    helper_end = "async function restoreDurableMetaDrafts("
    assert case_marker in text, "missing meta.menu case in selectSlashCmd"
    assert helper_marker in text, "missing shared MetaSkill invocation helper"
    case_body = text[text.index(case_marker):]
    helper_body = text[text.index(helper_marker):text.index(helper_end)]
    assert "runMetaInvocation" in case_body, "meta.menu must use the durable invocation path"
    assert "meta.run" in helper_body, "running a chosen meta-skill must call the meta.run RPC"
    assert "sessionKey" in helper_body, "meta.run must pass the session key"
    assert "dispatchHidden" in helper_body, "running a meta-skill must trigger a hidden turn"
    assert "setup_required" in helper_body, "setup failures must not start a turn"
    assert "readiness.missing_bins" in helper_body, "setup must identify missing binaries"
