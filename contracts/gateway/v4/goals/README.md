# Goals Contract slice

This slice defines the language-neutral v4 query/command boundary for
`goals.status`, `goals.set`, and `goals.reattach`. It intentionally wraps the
existing Gateway handlers; it does not change `GoalService`, task execution,
persistence, or event production.

* Both `sessionKey`/`session_key`/`key` input aliases remain accepted.
* Goal snapshots keep unknown fields so old and newer Gateways can interoperate.
* `goals.set` keeps the existing UUID-v4 idempotency fields and error codes.
* `goals.reattach` keeps the continuity-token/takeover lease semantics and
  accepts the current camelCase, snake_case, and legacy key/epoch aliases.
* Generated Python and TypeScript wire types are adapter-only. Vue code should
  depend on `GoalCenter` from `opensquilla-webui/src/modules/goalCenter.ts`.

`GoalCenter` is injected into `useChatGoals` for `goals.status` and
`goals.set`, while the separate `GoalContinuity` Module owns
`goals.reattach` and `session.event.goal` decoding. Edit/pause/resume/clear
mutations remain on the legacy path for a later slice. This keeps the current
RPC implementation authoritative while the migrated operations gain typed
domain seams.
