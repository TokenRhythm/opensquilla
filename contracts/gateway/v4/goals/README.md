# Goals Contract slice

This slice defines the language-neutral v4 query/command boundary for
`goals.status` and `goals.set`. It intentionally wraps the existing Gateway
handlers; it does not change `GoalService`, task execution, persistence, or
event production.

* Both `sessionKey`/`session_key`/`key` input aliases remain accepted.
* Goal snapshots keep unknown fields so old and newer Gateways can interoperate.
* `goals.set` keeps the existing UUID-v4 idempotency fields and error codes.
* Generated Python and TypeScript wire types are adapter-only. Vue code should
  depend on `GoalCenter` from `opensquilla-webui/src/modules/goalCenter.ts`.

`GoalCenter` is now injected into `useChatGoals` for `goals.status` and
`goals.set`; the remaining goal event decoding and edit/pause/resume/clear/
reattach mutations stay on the legacy path for a later slice. This keeps the
current RPC implementation authoritative while the two migrated operations
gain a typed domain seam.
