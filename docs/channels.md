# Channels

Channels let OpenSquilla run from messaging platforms while sharing the same
agent runtime as the CLI and Web UI. Use channels when you want the same agent
to answer from Slack, Telegram, Feishu/Lark, Discord, DingTalk, WeCom, Matrix,
QQ, or another supported adapter.

## Available Channel Types

Inspect your local install:

```sh
opensquilla channels types
opensquilla channels types --json
opensquilla channels describe feishu
```

This build exposes the following channel families. All public vendor adapters
are currently `YELLOW-experimental`; availability does not imply feature parity
with the provider's complete API.

| Type | Label | Transport | Public URL needed | Maturity |
| --- | --- | --- | :---: | --- |
| `dingtalk` | DingTalk | websocket | no | experimental |
| `discord` | Discord | gateway websocket | no | experimental |
| `feishu` | Feishu / Lark | mixed | depends on mode | experimental |
| `matrix` | Matrix | HTTP sync | no | experimental |
| `qq` | QQ Bot | gateway websocket | no | experimental |
| `slack` | Slack | mixed | depends on mode | experimental |
| `telegram` | Telegram | polling or webhook | depends on mode | experimental |
| `wecom` | WeCom | webhook or AI Bot websocket | depends on mode | experimental |

The local `channels describe <type>` output is the source of truth for required
fields, secrets, extras, and restart behavior.

## Setup Flow

Interactive setup:

```sh
opensquilla configure channels
```

Add a channel explicitly:

```sh
opensquilla channels add telegram --name personal
```

Add provider-specific fields as needed. Slack supports two modes:

```sh
# Slack Socket Mode: outbound websocket, no public URL.
opensquilla channels add slack --name team \
  --field connection_mode=socket \
  --field app_token=xapp-... \
  --token xoxb-...

# Slack Events API webhook: requires a public Request URL and signing secret.
opensquilla channels add slack --name team-webhook \
  --field connection_mode=webhook \
  --field signing_secret=... \
  --token xoxb-...
```

Restart the gateway process after config edits:

```sh
opensquilla gateway restart
```

Verify runtime connection:

```sh
opensquilla channels status
opensquilla channels status personal --json
```

Saving a channel proves the config was written. `channels status` proves whether
the running gateway loaded and connected it; status does not make an outbound
provider request.

## Runtime Safety and Delivery Contract

The channel runtime uses one shared boundary for provider identity, access
policy, durability, and delivery diagnostics. These guarantees apply to the
implemented runtime, not to provider features that the adapter does not expose.

### Authenticated Admission

Normal provider ingress records immutable provenance: provider, account,
transport, verification method, native event ID, and authenticated principal.
Webhook signatures or tokens are checked by the adapter; SDK and gateway
sessions are marked as such. Legacy or directly constructed messages remain
explicitly `legacy_unverified` and do not silently gain trusted status.

The dispatcher makes one admission decision before session creation, command
handling, approval resolution, attachment download, or transcript mutation. It
rejects an authenticated-principal/sender mismatch, applies DM/group and
allowlist policy, and defaults group mention checks to deny when an adapter has
no mention hook. This is the security boundary; provider metadata is not an
authorization decision by itself.

Authenticated direct messages default to `dm_access = "pairing"`. The first
unknown sender receives a fixed notice with a short request code; OpenSquilla
stores the provider identity and access state, never the rejected message
content. Operators can approve or revoke that identity from the channel's
**Pairings** tab. `dm_access = "open"` deliberately restores the earlier public
DM behavior, while `dm_access = "allowlist"` requires `allowed_senders`.

Group conversations default to `group_session_scope = "per_sender"`, so two
people in one room do not share transcript context. Use `shared_room` only when
the room is intentionally collaborative. `busy_input_mode` controls messages
arriving during a running turn: `followup`, `queue`, `steer`, or `interrupt`.
The runtime validates the selected mode and preserves its bounded pending-queue
policy.

### Durable Ingress and Outbound Intent

Managed channels use `channel_delivery.sqlite` in the OpenSquilla state
directory. The database uses SQLite WAL mode, full synchronous commits, and
owner-only file permissions where the platform supports them.

- Normalized inbound events with a native event ID are committed before they
  enter the dispatcher queue. Processing is claimed atomically, interrupted
  claims return to `accepted` on restart, and terminal dispositions are kept
  for diagnostics. Events without a stable provider ID cannot receive this
  durable deduplication guarantee.
- HTTP webhook handlers that synchronously enqueue an event commit it before
  returning success. SDK and socket protocols may require an earlier protocol
  ACK; their provider-side replay behavior remains transport-specific.
