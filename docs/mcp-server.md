# MCP Server Bridge

OpenSquilla can run as a stdio MCP server bridge for MCP-capable clients. Use
this when another local AI client should call into OpenSquilla session
workflows through the Model Context Protocol.

The MCP bridge is an integration surface. It is separate from OpenSquilla's Web
UI, CLI, channels, and gateway control console.

## Requirements

The SDK 2.x implementation described here is included in the base installation
of the current source. The published
[v0.5.4 wheel](https://github.com/TokenRhythm/opensquilla/releases/download/v0.5.4/opensquilla-0.5.4-py3-none-any.whl)
predates this migration; upgrading only its SDK does not migrate OpenSquilla.
Use [Install from source](../README.md#install-from-source) for this implementation.
From the prepared checkout, install the core profile on macOS/Linux:

```sh
bash scripts/install_source.sh --profile core
```

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1 -Profile core
```

The recommended profile includes MCP as well. In this source version, existing
installation commands that include the `mcp` extra remain accepted; the extra
is a compatibility alias and adds no dependencies.

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

If the command reports that MCP dependencies are missing, repair or reinstall
OpenSquilla with its dependencies using the source installation steps above.

Read next:

- [`configuration.md`](configuration.md)
- [`tools-and-sandbox.md`](tools-and-sandbox.md)
- [`operations.md`](operations.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/TokenRhythm/opensquilla/issues/new?template=docs_report.yml)
