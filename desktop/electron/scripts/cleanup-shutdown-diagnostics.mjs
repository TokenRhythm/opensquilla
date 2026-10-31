import { createConnection } from 'node:net'
import { gatewayProcessSnapshot } from './e2e-shutdown-helpers.mjs'
import { sameDesktopGatewayOwnershipInstance } from '../dist/desktop-gateway-ownership.js'

// Diagnostic only: a timeout or denied connection is never proof of a closed port.
export function loopbackListenerSnapshot(port, { connect = createConnection, timeoutMs = 500 } = {}) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    return Promise.resolve({ port, status: 'unknown', errorCode: 'invalid_port' })
  }
  return new Promise(resolve => {
    let socket
    let timer
    let settled = false
    const finish = (status, errorCode = null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      socket?.destroy()
      resolve({ port, status, errorCode })
    }
    const failed = error => {
      const code = ['ECONNREFUSED', 'ECONNRESET', 'EACCES', 'EPERM', 'ETIMEDOUT'].includes(error?.code)
        ? error.code : 'other'
      finish(code === 'ECONNREFUSED' ? 'closed' : 'unknown', code)
    }
    try {
      socket = connect({ host: '127.0.0.1', port })
      socket.once('connect', () => finish('listening'))
      socket.once('error', failed)
      timer = setTimeout(() => finish('unknown', 'timeout'), timeoutMs)
    } catch (error) { failed(error) }
  })
}

export async function cleanupGatewaySnapshot(record, loaded, {
  processSnapshot = gatewayProcessSnapshot,
  listenerSnapshot = loopbackListenerSnapshot,
} = {}) {
  let relation = loaded.status
  if (loaded.status === 'valid') {
    relation = sameDesktopGatewayOwnershipInstance(record, loaded.record)
      && record.schema_version === loaded.record.schema_version
      && record.version === loaded.record.version ? 'same-instance' : 'replaced'
  }
  return {
    // Probe the verified Python owner, which differs from the uv launcher PID.
    process: processSnapshot(record),
    listener: await listenerSnapshot(record.port),
    ownership: ['missing', 'same-instance', 'replaced', 'invalid'].includes(relation)
      ? relation : 'invalid',
  }
}

const GATEWAY_EVENTS = new Set([
  'gateway.shutdown_requested', 'gateway.shutdown_phase_timeout',
  'gateway.desktop_shutdown_deadline', 'gateway.shutdown_incomplete_force_exit',
  'gateway.owned_process_cleanup_failed', 'gateway.owned_process_cleanup_incomplete',
  'gateway.task_runtime_shutdown_failed', 'gateway.task_runtime_shutdown_incomplete',
  'gateway.terminal_shutdown_timeout', 'gateway.terminal_shutdown_failed',
  'gateway.skill_install_shutdown_incomplete', 'gateway.channel_shutdown_incomplete',
  'gateway.channels_stopped', 'gateway.mcp_clients_closed',
  'gateway.mcp_clients_close_skipped_for_live_runtime',
  'gateway.background_completion_close_skipped_for_live_runtime',
  'gateway.services_close_skipped_for_live_runtime',
  'gateway.run_started', 'gateway.run_finished', 'gateway.stopped',
  'gateway.shutdown_watchdog_expired', 'gateway.transports_stopped_with_runtime_residuals',
  'gateway.deferred_warmups_started', 'gateway.deferred_warmups_ready', 'gateway.deferred_warmups_cancelled',
  'gateway.squilla_router_preload_started', 'gateway.squilla_router_preloaded',
  'gateway.squilla_router_preload_skipped', 'gateway.squilla_router_preload_unavailable',
  'gateway.squilla_router_preload_failed',
])
const GATEWAY_VALUES = {
  phase: new Set(['terminals', 'optional_start', 'warmup', 'skills', 'runtime', 'goals',
    'owned_processes', 'owned_processes_final', 'background_completion', 'channels',
    'broadcast', 'websockets', 'server', 'preview_server', 'mcp', 'services', 'protected_settlement']),
  mode: new Set(['quit', 'drain']),
  reason: new Set(['desktop_quit', 'desktop_drain', 'desktop_api_shutdown', 'api_shutdown',
    'shutdown', 'keyboard_interrupt', 'sigterm', 'sigint', 'desktop_fast_start']),
  outcome: new Set(['shutdown_requested', 'server_task_finished', 'interrupted', 'exception']),
  platform: new Set(['win32', 'linux', 'darwin']),
  error_type: new Set(['CancelledError', 'KeyboardInterrupt', 'TimeoutError', 'RuntimeError',
    'OSError', 'PermissionError', 'SystemExit', 'Exception', 'ValueError']),
}
const GATEWAY_NUMBERS = ['elapsed_ms', 'exit_code', 'running', 'queued', 'waiting',
  'reservations', 'auxiliary', 'remaining_drivers', 'remaining_reservations',
  'remaining_auxiliary', 'abandoned_tasks', 'duration_ms', 'pid', 'parent_pid', 'timeout', 'count', 'generation']
