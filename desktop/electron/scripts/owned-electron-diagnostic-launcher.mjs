import { spawn } from 'node:child_process'
import { win32 } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

import { chromium } from 'playwright'
import { terminateWindowsProcessTree } from '../dist/windows-process-tree.js'

async function bounded(operation, label, timeoutMs) {
  let timer
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs)
      }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function processExists(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    if (error.code === 'ESRCH') return false
    throw new Error('Owned Electron process observation failed')
  }
}

function quoteControlledArgument(value) {
  // This diagnostic accepts only controlled CI paths/flags. It is deliberately
  // not a general cmd.exe quoting implementation (including expansion syntax).
  if (typeof value !== 'string' || !value || /[\x00-\x1f\x7f"'&|<>^()%!]/.test(value) || value.endsWith('\\')) {
    throw new Error('Unsupported owned Electron command argument')
  }
  return `"${value}"`
}

function loopbackEndpoint(value) {
  const endpoint = new URL(value)
  if (endpoint.protocol !== 'ws:' || !['127.0.0.1', 'localhost', '[::1]'].includes(endpoint.hostname)
      || !endpoint.port || endpoint.username || endpoint.password) {
    throw new Error('Unexpected owned Electron debug endpoint')
  }
  return endpoint.href
}

class InspectorConnection {
  constructor(endpoint) {
    const socket = this.socket = new WebSocket(endpoint)
    this.nextId = 0
    this.pending = new Map()
    this.didClose = false
    this.opened = new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true })
      socket.addEventListener('error', () => reject(new Error('Node inspector connection failed')), { once: true })
    })
    this.closed = new Promise(resolve => socket.addEventListener('close', () => {
      this.didClose = true
      for (const pending of this.pending.values()) pending.reject(new Error('Node inspector disconnected'))
      this.pending.clear()
      resolve()
    }, { once: true }))
    socket.addEventListener('message', event => {
      let message
      try { message = JSON.parse(String(event.data)) } catch { return }
      const pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id)
      if (message.error) pending.reject(new Error('Node inspector protocol request failed'))
      else pending.resolve(message.result)
    })
  }

  async request(method, params = {}) {
    const id = ++this.nextId
    return bounded(() => new Promise((resolve, reject) => {
      if (this.socket.readyState !== WebSocket.OPEN) {
        reject(new Error('Node inspector is not connected'))
        return
      }
      this.pending.set(id, { resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    }), 'Node inspector request', 5_000).finally(() => this.pending.delete(id))
  }

  async evaluate(fn, arg) {
    const expression = `(${String(fn)})(require('electron'),${arg === undefined ? 'undefined' : JSON.stringify(arg)})`
    const result = await this.request('Runtime.evaluate', {
      expression, includeCommandLineAPI: true, returnByValue: true, awaitPromise: true,
    })
    if (result.exceptionDetails) throw new Error('Node inspector evaluation threw')
    return result.result.value
  }

  async close() {
    if (!this.didClose) this.socket.close()
    await bounded(() => this.closed, 'Node inspector close acknowledgment', 5_000)
  }
}

/**
 * Windows-only transport diagnostic, not the normal Electron launcher. Unlike
 * Playwright's Electron adapter, this owns a public connectOverCDP Browser whose
 * close disconnects Chromium transport without asking the application to quit.
 * The caller retains the existing strict cleanup deadline and force containment.
 */
export async function launchOwnedElectronDiagnostic({ executablePath, args = [], env = process.env, onEvent = () => {} }) {
  if (process.platform !== 'win32') throw new Error('Owned Electron diagnostic requires Windows')
  if (Number(process.versions.node.split('.')[0]) < 22 || typeof WebSocket !== 'function') {
    throw new Error('Owned Electron diagnostic requires Node 22 or later')
  }
  if (typeof executablePath !== 'string' || !win32.isAbsolute(executablePath)
      || !/^[a-z]:[\\/]/i.test(executablePath) || !executablePath.toLowerCase().endsWith('.exe') || !Array.isArray(args)) {
    throw new Error('Owned Electron diagnostic requires a controlled absolute executable path')
  }
  const command = [executablePath, '--inspect=0', '--remote-debugging-port=0', ...args]
    .map(quoteControlledArgument).join(' ')
  // Match Playwright's Electron launch environment without mutating the caller.
  const childEnv = { ...env }
  delete childEnv.NODE_OPTIONS
  const emit = metadata => {
    // Observational logging must neither expose raw argv/stdio nor affect quit.
    try { onEvent(metadata) } catch { /* diagnostics are best effort */ }
  }
  // Match Playwright 1.60's Windows cmd wrapper and five stdio entries.
  const child = spawn(command, [], {
    shell: true, windowsHide: true, env: childEnv,
    stdio: ['ignore', 'pipe', 'pipe', 'pipe', 'pipe'],
  })
  let childClosed = false
  let spawnFailed = false
  const closed = new Promise(resolve => child.once('close', (exitCode, signal) => {
    childClosed = true
    emit({ event: 'owned-child-close', exitCode, signal })
    resolve({ exitCode, signal })
  }))
  child.once('exit', (exitCode, signal) => emit({ event: 'owned-child-exit', exitCode, signal }))
  child.once('error', () => {
    spawnFailed = true
    emit({ event: 'owned-child-spawn-error' })
  })
  let stderr = ''
  child.stdout.on('data', () => {})
  child.stderr.on('data', chunk => {
    // Only endpoint discovery consumes stderr. Never forward product output.
    stderr = (stderr + chunk.toString()).slice(-65_536)
  })
  let inspector, browser, actualPid

  async function containSetupFailure() {
    // Only launch/setup failure may force this still-owned wrapper tree. close()
    // never invokes this path; its caller owns the 100-second cleanup deadline.
    let treeReaped = false
    if (!childClosed && child.exitCode === null && Number.isSafeInteger(child.pid)) {
      emit({ event: 'owned-setup-containment', wrapperPid: child.pid })
      treeReaped = await terminateWindowsProcessTree({
        pid: child.pid,
        timeoutMs: 5_000,
        fallback: () => {
          if (!childClosed && child.exitCode === null) child.kill('SIGKILL')
        },
      })
    }
    await inspector?.close().catch(() => {})
    await bounded(() => browser?.close(), 'Setup CDP disconnect', 5_000).catch(() => {})
    const observedClose = await bounded(() => closed.then(() => true), 'Owned setup process close', 5_000).catch(() => false)
    const electronPidExists = actualPid ? processExists(actualPid) : null
    emit({ event: treeReaped && observedClose && electronPidExists !== true
      ? 'owned-setup-contained' : 'owned-setup-containment-failed',
    treeReaped, childClosed: observedClose, electronPidExists })
  }

  let setupPhase = 'endpoints'
  try {
    const deadline = Date.now() + 15_000
    let endpoints
    while (Date.now() < deadline) {
      if (spawnFailed || childClosed || child.exitCode !== null) throw new Error('Owned Electron exited during setup')
      const node = stderr.match(/^Debugger listening on (ws:\/\/.*)$/m)?.[1]?.trim()
      const chrome = stderr.match(/^DevTools listening on (ws:\/\/.*)$/m)?.[1]?.trim()
      if (node && chrome) {
        endpoints = { node: loopbackEndpoint(node), chrome: loopbackEndpoint(chrome) }
        break
      }
      await delay(25)
    }
    if (!endpoints) throw new Error('Owned Electron endpoint discovery timed out')
    setupPhase = 'inspector'
    inspector = new InspectorConnection(endpoints.node)
    await bounded(() => inspector.opened, 'Node inspector connect', 5_000)
    await inspector.request('Runtime.enable')
    setupPhase = 'evaluate'
    actualPid = await inspector.evaluate(() => process.pid)
    setupPhase = 'connect-cdp'
    browser = await chromium.connectOverCDP(endpoints.chrome, { timeout: 10_000 })
    browser.once('disconnected', () => emit({ event: 'chromium-cdp-disconnected' }))
    setupPhase = 'context'
    const context = browser.contexts()[0]
    if (!context || !Number.isSafeInteger(actualPid) || actualPid < 1 || actualPid === child.pid) {
      throw new Error('Invalid owned Electron process/context identity')
    }
    emit({ event: 'owned-launch', wrapperPid: child.pid, electronPid: actualPid, shell: true })
    let closing
    return {
      process: () => child,
      context: () => context,
      firstWindow: async ({ timeout = 30_000 } = {}) => context.pages()[0] || context.waitForEvent('page', { timeout }),
      evaluate: (fn, arg) => inspector.evaluate(fn, arg),
      close: () => {
        closing ??= (async () => {
          await inspector.evaluate(({ app }) => { setImmediate(() => app.quit()); return 'quit-scheduled' })
          await inspector.close()
          emit({ event: 'node-inspector-disconnected', acknowledged: inspector.didClose })
          await bounded(() => browser.close(), 'Public Chromium transport disconnect', 5_000)
          if (!inspector.didClose || browser.isConnected()) throw new Error('Owned Electron transport remains connected')
          // No lifetime timer, natural-exit timeout, or forced cleanup here.
          // A prevented quit stays pending for the caller's strict outer gate.
          const result = await closed
          if (result.exitCode !== 0 || result.signal !== null) throw new Error('Owned Electron did not exit naturally with zero code')
          while (processExists(child.pid) || processExists(actualPid)) await delay(25)
          const metadata = { ...result, wrapperPid: child.pid, electronPid: actualPid,
            wrapperPidExists: false, electronPidExists: false,
            nodeInspectorClosed: inspector.didClose, chromiumConnected: browser.isConnected(), forced: false }
          emit({ event: 'owned-natural-exit', ...metadata })
          return metadata
        })()
        return closing
      },
    }
  } catch {
    emit({ event: 'owned-setup-failed', phase: setupPhase })
    await containSetupFailure().catch(() => emit({ event: 'owned-setup-containment-failed' }))
    throw new Error('Owned Electron diagnostic launch failed')
  }
}
