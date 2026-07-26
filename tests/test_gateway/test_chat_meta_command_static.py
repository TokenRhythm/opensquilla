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
    marker = "case 'meta.menu':"
    assert marker in text, "missing meta.menu case in selectSlashCmd"
    body = text[text.index(marker):]
    assert "meta.run" in body, "running a chosen meta-skill must call the meta.run RPC"
    assert "sessionKey" in body, "meta.run must pass the session key"
    assert "dispatchHidden" in body, "running a meta-skill must trigger a turn via dispatchHidden"
