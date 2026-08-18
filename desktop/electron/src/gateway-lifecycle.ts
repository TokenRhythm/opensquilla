import { performance } from 'node:perf_hooks'

export interface LifecycleProcessDrainOptions<T> {
  currentProcess: () => T | null
  stopCurrentProcess: (process: T) => void
  liveProcesses: () => T[]
  waitForExit: (process: T) => Promise<boolean>
  maxRounds?: number
}

export interface GatewayReadinessWaitOptions {
  probe: (remainingMs: number) => Promise<boolean>
  exitMessage?: () => string | null
  primaryTimeoutMs: number
  lateGraceMs: number
  pollIntervalMs: number
  now?: () => number
  sleep?: (milliseconds: number) => Promise<void>
}

export type GatewayReadinessWaitResult =
  | { status: 'ready'; late: boolean }
  | { status: 'exited'; message: string }
  | { status: 'timeout' }

/**
 * Wait for a spawned Gateway without turning the first readiness deadline into
 * an irreversible failure. The late window remains bounded, and child exit is
 * checked on both sides of every probe so a dead process never consumes the
 * remainder of the startup budget.
 */
export async function waitForGatewayReadiness(
  options: GatewayReadinessWaitOptions,
): Promise<GatewayReadinessWaitResult> {
  const now = options.now ?? (() => performance.now())
  const sleep = options.sleep ?? ((milliseconds: number) => (
    new Promise<void>((resolve) => setTimeout(resolve, milliseconds))
  ))
  const primaryTimeoutMs = Math.max(0, options.primaryTimeoutMs)
  const totalTimeoutMs = primaryTimeoutMs + Math.max(0, options.lateGraceMs)
  const pollIntervalMs = Math.max(1, options.pollIntervalMs)
  const startedAt = now()

  while (true) {
    const beforeProbeExit = options.exitMessage?.()
    if (beforeProbeExit) return { status: 'exited', message: beforeProbeExit }

    const elapsedBeforeProbe = Math.max(0, now() - startedAt)
    const remainingBeforeProbeMs = totalTimeoutMs - elapsedBeforeProbe
    if (remainingBeforeProbeMs <= 0) return { status: 'timeout' }

    let probeTimeout: NodeJS.Timeout | null = null
    const ready = await Promise.race<boolean | null>([
      options.probe(remainingBeforeProbeMs),
      new Promise<null>((resolveTimeout) => {
        probeTimeout = setTimeout(() => resolveTimeout(null), remainingBeforeProbeMs)
      }),
    ]).finally(() => {
      if (probeTimeout) clearTimeout(probeTimeout)
    })

    const afterProbeExit = options.exitMessage?.()
    if (afterProbeExit) return { status: 'exited', message: afterProbeExit }

    const elapsedAfterProbe = Math.max(0, now() - startedAt)
    if (ready === null || elapsedAfterProbe >= totalTimeoutMs) return { status: 'timeout' }
    if (ready) return { status: 'ready', late: elapsedAfterProbe >= primaryTimeoutMs }

    const remainingMs = totalTimeoutMs - elapsedAfterProbe
    if (remainingMs <= 0) return { status: 'timeout' }
    await sleep(Math.min(pollIntervalMs, remainingMs))
  }
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
