# Conversation and Turn Command Contracts

This directory describes the v4 conversation event family used by the
WebSocket replay stream. It is a compatibility Contract, not a new wire
channel.

* `conversation-events.schema.json` is the source of generated event types;
  the TurnCommands schemas below are the corresponding sources for command
  types.
* `wireNames` is the reviewed list of event names currently consumed by chat;
  the frame pattern intentionally accepts additive `session.event.*` and
  `task.*` names.
* Existing producers may omit `schema_version`. The decoder accepts that
  legacy form and never rewrites the event sent on the wire.
* `stream_generation`/`stream_seq` are session replay coordinates; `seq` is
  the per-connection WebSocket coordinate. They must not be conflated.
* Generated wire models/validators are imported only by the corresponding
  adapters and Contract tests. The generated Python registry may compose those
  models for metadata, but Gateway handlers and Vue components must not import
  generated wire types directly.

S9 only lands the decoder and fixtures. Event producers and live consumers are
intentionally unchanged; those changes belong to the later Conversation
Runtime migration after the compatibility matrix is green.

The TurnCommands slice also describes the five existing v4 command routes:
`chat.send`, `chat.abort`, `sessions.steer.v2`,
`sessions.pending_inputs.dispatch`, and `sessions.pending_inputs.steer`.
Their schemas are compatibility descriptions for the existing JSON frames;
they do not register a second handler or alter the Gateway implementation.
The WebUI `turnCommandsV4` Adapter is the only production consumer of their
generated request/result validators. Request validation is advisory so the
Gateway remains the owner of historical malformed-input errors; a response
that violates its Contract fails closed at the Adapter boundary.

The generated Python models are retained for Contract fixtures, registry
metadata, and cross-language generation checks in this slice. They are not
imported by Gateway handlers or other production implementations; no Python
transport or handler wrapper is introduced here.
