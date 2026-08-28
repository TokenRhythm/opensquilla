/** Compare one authored transport-debt kind in both directions. */
export function exactTransportDebtFailures(kind, expected, actual) {
  const failures = []
  for (const [rel, count] of actual) {
    const approved = expected.get(rel)
    if (approved === undefined) {
      failures.push(
        `${rel}: unexpected raw transport ${kind} (${count}); add a domain Adapter instead.`,
      )
    } else if (approved !== count) {
      failures.push(
        `${rel}: raw transport ${kind} count is ${count}; lane debt requires ${approved}.`,
      )
    }
  }
  for (const [rel, count] of expected) {
    if (!actual.has(rel)) {
      failures.push(
        `${rel}: stale raw transport ${kind} debt (${count}); remove it from its lane file.`,
      )
    }
  }
  return failures
}
