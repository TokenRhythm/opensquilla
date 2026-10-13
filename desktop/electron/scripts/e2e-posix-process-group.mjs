import { readFileSync } from 'node:fs'
import { setTimeout as delay } from 'node:timers/promises'

export function readLinuxProcessStat(pid) {
  const raw = readFileSync('/proc/' + pid + '/stat', 'utf8')
  const recordedPid = /^(\d+) \(/.exec(raw)?.[1]
  const fields = raw.slice(raw.lastIndexOf(')') + 2).trim().split(/\s+/)
  if (Number(recordedPid) !== pid || !/^[A-Za-z]$/.test(fields[0] ?? '')
    || ![fields[1], fields[2], fields[19]].every(value => /^\d+$/.test(value ?? ''))) {
    throw new Error('Invalid owned child /proc stat.')
  }
  return { pid, ppid: Number(fields[1]), pgid: Number(fields[2]), startTicks: fields[19] }
}

const childExited = child => child.exitCode !== null || child.signalCode !== null
const pipesClosed = child => ['stdout', 'stderr'].every(name => (
  !child[name] || child[name].readableEnded || child[name].destroyed
))

// Playwright launches a new POSIX process group. Only force a still-live direct
// child whose own /proc record confirms that group; never infer it from PID alone.
// A leader that exited before this check cannot authorize killing its old group.
export async function terminateOwnedPosixProcessGroup(child, {
  platform = process.platform,
  ownerPid = process.pid,
  timeoutMs = 5_000,
  readStat = readLinuxProcessStat,
  signalGroup = (pgid, signal) => process.kill(-pgid, signal),
  now = () => performance.now(),
  sleep = delay,
} = {}) {
  const failure = message => ({ error: new Error(message), processTreeReaped: false })
  if (platform !== 'linux') return failure('Owned POSIX group identity cannot be verified on this platform.')
  if (!child || !Number.isSafeInteger(child.pid) || child.pid <= 1 || childExited(child)) {
    return failure('Live owned child unavailable; refusing process-group signal.')
  }
  const deadline = now() + timeoutMs
  try {
    const stat = readStat(child.pid)
    if (childExited(child) || stat.pid !== child.pid || stat.ppid !== ownerPid
      || stat.pgid !== child.pid || !/^\d+$/.test(stat.startTicks ?? '')) {
      return failure('Child is not our live detached process-group leader.')
    }
    // No await between the direct-child ownership check and this one signal.
    // Do not child.kill(): its killed flag disables Playwright's group cleanup.
    try { signalGroup(child.pid, 'SIGKILL') }
    catch (error) { if (error?.code !== 'ESRCH') throw error }
    while (now() < deadline) {
      let groupPresent = true
      try { signalGroup(child.pid, 0) }
      catch (error) {
        if (error?.code !== 'ESRCH') throw error
        groupPresent = false
      }
      if (!groupPresent && childExited(child) && pipesClosed(child)) {
        return { error: null, processTreeReaped: true }
      }
      await sleep(Math.min(25, Math.max(1, deadline - now())))
    }
    return failure('Owned POSIX process group or output pipes did not close within cleanup budget.')
  } catch (error) {
    return failure(error?.message || String(error))
  }
}
