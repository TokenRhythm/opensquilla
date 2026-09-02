import { spawn, type ChildProcess } from 'node:child_process'

export interface WindowsProcessTreeTerminationFailure {
  readonly pid: number
  readonly timedOut: boolean
  readonly exitCode: number | null
  readonly signal: NodeJS.Signals | null
  readonly error: string | null
}

export interface WindowsProcessTreeTerminationOptions {
  readonly pid: number
  readonly timeoutMs: number
  readonly fallback: () => void
  readonly onFailure?: (failure: WindowsProcessTreeTerminationFailure) => void
  readonly spawnProcess?: typeof spawn
}

/**
 * Reap a Windows process tree without blocking Electron's main event loop.
 *
 * `spawnSync(taskkill)` can hold `before-quit` forever when the OS helper is
 * wedged. This asynchronous helper owns one absolute deadline, terminates the
 * helper on overrun, and invokes the caller's direct-child fallback.
 */
export function terminateWindowsProcessTree(
  options: WindowsProcessTreeTerminationOptions,
): Promise<boolean> {
  return new Promise(resolve => {
    let settled = false
    let timer: NodeJS.Timeout | null = null
    let killer: ChildProcess

    const finish = (
      success: boolean,
      failure: Omit<WindowsProcessTreeTerminationFailure, 'pid'> | null = null,
    ) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      if (!success && failure) {
        try {
          options.onFailure?.({ pid: options.pid, ...failure })
        } catch {
          // Diagnostics must never prevent the direct-child fallback.
        }
        try {
          options.fallback()
        } catch {
          // The caller records process liveness after this best-effort fallback.
        }
      }
      resolve(success)
    }

    try {
      killer = (options.spawnProcess ?? spawn)(
        'taskkill',
        ['/pid', String(options.pid), '/t', '/f'],
        { stdio: 'ignore', windowsHide: true },
      )
    } catch (error) {
      finish(false, {
        timedOut: false,
        exitCode: null,
        signal: null,
        error: String(error),
      })
      return
    }

    killer.once('error', error => {
      finish(false, {
        timedOut: false,
        exitCode: null,
        signal: null,
        error: String(error),
      })
    })
    killer.once('exit', (code, signal) => {
      finish(code === 0, code === 0 ? null : {
        timedOut: false,
        exitCode: code,
        signal,
        error: null,
      })
    })
    timer = setTimeout(() => {
      finish(false, {
        timedOut: true,
        exitCode: killer.exitCode,
        signal: killer.signalCode,
        error: `taskkill exceeded ${options.timeoutMs}ms`,
      })
      try {
        killer.kill('SIGKILL')
      } catch {
        // The direct-child fallback above is the final ownership boundary.
      }
    }, options.timeoutMs)
  })
}
