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

/** Extract only known lifecycle events and bounded values; never return a raw log line. */
export function gatewayLifecycleLogSnapshot(text) {
  if (typeof text !== 'string') return { logReadFailed: true, records: [] }
  const records = []
  for (const raw of text.slice(-256 * 1024).split(/\r?\n/)) {
    const line = raw.replace(/\x1b\[[0-9;]*m/g, '')
    let parsed
    try { parsed = JSON.parse(line) } catch { /* Dev logs may use the console renderer. */ }
    const event = parsed && typeof parsed === 'object'
      ? parsed.event : [...GATEWAY_EVENTS].find(name => (
        new RegExp(`(?:^|\\s)${name.replaceAll('.', '\\.')}(?=\\s|$)`).test(line)
      ))
    if (!GATEWAY_EVENTS.has(event)) continue
    const record = { event }
    for (const [field, allowed] of Object.entries(GATEWAY_VALUES)) {
      const value = parsed?.[field] ?? line.match(new RegExp(`\\b${field}=([A-Za-z_]+)(?=\\s|$)`))?.[1]
      if (allowed.has(value)) record[field] = value
    }
    for (const field of GATEWAY_NUMBERS) {
      const rawValue = parsed?.[field] ?? line.match(new RegExp(`\\b${field}=(\\d+(?:\\.\\d+)?)(?=\\s|$)`))?.[1]
      const value = typeof rawValue === 'string' ? Number(rawValue) : rawValue
      if (typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER) {
        record[field] = value
      }
    }
    records.push(record)
  }
  return { records: records.slice(-32) }
}
