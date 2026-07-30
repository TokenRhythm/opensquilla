# Gateway

The OpenSquilla gateway is the local server behind the Web UI, channels, RPC
clients, sessions, approvals, diagnostics, and usage views. Most day-to-day
OpenSquilla surfaces work best when the gateway is running.

Use this page when you want to start, stop, inspect, expose, or troubleshoot
the gateway.

## Foreground Gateway

Run the gateway in the current terminal:

```sh
opensquilla gateway run
```

Open the control console:

```text
http://127.0.0.1:18791/control/
```

Stop a foreground gateway with `Ctrl+C`.

## Managed Background Gateway

Start a managed background process and wait for readiness:

```sh
opensquilla gateway start --json
```

Inspect it:

```sh
opensquilla gateway status
opensquilla gateway status --json
```

Restart or stop it:

```sh
opensquilla gateway restart
opensquilla gateway stop
```

Stop and restart shut down gracefully: in-flight agent turns and background
completions are drained before the process exits, and the force-kill deadline
exceeds that drain budget so work is not cut off mid-write. Tune the per-phase
drain budget with `OPENSQUILLA_GATEWAY_GRACEFUL_TIMEOUT` (seconds; default 30,
bounded). The same drain runs on `Ctrl+C` / `SIGTERM` for a foreground gateway.
On Windows — which has no real `SIGTERM` — the desktop app and `gateway stop`
trigger the drain through an owner-only, loopback `POST /api/system/shutdown`.

Use the managed gateway for the Web UI, channels, scheduled jobs, and local
automation that should survive the current terminal tab.

## Host and Port

Use a different port:

```sh
opensquilla gateway run --port 18792
opensquilla gateway status --port 18792
```

Bind to a specific host:

```sh
opensquilla gateway run --listen 127.0.0.1 --port 18791
```

`--listen` is an alias for the bind host and wins over `--bind` when both are
provided.

## Safety Defaults

The gateway defaults to loopback scope, usually `127.0.0.1`, because the local
gateway controls chat, tools, sessions, channels, approvals, and configuration.

Public binding is opt-in:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

Do not expose a gateway to an untrusted network without token auth and a network
boundary you understand.

When a wildcard-bound gateway is reached through a custom DNS name or reverse
proxy, list the exact browser origin in `cors.allowed_origins` (for example,
`https://control.example`). IP-literal and localhost authorities work without
that entry. OpenSquilla intentionally does not trust arbitrary `Host` values on
a wildcard listener, because doing so would permit DNS rebinding; `"*"` is not
accepted for this purpose.

## Configuration Path

Use a specific config file:

```sh
opensquilla gateway run --config /path/to/opensquilla.toml
opensquilla gateway status --config /path/to/opensquilla.toml
```

OpenSquilla also reads standard configuration locations described in
[`configuration.md`](configuration.md).

## Remote Status Check

Inspect a gateway URL directly:

```sh
opensquilla gateway status --gateway ws://localhost:18791/ws
```

This is useful when a client or MCP bridge is configured with an explicit
gateway URL.

## When to Restart

Restart the gateway after changing:

- provider or router configuration;
- channel configuration;
- durable agent entries;
- global sandbox posture;
- search or image-generation setup;
- environment variables used by configured providers.

```sh
opensquilla gateway restart
```

## Troubleshooting

Check status and readiness:

```sh
opensquilla gateway status
opensquilla doctor
```

If the port is busy:

```sh
opensquilla gateway run --port 18792
```

If the Web UI cannot connect, confirm that the URL matches the gateway bind
host and port.

Read next:

- [`web-ui.md`](web-ui.md)
- [`configuration.md`](configuration.md)
- [`channels.md`](channels.md)
- [`troubleshooting.md`](troubleshooting.md)

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
