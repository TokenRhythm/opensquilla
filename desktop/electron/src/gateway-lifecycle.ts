import type { ChildProcess } from 'node:child_process'
import { performance } from 'node:perf_hooks'

export type GatewayStopReason =
  | 'desktop_stop'
  | 'desktop_quit'
  | 'desktop_update'
  | 'shutdown_endpoint'
  | 'ownership_verification_failed'

export interface GatewayProcessDiagnostics {
  requestStop(reason: GatewayStopReason): void
  requestTermination(signal: NodeJS.Signals): void
}

/** Observe the owned child independently of its stdio and recovery lifecycle. */
export function observeGatewayProcessLifecycle(
  child: Pick<ChildProcess, 'on' | 'once' | 'pid'>,
  options: {
    instanceId: string
    supervisorPid: number
    platform: NodeJS.Platform
    restartAttempt: number
    report: (event: string, detail: Record<string, unknown>) => void
    now?: () => number
  },
): GatewayProcessDiagnostics {
  const now = options.now ?? (() => performance.now())
  const startedAt = now()
  let stopReason: GatewayStopReason | null = null
  let requestedSignal: NodeJS.Signals | null = null
  let exitAt: number | null = null
  let errorObserved = false
  const report = (event: string, detail: Record<string, unknown> = {}): void => {
    try {
      options.report(event, {
        process_instance_id: options.instanceId,
        supervisor_pid: options.supervisorPid,
        pid: child.pid ?? null,
        platform: options.platform,
        restart_attempt: options.restartAttempt,
        uptime_ms: Math.max(0, now() - startedAt),
        stop_reason: stopReason,
        requested_signal: requestedSignal,
        ...detail,
      })
    } catch { /* diagnostic failure must not change process ownership */ }
  }
  child.once('spawn', () => report('gateway_process_started'))
  child.on('error', (error: NodeJS.ErrnoException) => {
    if (errorObserved) return
    errorObserved = true
    // Error messages can contain launch arguments or machine-specific paths.
    report('gateway_process_error_observed', { error_code: error.code ?? null })
  })
  child.once('exit', (code: number | null, signal: NodeJS.Signals | null) => {
    exitAt = now()
    report('gateway_process_exit_observed', {
      code,
      signal,
      // Windows may report a signed or unsigned NTSTATUS and no POSIX signal.
      windows_status_hex: options.platform === 'win32' && code !== null
        ? `0x${(code >>> 0).toString(16).padStart(8, '0')}` : null,
      outcome: stopReason !== null ? 'stop_requested'
        : signal !== null ? 'signal_observed'
          : code === 0 ? 'unrequested_clean_exit'
            : code !== null ? 'nonzero_exit' : 'unknown_exit',
    })
  })
  child.once('close', () => {
    report('gateway_process_stdio_closed', {
      exit_observed: exitAt !== null,
      exit_to_close_ms: exitAt === null ? null : Math.max(0, now() - exitAt),
    })
  })
  return {
    requestStop(reason) {
      if (exitAt !== null || stopReason !== null) return
      stopReason = reason
      report('gateway_process_stop_requested')
    },
    requestTermination(signal) {
      if (exitAt !== null || signal === requestedSignal) return
      requestedSignal = signal
      report('gateway_process_termination_requested')
    },
  }
}

export interface LifecycleProcessDrainOptions<T> {
  currentProcess: () => T | null
  stopCurrentProcess: (process: T) => void
  liveProcesses: () => T[]
  waitForExit: (process: T) => Promise<boolean>
  onStopRequested?: (process: T) => void
  onChildExited?: (process: T, exited: boolean) => void
  onComplete?: (exited: boolean) => void
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

export const DESKTOP_GATEWAY_STARTUP_TIMEOUT_MS = 120_000

export class GatewayReadinessTimeoutError extends Error {
  constructor(url: string) {
    super(`Gateway did not become ready at ${url}`)
    this.name = 'GatewayReadinessTimeoutError'
  }
}

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
  const report = (callback: (() => void) | undefined): void => {
    try { callback?.() } catch { /* diagnostics are fail-open */ }
  }
  const reportChild = (callback: ((exited: boolean) => void) | undefined, exited: boolean): void => {
    try { callback?.(exited) } catch { /* diagnostics are fail-open */ }
  }
  const maxRounds = options.maxRounds ?? 8
  for (let round = 0; round < maxRounds; round += 1) {
    const current = options.currentProcess()
    if (current !== null) {
      report(() => options.onStopRequested?.(current))
      options.stopCurrentProcess(current)
    }

    const processes = [...new Set(options.liveProcesses())]
    if (processes.length === 0) {
      const complete = options.currentProcess() === null
      report(() => options.onComplete?.(complete))
      return complete
    }

    const exited = await Promise.all(processes.map((process) => options.waitForExit(process)))
    processes.forEach((process, index) => reportChild(
      done => options.onChildExited?.(process, done), exited[index],
    ))
    if (!exited.every(Boolean)) {
      report(() => options.onComplete?.(false))
      return false
    }
  }
  const complete = options.currentProcess() === null && options.liveProcesses().length === 0
  report(() => options.onComplete?.(complete))
  return complete
}
