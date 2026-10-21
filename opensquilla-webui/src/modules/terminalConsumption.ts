/** One bounded cumulative ACK owner per terminal, independent of visibility.
 * The caller invokes consumed only from xterm.write's completion callback.
 * No output strings or per-frame promises are retained here.
 */
export function createTerminalConsumption(
  send: (seq: number, signal: AbortSignal) => Promise<unknown>,
  onOwnerLost?: (error: unknown) => void,
) {
  const controller = new AbortController()
  let consumedSeq = 0
  let acknowledgedSeq = 0
  let inFlight = false
  let retryDelay = 250
  let timer: ReturnType<typeof setTimeout> | null = null

  function schedule(delay = 16) {
    if (controller.signal.aborted || timer !== null || inFlight || consumedSeq <= acknowledgedSeq) return
    timer = setTimeout(() => { timer = null; void flush() }, delay)
  }

  async function flush() {
    if (controller.signal.aborted || inFlight || consumedSeq <= acknowledgedSeq) return
    inFlight = true
    const seq = consumedSeq
    let delay = 16
    try {
      await send(seq, controller.signal)
      acknowledgedSeq = seq
      retryDelay = 250
    } catch (error) {
      const code = error && typeof error === 'object' && 'code' in error ? error.code : null
      if (code === 'TERMINAL_NOT_FOUND' || code === 'UNAUTHORIZED' || code === 'TERMINAL_CLOSED') {
        controller.abort()
        onOwnerLost?.(error)
        return
      }
      // A timeout is an unknown receipt. Retrying the same cumulative cursor
      // is idempotent; never fall back to unbounded output on an ACK failure.
      delay = retryDelay
      retryDelay = Math.min(5_000, retryDelay * 2)
    } finally {
      inFlight = false
      schedule(delay)
    }
  }

  return {
    consumed(seq: number) {
      if (controller.signal.aborted || !Number.isSafeInteger(seq) || seq <= consumedSeq) return
      consumedSeq = seq
      schedule()
    },
    dispose() {
      controller.abort()
      if (timer !== null) clearTimeout(timer)
      timer = null
    },
  }
}
