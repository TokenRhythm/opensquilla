/**
 * Run a telemetry-only local side effect without changing the observed product
 * operation's outcome. The caller's failure callback is best-effort as well so
 * logging or closing a runtime gate can never replace the original operation.
 */
export async function runTelemetrySideEffectFailOpen(
  sideEffect: () => Promise<void>,
  onFailure: () => void,
): Promise<boolean> {
  try {
    await sideEffect()
    return true
  } catch {
    try {
      onFailure()
    } catch {
      // Telemetry diagnostics are never an application control-flow boundary.
    }
    return false
  }
}
