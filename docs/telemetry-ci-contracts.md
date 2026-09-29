# Telemetry shutdown CI pilot

This is a bounded test-design pilot, not a change to product deadlines or a
repository-wide increase in timeouts. It covers the two send-lock/accepted-write
shutdown regressions in `tests/test_telemetry/test_client_runtime.py`.

## Coverage that must stay independent

1. `test_shutdown_deadlines.py` checks exact `start + budget` arithmetic, the
   guard's target/deadline, idempotency before and after expiration, and delivery
   of an already-expired real asyncio timeout. It never changes the real event
   loop's clock.
2. The existing controlled-deadline fixture checks cancellation consequences.
   It is intentionally NOT evidence that a real timer fires at the right time.
3. The isolated shutdown probes keep the real timer, consent lock, SQLite
   transaction, runtime close, and database reopen. Only HTTP is mocked. An
   event barrier holds a real commit until upload cancellation and the blocked
   producer have both been observed. Reopening verifies accepted records and
   the unacknowledged lease. Do not replace this coupled scenario with separate
   successful cancellation and successful database tests.

## Budgets and ownership

- The existing one-second cancellation check and test's 50 ms product deadline
  override are retained. Neither includes cold database setup or commit time.
- Durable completion has a separate 10-second test guard; task cancellation has
  a five-second cleanup guard. Neither changes production timeout constants.
- A parent process allows 90 seconds for cold setup, then at most 30 seconds for
  the entire scenario, including teardown/reopen. READY can transition only once;
  repeated log output cannot extend the budget. The absolute cap is 120 seconds,
  plus a bounded five-second OS reap after termination.
- The child owns SQLite threads, not descendant processes. The watchdog kills
  only its exact `Popen` process handle. This helper must not be reused for a
  process-tree scenario without implementing and testing descendant ownership.
- Timeout output includes the last phase and the child's combined log. A
  timeout remains a test failure, never an automatically retryable CI error.

## Negative controls and scheduling

The parent tests must reject a disabled cancellation guard and a producer that
claims success while dropping a record. A cancellation-resistant cleanup must
be terminated by the external watchdog, rather than hanging the pytest worker.

Native probes have explicit `ci_serial` marks on TESTS, not on fixtures. The
collection regression verifies that parallel/serial node sets are disjoint and
their union is the complete pilot inventory. No worker-count changes or dynamic
fixture-time marker injection are part of this pilot.

## Validation

```text
uv run --frozen --extra dev pytest tests/test_telemetry/test_client_runtime.py tests/test_telemetry/test_shutdown_deadlines.py -q
uv run --frozen --extra dev pytest tests/test_ci/test_windows_test_shards.py -q -k telemetry
```

Use fresh Windows CI as the integration gate. Track first-attempt results; a
rerun is not stability evidence. Do not generalize this pilot to remaining
telemetry shutdown cases or other suites until those contracts and their
failure/cleanup paths have been audited separately.

## Queue-discovered authority-test follow-up

Queue run `36544294774` failed in the project-child restart authority test,
outside the telemetry pilot: its test-only one-second stream-idle override
expired during a real filesystem/persistence scenario. The two matching
restart-authority tests now retain the production stream wrapper and default
idle budget, with a separate 60-second asyncio guard around each dispatch.
Product defaults, real storage/reopen, authority assertions, and stream-timeout
unit tests are unchanged. A 1.1-second injected write delay reproduces the old
failure and must still pass all authority and removed-project assertions.
This guard covers cooperative dispatch, not cancellation-resistant cleanup;
the telemetry process watchdog is not generalized to these tests.
