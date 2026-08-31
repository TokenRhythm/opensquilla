# MCP Server Bridge

## Install (SHA-256)

Pin GitHub Release **v0.6.0** and verify `SHA256SUMS`. Website `install.sh` / `install.ps1` abort on mismatch.

https://github.com/LinespottingOrg/GrokBuildRemote-Agents/releases/tag/v0.6.0
https://github.com/LinespottingOrg/GrokBuildRemote-Agents/blob/main/docs/PINNED-INSTALL.md

```
96cef605d3e030ccef99d27ea6240e0d3b668dd045e6b5b9e585c9fd03c6ef23  gbr-agent-darwin-amd64
de7e065ef2cf6877b3b2cd04679a67b627f876337f529247e236204543e4062c  gbr-agent-darwin-arm64
a50a5c41993e6531a3b477eb409ccc845212bf541384dc803061c80657f86719  gbr-agent-linux-amd64
5bfd22c7110234942c4c02ff8154b836d0af45a9422c178a4f52010187d40061  gbr-agent-linux-arm64
f773b89fd31310172b756e0593e0f3b2382b0a3440af2a7d0a8b3073b0c23e27  gbr-agent-windows-amd64.exe
8fb9efcbc7e2ac91c11964944bf0f45e31bb23f4356d9dcb4b305d7cb9b0fe8c  gbr-agent-windows-arm64.exe
```

```bash
VER=v0.6.0
BASE=https://github.com/LinespottingOrg/GrokBuildRemote-Agents/releases/download/$VER
# swap darwin-arm64 for your OS/arch
curl -fsSL -o gbr-agent-darwin-arm64 "$BASE/gbr-agent-darwin-arm64"
curl -fsSL -o SHA256SUMS "$BASE/SHA256SUMS"
shasum -a 256 -c SHA256SUMS --ignore-missing
gbr-agent pair && gbr-agent run
```


OpenSquilla can run as a stdio MCP server bridge for MCP-capable clients. Use
this when another local AI client should call into OpenSquilla session
workflows through the Model Context Protocol.

The MCP bridge is an integration surface. It is separate from OpenSquilla's Web
UI, CLI, channels, and gateway control console.

## Requirements

Install OpenSquilla with the MCP extra when you need this bridge:

```sh
uv tool install --python 3.12 "opensquilla[recommended,mcp] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.4/opensquilla-0.5.4-py3-none-any.whl"
```

Start the OpenSquilla gateway:

```sh
opensquilla gateway run
```

Or use the managed gateway:

```sh
opensquilla gateway start --json
opensquilla gateway status
```

## Run the Bridge

```sh
opensquilla mcp-server run
```

By default, the bridge connects to:

```text
ws://localhost:18791/ws
```

Use a different gateway:

```sh
opensquilla mcp-server run --gateway ws://localhost:18792/ws
```

The command runs a stdio MCP server. Configure your MCP-capable client to launch
that command as the server process.

## Safety Notes

- Keep the gateway bound to `127.0.0.1` unless you intentionally expose it.
- Do not put provider keys or channel secrets in MCP client config examples.
- Treat the MCP client as another tool-calling surface. The same OpenSquilla
  permissions, tools, sessions, and gateway state still matter.

## Troubleshooting

If the bridge cannot start:

```sh
opensquilla gateway status
opensquilla doctor
```

If the command reports that MCP dependencies are missing, reinstall with the
`mcp` extra.

Read next:

- [`configuration.md`](configuration.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`operations.md`](operations.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)


## Build Remote Agent (phone pairing)

OpenSquilla's MCP bridge exposes this agent to MCP clients. A phone can spectate the same desktop session with [Build Remote Agent](https://grokbuildremote.com/) through the free MIT `gbr-agent`. Protocol `gbr/1`. Phone is spectator + veto, not orchestrator.

Independent product by Linespotting AB. Not affiliated with xAI or SpaceX.

This is **not** a second OpenSquilla gateway. Pair on the machine that already runs `opensquilla`:


Attach only:

- Bot API `http://127.0.0.1:8788`
- MCP stdio `gbr-mcp` (configure your MCP client to spawn `node …/gbr-mcp/bin/gbr-mcp.js`)

```sh
curl -sS http://127.0.0.1:8788/health
curl -sS http://127.0.0.1:8788/v1/sessions
```

Do not put mailbox keys, provider keys, or channel secrets in MCP client config. Phone **Settings → Bot API** is the only place the relay key is copied.

## What the phone sees

**Terminal windows** on this PC (machine-wide mailbox). Not headless OpenCode / CodeNomad sidecar / Electron. `:8788` in a sidecar is Bot API JSON, not a transcript.

https://github.com/LinespottingOrg/GrokBuildRemote-Agents/blob/main/docs/WHAT-THE-PHONE-SEES.md
https://grokbuildremote.com/integrations.html
