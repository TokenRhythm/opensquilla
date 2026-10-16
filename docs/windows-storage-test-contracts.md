# SQLite acceptance and recovery test contracts

The scenarios below verify cancellation ownership, restart recovery and workspace rejection with
real SQLite. Their former two- or three-second waits also measured database and runner
speed, without corresponding product deadlines. These changes remove
these test-only completion limits; they do not change a production timeout or
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
- `test_fork_revalidates_file_replacement_after_durable_acceptance` replaces the
  project directory with a file only after the original acceptance commits.
  It awaits the exact registered direct task and verifies no runner call, one
  durable workspace error and one error terminal, then drains the owned registry
  tasks and closes/joins the real SQLite resources. Its shared two-second helper
  and all other project-workspace scenarios remain unchanged. Controls inject
  slow real commits, stale-context validation bypass, duplicate error writes,
  noncompletion at error persistence and noncompletion while closing SQLite.
- `test_cancelled_post_persist_write_settles_and_rolls_back_on_failure` preserves
  all four write-success/failure and owner/all-task cancellation combinations.
  Its held write boundary follows the original store read; no extra prewarming
  is added. It awaits the exact owner without a timeout injecting another cancel,
  checks rollback and one real commit, rereads the database and joins its owned
  worker after native connections close. Controls delay a real commit beyond
  three seconds, perform a duplicate write, retain a real connection, or block
  the worker inside a real transaction's commit boundary.

Each scenario owns one Python child and its SQLite/worker threads. The existing
`tests/helpers/sqlite_process_probe.py` gives cold setup one bounded startup
period, then 30 seconds for the complete scenario **including cleanup**. READY
transitions once; progress messages cannot renew the deadline. The watchdog
terminates only its exact child handle. These scenarios do not own descendant
processes and must not start external services.

Positive delay controls keep the real database operations and introduce a known
delay longer than the old per-test guard. They demonstrate the intended test
contract, not the root cause of a historical failure or a performance result.
Negative controls must hit their declared fault boundary and fail the original
behavior assertion. An import/setup error or an unrelated timeout is not a
passing negative control. For deliberate noncompletion, a dedicated fault-entry
signal starts a short watchdog only once the broken operation is actually
entered; the parent requires that phase and execution/cleanup timeout.

The scenarios retain their original test names and parallel shard placement.
Other tests' time budgets, Windows worker counts, SQLite durability, production
state transitions and shared watchdog implementation are unchanged.
