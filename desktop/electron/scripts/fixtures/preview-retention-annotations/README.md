# Real preview and annotation acceptance

`seed.py` creates two synthetic chats and eight HTML documents in a **new**
profile through the real session, document adoption, and workspace-preview APIs.
It makes no model calls.

After installing Desktop/WebUI dependencies and building both applications, run
from the repository root:

```powershell
node desktop/electron/scripts/test-preview-annotation-ui.mjs --python <prepared-venv-python> --output .tmp/preview-annotation-acceptance --hold
```

The Python environment must already contain the project's dependencies. The
runner uses `UV_NO_SYNC=1` and disables bytecode writes. The output directory must
not exist. Desktop starts and owns its Gateway through the normal trusted launch
path, with isolated credentials, configuration, user data, and workspaces.

The test clicks rendered WebUI links, tabs, chat entries, and annotation controls.
It sends native mouse/keyboard input to the actual preview and annotation editor;
it does not inject store state or fabricate selection/IPC responses. It checks:

- Eight retained previews across two chats.
- Continuous annotation, chat/tab changes, and five repeated round trips.
- Five rapid Source/Preview round trips followed by another annotation.
- Explicit stop, ordinary HTML interactions, and form/JavaScript state retention.
- Re-enabling selection without reloading the current document.
- Actual same-document navigation while selection is enabled.
- Unsubmitted draft recovery to its original target after chat/tab changes.

`report.json` contains the assertions and native page identities. PNG files record
the rendered WebUI, native previews, and annotation editors before and after text
entry. `ready.json` identifies the isolated app. With `--hold`, write any text to
`<output>/stop` to close the app and its owned Gateway gracefully.