- Declared outbound mutations first persist a delivery ID, target, content
  hash, and sanitized intent. This covers ordinary messages plus supported
  file uploads, edits, deletes, reactions, and streaming operations; cards sent
  through the ordinary message envelope use that same path. Confirmed results
  store provider message/file IDs. Legacy adapters that return no receipt are
  `sent_unconfirmed`; an exception is `unknown`, because retrying a possibly
  delivered request could duplicate it.
- The outbox is an intent and receipt ledger, not a blind automatic resend
  worker. Provider-specific admin/content APIs outside the public channel
  mutation surface keep their own operation contracts.

Delivery counts, oldest pending records, unknown outcomes, and lease state are
included in channel status diagnostics.

### One Active Transport per Account

Before starting an adapter, the manager acquires a renewable lease for the
provider family and derived account identity in the same state database. The
lease carries a monotonically increasing fencing token, is renewed while the
adapter runs, and is exposed in health diagnostics. A second local gateway
using the same state database cannot start the same account transport while the
lease is live. Losing renewal marks the adapter disconnected and stops it.

This is a SQLite-backed process/account ownership guard, not a distributed
multi-region lease service.

## Capability Evidence and Live Probes

`channels status` returns three separate views:

- a typed capability profile for low-level operations;
- a provider manifest for chat, files, media, attachments, threads, cards,
  docs, drive, wiki, permissions, and scopes;
- evidence for each declared capability plus the adapter maturity tier.

Method-backed evidence means a callable implementation exists. Declaration
evidence covers semantic behavior such as group topology. Neither is an
end-to-end provider certification: current proof status is `unverified`, which
is why the public adapters remain experimental.

The setup UI can run an explicit admin-scoped live probe. Slack, Telegram,
Discord, Feishu, DingTalk, and WeCom corp-app mode currently implement
credential/network probes. The DingTalk probe requests ephemeral Stream
connection metadata but does not open the websocket or send business data.
Matrix, QQ, and WeCom AI Bot websocket mode report `unsupported` rather than
opening a second ingress connection.
A `verified` probe confirms only the check it ran; it does not prove callback
delivery, every permission scope, outbound delivery, rate-limit behavior, or
feature completeness.

For release certification, use the environment-only CLI runner. It constructs
an ephemeral adapter and never writes the supplied credentials into the channel
configuration or evidence output:

```bash
export OPENSQUILLA_CHANNEL_CERT_FEISHU_APP_ID='...'
export OPENSQUILLA_CHANNEL_CERT_FEISHU_APP_SECRET='...'
export OPENSQUILLA_CHANNEL_CERT_DINGTALK_CLIENT_ID='...'
export OPENSQUILLA_CHANNEL_CERT_DINGTALK_CLIENT_SECRET='...'
export OPENSQUILLA_CHANNEL_CERT_TELEGRAM_TOKEN='...'
export OPENSQUILLA_CHANNEL_CERT_DISCORD_TOKEN='...'

opensquilla channels certify \
  --provider feishu \
  --provider dingtalk \
  --provider telegram \
  --provider discord \
  --json
```

Use newly rotated credentials in a local shell or secret manager; never paste
them into source, command history, an issue, or a test fixture. The default
certification performs credential probes only. A real outbound test is gated by
all three of `--send-test-message`, `--allow-side-effects`, and an explicit
`--target provider=destination`. Targets and credential values are omitted from
the resulting evidence. QQ destinations use `c2c:<openid>` or
`group:<group_openid>`. DingTalk outbound certification is reported as
unsupported until an inbound message supplies its temporary `sessionWebhook`.
Telegram transport startup is deliberately outside the safe probe because
polling/webhook startup changes the bot's webhook state.

## Channel Approvals

When a channel turn reaches a gated tool, OpenSquilla sends a short approval
code instead of exposing the raw approval ID. The durable binding records the
requesting sender, session, channel entry, conversation, and thread. Resolution
must come from the same admitted session/conversation and requester; an
authenticated principal is used when available.

Adapters declaring interactive cards may render buttons. Every adapter has the
plain-text fallback `/approve CODE` and `/deny CODE`. A successful channel
approval releases only that gated action and never enables session-wide
elevated mode.

## Manage Channels

```sh
opensquilla channels list
opensquilla channels enable <name>
opensquilla channels disable <name>
opensquilla channels edit <name>
opensquilla channels restart <name>
opensquilla channels logout <name>
opensquilla channels remove <name>
```

Use `gateway restart` after config changes. Use `channels restart <name>` only
for an already-loaded live adapter.

## Slack Modes

Slack Socket Mode uses an outbound websocket and does not require a public
Request URL. It requires the bot token (`xoxb-...`) plus an app-level token
(`xapp-...`) saved as `app_token`.

Slack webhook mode uses the Events API Request URL. It requires the bot token
plus `signing_secret`, and the gateway must be reachable by Slack.

Leave `slack_channel_id` empty when the adapter should reply to the incoming
conversation. Set it only when you want a default fallback channel. Enable
`reply_in_thread` when replies should stay in Slack threads.