// PrivateLogFormatter emits metadata JSON after this timestamp/level/logger
// prefix. Do not search for arbitrary JSON embedded in raw exception prose.
const PRIVATE_LOG_PREFIX = /^\d{4}-\d\d-\d\d[ T][\d:.,+Z-]+\s+\[(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\]\s+[A-Za-z_][A-Za-z0-9_.]*:\s*/

/** Extract only known lifecycle events and bounded values; never return a raw log line. */
export function gatewayLifecycleLogSnapshot(text) {
  if (typeof text !== 'string') return { logReadFailed: true, records: [] }
  const records = []
  for (const raw of text.slice(-256 * 1024).split(/\r?\n/)) {
    const line = raw.replace(/\x1b\[[0-9;]*m/g, '')
    const prefix = line.match(PRIVATE_LOG_PREFIX)
    let parsed
    try { parsed = JSON.parse(prefix ? line.slice(prefix[0].length) : line) } catch {
      if (prefix) continue
      // Legacy dev logs may use the unstructured console renderer.
    }
    const structured = parsed !== null && typeof parsed === 'object'
    const event = structured
      ? parsed.event : [...GATEWAY_EVENTS].find(name => (
        new RegExp(`(?:^|\\s)${name.replaceAll('.', '\\.')}(?=\\s|$)`).test(line)
      ))
    if (!GATEWAY_EVENTS.has(event)) continue
    const record = { event }
    for (const [field, allowed] of Object.entries(GATEWAY_VALUES)) {
      const value = structured ? parsed[field]
        : line.match(new RegExp(`\\b${field}=([A-Za-z_]+)(?=\\s|$)`))?.[1]
      if (allowed.has(value)) record[field] = value
    }
    for (const field of GATEWAY_NUMBERS) {
      const rawValue = structured ? parsed[field]
        : line.match(new RegExp(`\\b${field}=(\\d+(?:\\.\\d+)?)(?=\\s|$)`))?.[1]
      const value = typeof rawValue === 'string' ? Number(rawValue) : rawValue
      if (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER) {
        record[field] = value
      }
    }
    records.push(record)
  }
  return { records: records.slice(-32) }
}

const STALL_NUMBERS = ['ts', 'capture_ts', 'capture_monotonic_s', 'capture_finished_monotonic_s',
  'lag_ms', 'heartbeat_age_ms']
const STALL_COUNTS = ['heartbeat_seq', 'detected_heartbeat_seq', 'heartbeat_seq_before', 'heartbeat_seq_after']
const STALL_FLAGS = ['heartbeat_alive', 'heartbeat_started', 'same_heartbeat_window']
const PYTHON_FRAME_NAMES = new Set(['<module>', '<lambda>', '<listcomp>', '<dictcomp>', '<setcomp>', '<genexpr>'])

/** Failure-only projection of the opt-in watchdog; paths, source and locals never leave the fixture. */
export function gatewayStallLogSnapshot(text) {
  if (typeof text !== 'string') return { logReadFailed: true, records: [] }
  const records = []
  for (const line of text.slice(-256 * 1024).split(/\r?\n/)) {
    let parsed
    try { parsed = JSON.parse(line) } catch { continue }
    if (parsed?.type !== 'stack_sample' || !Array.isArray(parsed.stack)) continue
    const record = { type: 'stack_sample' }
    for (const field of [...STALL_NUMBERS, ...STALL_COUNTS]) {
      const value = parsed[field]
      if (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER
        && (!STALL_COUNTS.includes(field) || Number.isSafeInteger(value))) record[field] = value
    }
    for (const field of STALL_FLAGS) {
      if (typeof parsed[field] === 'boolean') record[field] = parsed[field]
    }
    // The producer reverses f_back traversal: its innermost frames are last.
    record.stack = parsed.stack.slice(-12).flatMap(frame => {
      if (!frame || typeof frame.file !== 'string' || typeof frame.function !== 'string'
        || !Number.isSafeInteger(frame.line) || frame.line < 1) return []
      const file = frame.file.replaceAll('\\', '/').split('/').at(-1)
      if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(file)
        || (!/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(frame.function) && !PYTHON_FRAME_NAMES.has(frame.function))) return []
      return [{ file, function: frame.function, line: frame.line }]
    })
    records.push(record)
  }
  return { records: records.slice(-4) }
}
