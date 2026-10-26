# Session directory events

`sessions.changed` is a live-only, best-effort invalidation. The authoritative
state comes from `sessions.list`; this event does not replay on subscription.
Clients subscribe before their initial snapshot and refresh after reconnect.

The reserved pair `key: "*"` and `reason: "metadata_backfilled"` invalidates the
whole directory after background transcript metadata maintenance. `*` is not a
session identity. This notification carries no title, task, epoch, or run status
and must not create a session row or change task attention. Consumers coalesce
it into their ordinary directory refresh; consumers of an individual session
must not resolve `*` as a session key.

The maintenance worker sends at most one such notification per run: after work
completes, or after a non-cancellation failure that may follow committed work.
A successful run with no pending rows sends nothing. Cancellation does not start
a new notification; it cannot retract messages already sent by an in-flight
broadcast.
It is not a progress event and does not mean that every pending row was repaired.
The canonical example is in `fixtures/sessions-changed/events.json`.

This convention uses the existing open string fields of schema version 1.
Older clients that treat an unknown reason as a directory invalidation remain
compatible. No acknowledgement, replay cursor, or periodic polling is added.