## Webhook Channels

Slack webhook mode and WeCom require a public, provider-reachable URL. Feishu
and Telegram may require one depending on mode.

For public channels:

- bind the gateway to a reachable interface;
- place it behind a trusted reverse proxy or tunnel;
- configure auth;
- check provider callback URLs and secrets carefully.

Example bind for a controlled network:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

Do not expose an unauthenticated gateway to the public internet.

## Provider Boundaries and Official References

The limitations below are intentional release boundaries, not a description of
everything each vendor supports.

| Provider | Current experimental boundary | Primary documentation |
| --- | --- | --- |
| Slack | Events API and Socket Mode messaging subset. Webhook mode fails closed without request signing. Native Slack AI streaming and complete inbound file handling are not yet exposed. | [Request verification][slack-verification], [Socket Mode][slack-socket] |
| Discord | Gateway messaging/reply subset. Full sharding, component/modal workflows, and parity across all message operations remain incomplete. | [Gateway][discord-gateway], [interactions][discord-interactions] |
| Telegram | Polling/webhook messaging and common media subset. Draft streaming, callback keyboards, and reaction workflows are not yet complete. | [Bot API][telegram-api] |
| Feishu / Lark | Webhook or long-connection messaging, attachments, and selected cards. CardKit native streaming and complete event/action normalization remain incomplete. | [Event receipt and encryption][feishu-events] |
| WeCom | Corp-app webhook and AI Bot websocket messaging. AI Bot live probing and websocket media upload are intentionally unsupported; inbound attachment resolution remains incomplete. Untargeted corp-app sends are rejected instead of broadcasting to `@all`. | [Callback encryption][wecom-callback], [app messages][wecom-messages] |
| Matrix | Client-server sync and room messaging subset. Full encrypted-media/device-trust behavior and reaction/thread parity remain incomplete. | [Client-Server API v1.19][matrix-client] |
| QQ | C2C/group text messaging subset. Rich-media, interaction, and complete recall coverage are not yet exposed despite current platform support. | [Message sending][qq-send], [rich media][qq-media] |
| DingTalk | Stream-mode message/reply and selected card streaming. General inbound media and interactive-card action coverage remain incomplete. | [Robot replies and sends][dingtalk-messages], [card interaction][dingtalk-cards] |
| Microsoft Teams | The legacy adapter is hidden and is not a supported public channel. It must migrate from Bot Framework to Teams SDK or Microsoft 365 Agents SDK before promotion. | [SDK comparison][teams-sdk], [Bot Framework migration][teams-migration] |

## Attachments and Artifacts

Channel adapters can differ in attachment and artifact delivery behavior.
OpenSquilla normalizes agent execution through the same runtime path, but the
platform transport still controls file size limits, message threading, and
download/upload capabilities.

When a channel cannot deliver a large artifact directly, use the Web UI artifact
card or session export as the recovery path.

## Troubleshooting

If a channel does not respond:

1. Check config entries:

   ```sh
   opensquilla channels list
   ```

2. Check runtime status:

   ```sh
   opensquilla channels status <name> --json
   ```

3. Restart the gateway process after config changes:

   ```sh
   opensquilla gateway restart
   ```

4. For webhook channels, confirm the public URL, provider callback secret, and
   gateway auth/network boundary.

5. Treat `sent_unconfirmed` or `unknown` outbox entries as an operator review
   condition. Do not retry them blindly.

[slack-verification]: https://docs.slack.dev/authentication/verifying-requests-from-slack/
[slack-socket]: https://docs.slack.dev/apis/events-api/using-socket-mode/
[discord-gateway]: https://docs.discord.com/developers/events/gateway
[discord-interactions]: https://docs.discord.com/developers/interactions/receiving-and-responding
[telegram-api]: https://core.telegram.org/bots/api
[feishu-events]: https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/encrypt-key-encryption-configuration-case?lang=en-US
[wecom-callback]: https://developer.work.weixin.qq.com/document/path/90930
[wecom-messages]: https://developer.work.weixin.qq.com/document/path/90236
[matrix-client]: https://spec.matrix.org/v1.19/client-server-api/
[qq-send]: https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html
[qq-media]: https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/rich-media.html
[dingtalk-messages]: https://open.dingtalk.com/document/dingstart/robot-reply-and-send-messages
[dingtalk-cards]: https://open.dingtalk.com/document/dingstart/using-event-chains-for-card-interaction
[teams-sdk]: https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/teams/sdk-comparison
[teams-migration]: https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/bf-migration-guidance

---

[Docs index](README.md) · [Product guide](../README.product.md) · [Improve this page](contributing-docs.md) · [Report a docs issue](https://github.com/opensquilla/opensquilla/issues/new?template=docs_report.yml)
