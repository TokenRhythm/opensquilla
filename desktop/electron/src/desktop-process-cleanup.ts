import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'

import type { DesktopQuitBudget } from './desktop-quit.js'

const CLEANUP_PROTOCOL = 'opensquilla-desktop-process-cleanup-v1'
const CLEANUP_OUTPUT_LIMIT = 8 * 1024

export interface DesktopProcessCleanupLaunch {
  command: string
  args: string[]
  cwd: string
  mode: 'bundled' | 'dev'
  env: NodeJS.ProcessEnv
  ownershipDir: string
  instanceNonce: string
}

export interface DesktopProcessCleanupOptions {
  launch: DesktopProcessCleanupLaunch
  budget: DesktopQuitBudget
  spawnProcess?: typeof spawn
  signalProcess?: typeof process.kill
}

/** Confirm the retained launch's tools without depending on its Gateway event loop. */
export async function cleanupDesktopProcessTrees(
  options: DesktopProcessCleanupOptions,
): Promise<boolean> {
  const { launch, budget } = options
  if (budget.remainingTotalMs <= 0) return false
  const args = launch.mode === 'bundled'
    ? ['--internal-child', 'process-tree', '--desktop-quit-cleanup']
    : ['run', '--no-sync', 'python', '-m', 'opensquilla.process_tree', '--desktop-quit-cleanup']
  const environment = { ...launch.env }
  for (const key of [
    'OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_NONCE',
    'OPENSQUILLA_DESKTOP_GATEWAY_INSTANCE_ID',
    'OPENSQUILLA_DESKTOP_GATEWAY_OWNERSHIP_DIR',
  ]) delete environment[key]
  let helper: ChildProcessWithoutNullStreams | undefined
  let exited = false
  let finish!: (complete: boolean) => void
  let settled = false
  const stopHelper = (): void => {
    // Only this newly spawned, detached helper group is signalable. Once its
    // child handle exits, its numeric PID is never used again.
    if (!helper || exited || helper.exitCode !== null || helper.signalCode !== null) return
    if (helper.pid) {
      try {
        (options.signalProcess ?? process.kill)(-helper.pid, 'SIGKILL')
        return
      } catch {
        // The exact child handle is the final fallback if its group is gone.
      }
    }
    try { helper.kill('SIGKILL') } catch {}
  }
  const work = new Promise<boolean>(resolve => {
    finish = complete => {
      if (settled) return
      settled = true
      resolve(complete)
    }
    try {
      helper = (options.spawnProcess ?? spawn)(launch.command, args, {
        cwd: launch.cwd,
        env: environment,
        detached: true,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
      }) as ChildProcessWithoutNullStreams
      let stdout = ''
      helper.stdout.on('data', (chunk: Buffer | string) => {
        if (settled) return
        stdout += String(chunk)
        if (Buffer.byteLength(stdout, 'utf8') > CLEANUP_OUTPUT_LIMIT) {
          stopHelper()
          finish(false)
        }
      })
      // Neither the launch secret nor helper diagnostics belong in desktop logs.
      helper.stderr.resume()
      helper.stdin.on('error', () => { stopHelper(); finish(false) })
      helper.once('error', () => { stopHelper(); finish(false) })
      helper.once('exit', () => { exited = true })
      helper.once('close', (code, signal) => {
        exited = true
        let complete = false
        if (code === 0 && signal === null) {
          try {
            const response: unknown = JSON.parse(stdout)
            complete = typeof response === 'object' && response !== null
              && !Array.isArray(response)
              && Object.keys(response).length === 2
              && 'protocol' in response && response.protocol === CLEANUP_PROTOCOL
              && 'complete' in response && response.complete === true
          } catch {}
        }
        finish(complete)
      })
      helper.stdin.end(JSON.stringify({
        ownership_dir: launch.ownershipDir,
        instance_nonce: launch.instanceNonce,
        timeout_ms: Math.min(5000, budget.remainingTotalMs),
      }) + '\n')
    } catch {
      stopHelper()
      finish(false)
    }
  })
  const result = await budget.termination(work)
  if (!result.completed) {
    stopHelper()
    finish(false)
  }
  return result.completed && result.value
}
