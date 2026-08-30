# Conversation event Contract

This directory describes the v4 conversation event family used by the
WebSocket replay stream. It is a compatibility Contract, not a new wire
channel.

* `conversation-events.schema.json` is the only source of generated types.
* `wireNames` is the reviewed list of event names currently consumed by chat;
  the frame pattern intentionally accepts additive `session.event.*` and
  `task.*` names.
* Existing producers may omit `schema_version`. The decoder accepts that
  legacy form and never rewrites the event sent on the wire.
* `stream_generation`/`stream_seq` are session replay coordinates; `seq` is
  the per-connection WebSocket coordinate. They must not be conflated.
* Generated Python/TypeScript files are imported only by the corresponding
  adapters and Contract tests. No Vue component or runtime implementation
  should import generated wire types directly.

S9 only lands the decoder and fixtures. Event producers and live consumers are
intentionally unchanged; those changes belong to the later Conversation
Runtime migration after the compatibility matrix is green.
