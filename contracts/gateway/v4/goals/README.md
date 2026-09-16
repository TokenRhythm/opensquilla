# Goals Contract slice

This slice defines the language-neutral v4 query/command boundary for
`goals.status`, `goals.set`, `goals.edit`, `goals.pause`, `goals.resume`,
`goals.clear`, and `goals.reattach`. It intentionally wraps the
existing Gateway handlers; it does not change `GoalService`, task execution,
persistence, or event production.

* Both `sessionKey`/`session_key`/`key` input aliases remain accepted.
* Goal snapshots keep unknown fields so old and newer Gateways can interoperate.
* `goals.set` keeps the existing UUID-v4 idempotency fields and error codes.
* `goals.reattach` keeps the continuity-token/takeover lease semantics and
  accepts the current camelCase, snake_case, and legacy key/epoch aliases.
* Generated Python and TypeScript wire types are adapter-only. Vue code should
  depend on `GoalCenter` from `opensquilla-webui/src/modules/goalCenter.ts`.

`GoalCenter` is injected into `useChatGoals` for all Goal query and mutation
operations, while the separate `GoalContinuity` Module owns `goals.reattach`
and `session.event.goal` decoding. The v4 method names and legacy aliases stay
inside the Gateway Adapter; the existing GoalService remains authoritative.
