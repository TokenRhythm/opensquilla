# Channel acceptance and restart-recovery test contracts

The two scenarios below verify cancellation ownership and restart recovery with
real SQLite. Their former two-second waits also measured database and runner
speed, without a corresponding two-second product deadline. This change removes
that test-only completion limit; it does not change a production timeout or
identify the physical cause of the earlier CI delays.

- `test_submitted_accept_cancellation_still_hands_off_exactly_once` cancels the
  caller only after the real acceptance commit reaches its held boundary. The
  original worker must still hand off once, reject a duplicate acceptance, close
  its database and leave the accepted row readable from a new connection.
- `test_resumed_post_reservation_error_releases_lease_without_busy_retry`
  observes actual reservation cleanup and the recovery worker returning to its
  notification wait. A later notification must produce one successful execution
  and the original durable task records, with no autonomous retry or leaked
  resident reservation.

Each scenario owns one Python child and its SQLite/worker threads. The existing
`tests/helpers/sqlite_process_probe.py` gives cold setup one bounded startup
period, then 30 seconds for the complete scenario **including cleanup**. READY
transitions once; progress messages cannot renew the deadline. The watchdog
terminates only its exact child handle. These scenarios do not own descendant
processes and must not start external services.

Positive delay controls keep the real database operations and introduce a known
delay longer than the old two-second guard. They demonstrate the intended test
contract, not the root cause of a historical failure or a performance result.
Negative controls must hit their declared fault boundary and fail the original
behavior assertion. An import/setup error or an unrelated timeout is not a
passing negative control. For deliberate noncompletion, a dedicated fault-entry
signal starts a short watchdog only once the broken operation is actually
entered; the parent requires that phase and execution/cleanup timeout.

The scenarios retain their original test names and parallel shard placement.
Other tests' time budgets, Windows worker counts, SQLite durability, production
state transitions and shared watchdog implementation are unchanged.
