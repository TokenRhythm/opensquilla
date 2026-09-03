# Telemetry v2 external producer integration

The OpenSquilla repository owns the v2 collector, desktop application, Gateway,
Runtime, and Windows NSIS package. It does **not** contain the public website,
CDN/download service, or account service. Those services must integrate at
their own authoritative transaction boundaries; the desktop must not infer
their results.

## Shared destination

All growth producers send strict v1 batches to:

```text
POST https://<telemetry-origin>/v1/growth/events
Content-Type: application/json
```

The website, CDN, and account service each receive a different 32–64 byte
random secret. Raw secrets never enter browser JavaScript, URLs, installer
packages, application logs, or the dashboard. The collector refuses to start
without all three server-side credentials and refuses unsigned server-owned
events.

For every request these services send:

```text
X-OpenSquilla-Producer: website | cdn | account_service
X-OpenSquilla-Timestamp: <current Unix seconds>
X-OpenSquilla-Signature: v1=<lowercase HMAC-SHA256 hex>
```

The signature input is the ASCII string below, without a trailing newline:

```text
v1
POST
/v1/growth/events
<producer>
<timestamp>
<lowercase SHA-256 hex of the exact request body>
```

The collector accepts at most five minutes of clock skew. The signed producer
must match every event's `source`; mixed-source batches are rejected. Device
sources (`installer`, `desktop`, `gateway`, and `runtime`) cannot receive or use
these service credentials.

Python services can use
`opensquilla.telemetry.server_growth_producer.ServerGrowthProducer`. The helper
strictly validates and signs canonical batches, performs one bounded upload
attempt, and classifies the result. It intentionally does not provide an
in-memory retry queue.

## Authoritative event boundaries

Each service must create a stable UUIDv4 `event_id` and persist it in the same
transaction as the fact below. A retry uses the same event ID. The producer's
durable outbox also keeps a stable `batch_id` and `sent_at_utc` until that batch
is accepted.

| Owner | Event | Emit only when |
|---|---|---|
| Website backend | `landing_view` | the consented landing response is served and the first-party acquisition cookie is created/read |
| Website backend | `download_click` | a consented, valid download action is accepted by the backend |
| CDN/download service | `download_served` | the complete installer object is successfully delivered, not merely requested |
| Account service | `registration_result` | the registration transaction reaches success, fail, or cancel |

`analytics_user_id` is a random analytics-only UUID. It is not a hash of the
account ID. On successful registration, the account service stores the mapping
needed for deletion and emits both the journey's `acquisition_id` and the new
`analytics_user_id`. Failure and cancellation events must not contain an
analytics user ID.

## Deliberately inactive boundaries

The current ordinary NSIS package has no trustworthy acquisition token and the
user has not yet granted Growth consent before installation. It therefore must
not collect or upload `install_started` or `install_result`, and the desktop
must not backfill them after consent. These events can be activated only after
a guided installer or short-lived signed acquisition token is implemented with
an explicit pre-install notice and consent receipt.

The in-app registration action currently opens the external TokenRhythm site.
OpenSquilla has neither the account transaction nor a trustworthy acquisition
bridge at that point, so it must not emit `registration_result` or pretend the
external registration succeeded. The account service owns that result.

## Privacy and operations

- Never add IP address, MAC address, device fingerprint, raw account ID, URL
  query, referrer, file data, prompt, response, order, or payment fields.
- Reject unknown fields through the shared strict event contract.
- Do not sample growth events.
- Keep producer outboxes separate from business payloads and from Reliability
  telemetry.
- A `202` receipt is accepted only when its batch ID matches and
  `accepted + duplicates` equals the sent event count.
- Network ambiguity, `429`, and `5xx` are retryable. Authentication, contract,
  and identifier-conflict responses require operator repair and must not loop.
