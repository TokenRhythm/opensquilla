# Docker Deployment

Run the OpenSquilla gateway as a container on a machine that stays on — a home
server, a NAS, or a small VPS. This page uses Debian 12 as the worked example,
but any host with Docker Engine works the same way, on both `amd64` and `arm64`.

Docker is the right install path when:

- the host has no Python 3.12+ toolchain (or you do not want one),
- you want the gateway to survive reboots and upgrade by pulling a new image,
- you deploy on a NAS or headless server and use the Web UI from another device.

For a desktop machine, the [quickstart](quickstart.md) installs are simpler.

## Prerequisites

Install Docker Engine with the Compose plugin. On Debian 12, follow the
[official Docker instructions](https://docs.docker.com/engine/install/debian/),
then verify:

```sh
docker --version
docker compose version
```

Nothing else is required on the host for the prebuilt-image path — no
Python, no Git, no build tools.

## Quick Start with the Prebuilt Image

Prebuilt multi-arch images are published to
[`ghcr.io/opensquilla/opensquilla`](https://github.com/opensquilla/opensquilla/pkgs/container/opensquilla)
for each release tag. The immutable `v0.5.0` tag identifies the 0.5.0 stable release, while
`latest` follows the most recently pushed release tag, including previews and
backports. If a backport moves `latest`, the newest release workflow is rerun to
restore the intended ordering. If the release you want predates image
publishing, use
[Build the Image Yourself](#build-the-image-yourself) instead. A pull that
fails with `denied` or `manifest unknown` means the image for that tag has
not been published (or the package is not public yet) — check the package
page for available tags, or build from source.

Create a directory for the deployment and write this `compose.yaml`:

```yaml
services:
  gateway:
    # Pin v0.5.0 for reproducibility; latest follows the most recent tag push.
    image: ghcr.io/opensquilla/opensquilla:latest
    environment:
      # In-container bind. Keep it 0.0.0.0 — what the network can reach is
      # decided by `ports` below, not by this value.
      OPENSQUILLA_LISTEN: "0.0.0.0"
      # Token auth is required to administer a containerized gateway through
      # the Web UI, even from the same host. Generate a token with:
      #   openssl rand -hex 32
      OPENSQUILLA_AUTH_MODE: token
      OPENSQUILLA_AUTH_TOKEN: ${OPENSQUILLA_AUTH_TOKEN:?generate one with openssl rand -hex 32}
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-}
      TZ: ${TZ:-UTC}
    volumes:
      # All state — config, session DBs, memory, logs, workspace — lives under
      # /var/lib/opensquilla. The named volume makes it survive recreates.
      - opensquilla-state:/var/lib/opensquilla
    ports:
      # Loopback-only: reachable from this host, invisible to the network.
      # For NAS/LAN access see "Reach the Web UI from Your LAN" below.
      - "127.0.0.1:18791:18791"
    restart: unless-stopped

volumes:
  opensquilla-state:
```

Put the two secrets in a `.env` file next to `compose.yaml` (Compose reads it
automatically; keep it out of version control and private: `chmod 600 .env`):

```sh
OPENSQUILLA_AUTH_TOKEN=<output of: openssl rand -hex 32>
OPENROUTER_API_KEY=<your provider key>
```

Start it:

```sh
docker compose up -d
docker compose logs -f gateway
```

Then open the Web UI with the token in the URL:

```text
http://127.0.0.1:18791/control/?token=<your OPENSQUILLA_AUTH_TOKEN>
```

The token is consumed once and stored for the browser session. The first
request also writes the token into the gateway access log, so treat
`docker compose logs` output as sensitive — or open `/control/` without the
query parameter and paste the token into the connection panel instead. From
there, finish provider onboarding and configuration in the Web UI — provider
changes apply immediately and persist in the state volume.

Why token auth is not optional here: the container binds a wildcard address,
so the gateway treats every browser — including one on the same host — as a
remote operator. Remote operators without a token can chat but cannot
administer configuration or onboarding (only a small allowlist of safe
runtime toggles stays writable). With `OPENSQUILLA_AUTH_MODE=token` the token
grants the operator scopes that Web UI administration needs. Use `token` mode
specifically; `password` and `trusted-proxy` modes do not support the Web UI
connection.

## Reach the Web UI from Your LAN

On a headless NAS you will use the Web UI from another device. Two rules:

1. Publish the port on all interfaces by changing the `ports` entry — do
   **not** change `OPENSQUILLA_LISTEN`:

   ```yaml
   ports:
     - "18791:18791"
   ```

2. Keep token auth configured (already true if you followed the quick start).
   The gateway warns, but does not refuse, when it is network-reachable —
   exposure is your call, auth is not.

Recreate the container (`docker compose up -d`) and open
`http://<server-address>:18791/control/?token=<token>` from your device.
If the host runs a firewall, allow inbound TCP 18791 from your LAN only.
LAN traffic to the gateway is plain HTTP, so the token is visible to anyone
who can observe that network — if your LAN is not fully trusted, put the
gateway behind a TLS reverse proxy or use the VPN option below.

Do not forward the gateway port to the internet. For remote access away from
home, use a VPN (WireGuard, Tailscale) or a reverse proxy with TLS and its own
authentication in front. See the safety defaults in [`gateway.md`](gateway.md).

## Keep State on Your Own Storage (Bind Mount)

The named volume is the safest default. If you prefer a directory you manage
(RAID storage, backup tooling), bind-mount it — but the container runs as
non-root UID 10001, so give it ownership first or the gateway fails at boot:

```sh
sudo mkdir -p /srv/opensquilla
sudo chown -R 10001:10001 /srv/opensquilla
```

```yaml
volumes:
  - /srv/opensquilla:/var/lib/opensquilla
```

Everything worth backing up is under that one directory: `config.toml`,
`state/` (session and scheduler databases), `logs/`, `workspace/`, `media/`,
and an optional `.env`.

## Configure Providers and Secrets

Three ways, in order of preference:

1. **Web UI** — provider onboarding and most config changes at `/control/`
   hot-apply and persist to `config.toml` in the state volume. Channel,
   memory-embedding, and sandbox-posture changes need a restart — the Web UI
   marks these, and `docker compose restart gateway` applies them.
2. **Compose `environment`** — pass provider keys by env-var name, as in the
   quick start. Environment values always win over `.env` files.
3. **A `.env` inside the state volume** — the gateway loads
   `/var/lib/opensquilla/.env` at startup, so keys survive image upgrades
   without appearing in `compose.yaml`. On a bind mount, keep it owned by the
   container user and private: `chown 10001:10001 .env && chmod 600 .env`.
   Caveat: a key listed under `environment:` in `compose.yaml` shadows the
   state-volume `.env` even when the host variable is unset (Compose passes
   an empty value through) — remove it from `environment:` if you manage it
   in the state volume.

One precedence caveat for auth: values saved to `config.toml` — for example
by the Web UI — take precedence over environment variables at boot. If the
`OPENSQUILLA_AUTH_*` variables stop taking effect after configuring through
the Web UI, `config.toml` now owns the `[auth]` settings; rotate the token
there (or in the Web UI) and restart.

Hand-edits to `/var/lib/opensquilla/config.toml` are read at boot only —
restart to apply them:

```sh
docker compose restart gateway
```

## Change the Published Port

Change the host side of the mapping and keep the container side at 18791:

```yaml
ports:
  - "127.0.0.1:8080:18791"
```

Setting `OPENSQUILLA_GATEWAY_PORT` does **not** change the listen port of the
container entrypoint — the port lives in the mapping above.

## Health and CLI Access

`/healthz` answers liveness without auth, and `/readyz` returns 503 until the
gateway is fully ready. The image ships a healthcheck; inspect it with:

```sh
docker inspect --format '{{.State.Health.Status}}' $(docker compose ps -q gateway)
```

The full CLI is available inside the container:

```sh
docker compose exec gateway opensquilla doctor
docker compose exec gateway opensquilla gateway status
```

## Upgrade and Roll Back

State lives in the volume, so containers are disposable:

```sh
docker compose pull
docker compose up -d
```

To roll back, pin the previous release tag in `image:` and `docker compose up
-d` again. Pinned tags plus a state backup make both directions routine.

## Build the Image Yourself

The source checkout ships the same `Dockerfile` and a `compose.yaml` that
defaults to a self-built `opensquilla:local` image (override with
`OPENSQUILLA_GATEWAY_IMAGE` to use the GHCR image instead). Building needs
`git`, `git-lfs`, and the Git LFS router assets:

```sh
sudo apt install -y git git-lfs
git clone https://github.com/opensquilla/opensquilla.git
cd opensquilla
git lfs pull --include="src/opensquilla/squilla_router/models/**"
docker build -t opensquilla:local .
docker compose up -d
```

The Dockerfile builds the Vue console in a pinned Node.js build stage, so the
host does not need Node.js or npm. The first source image build does download
frontend packages and uses additional build time, network transfer, and cache
space. Published images already contain the console and have none of that
client-side build cost.

On a low-power or `arm64` NAS this build is slow; prefer the prebuilt image
and keep source builds for development machines.

## If Something Fails

- `docker compose logs gateway` shows boot errors, including an unwritable
  state directory (fix ownership as above).
- `docker compose exec gateway opensquilla doctor` reports readiness and
  recovery steps.
- The Docker sections in [`troubleshooting.md`](troubleshooting.md) cover the
  common failures: unreachable Web UI, rejected configuration changes,
  bind-mount permissions, and LFS-related build errors.

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
