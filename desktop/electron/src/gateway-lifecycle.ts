export interface LifecycleProcessDrainOptions<T> {
  currentProcess: () => T | null
  stopCurrentProcess: (process: T) => void
  liveProcesses: () => T[]
  waitForExit: (process: T) => Promise<boolean>
  maxRounds?: number
}

export function lifecycleAllowsProcessSpawn(
  lifecycleClosing: boolean,
  profileWriterAdmissionClosed: boolean,
  liveOwnedProcessCount = 0,
): boolean {
  return (
    !lifecycleClosing
    && !profileWriterAdmissionClosed
    && liveOwnedProcessCount === 0
  )
}

/**
 * Stop the current process and join every process that remains owned by the
 * lifecycle, including children whose stop was initiated by an earlier flow.
 *
 * A bounded retry closes the small race where a previously-started async flow
 * publishes its child while an earlier snapshot is being awaited. Exhaustion
 * fails closed: callers must not continue with update/profile writes while any
 * owned process remains live.
 */
export async function stopAndJoinLifecycleProcesses<T>(
  options: LifecycleProcessDrainOptions<T>,
): Promise<boolean> {
  const maxRounds = options.maxRounds ?? 8
  for (let round = 0; round < maxRounds; round += 1) {
    const current = options.currentProcess()
    if (current !== null) options.stopCurrentProcess(current)

    const processes = [...new Set(options.liveProcesses())]
    if (processes.length === 0) return options.currentProcess() === null

    const exited = await Promise.all(processes.map((process) => options.waitForExit(process)))
    if (!exited.every(Boolean)) return false
  }
  return options.currentProcess() === null && options.liveProcesses().length === 0
}
