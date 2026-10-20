import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { setTimeout as delay } from 'node:timers/promises'
import { _electron as electron } from 'playwright'

async function freePort() {
  const server = createServer()
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  await new Promise(resolve => server.close(resolve))
  return port
}

function inspectorConnection(url) {
  const socket = new WebSocket(url)
  const pending = new Map()
  const scripts = new Map()
  let sequence = 0, paused
  let resolvePaused
  const pausedPromise = new Promise(resolve => { resolvePaused = resolve })
  socket.addEventListener('message', event => {
    const value = JSON.parse(String(event.data))
    if (value.id) {
      const entry = pending.get(value.id)
      if (!entry) return
      pending.delete(value.id)
      clearTimeout(entry.timeout)
      if (value.error) entry.reject(new Error(value.error.message))
      else entry.resolve(value.result)
    } else if (value.method === 'Debugger.paused') {
      paused = value.params
      resolvePaused(paused)
    } else if (value.method === 'Debugger.scriptParsed') {
      scripts.set(value.params.scriptId, value.params.url)
    }
  })
  return {
    open: () => new Promise((resolve, reject) => {
      socket.addEventListener('open', resolve, { once: true })
      socket.addEventListener('error', reject, { once: true })
    }),
    send(method, params = {}) {
      const id = ++sequence
      return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          pending.delete(id)
          reject(new Error(`Inspector ${method} timed out`))
        }, 10_000)
        pending.set(id, { resolve, reject, timeout })
        socket.send(JSON.stringify({ id, method, params }))
      })
    },
    async paused() {
      if (paused) return paused
      let timer
      try {
        return await Promise.race([
          pausedPromise,
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Entry breakpoint not reached')), 10_000) }),
        ])
      } finally { clearTimeout(timer) }
    },
    close: () => socket.close(),
    entryFrames: frames => frames.slice(0, 3).map(frame => ({
      url: frame.url || scripts.get(frame.location.scriptId) || '',
      lineNumber: frame.location.lineNumber, functionName: frame.functionName,
    })),
  }
}

/**
 * Packaged Electron ignores CLI -r. Pause its actual entry point before app
 * startup, then install only the OS protocol-registration guard. The exact
 * packaged files remain unchanged. Never resume without a verified guard.
 *
 * Playwright 1.64 connects its inspector first, but its Electron launcher waits
 * for Chromium before initialize(); initialize only evaluates __playwright_run.
 * This separate connection owns the entry breakpoint and the guarded resume.
 */
export async function launchWithProtocolRegistrationGuard(options, guardPath) {
  const port = await freePort()
  const launch = electron.launch({
    ...options,
    args: [`--inspect-brk=127.0.0.1:${port}`, ...(options.args || [])],
  })
  // Attach a rejection handler immediately while the separately paused startup
  // is inspected, so an early launch failure never becomes unhandled.
  launch.catch(() => {})
  let connection
  try {
    const deadline = Date.now() + 60_000
    let url
    while (!url && Date.now() < deadline) {
      try {
        const response = await fetch(`http://127.0.0.1:${port}/json/list`)
        const targets = await response.json()
        url = targets.find(target => target.type === 'node')?.webSocketDebuggerUrl
      } catch { /* The new process has not opened its loopback inspector yet. */ }
      if (!url) await delay(100)
    }
    assert.ok(url, 'the disposable Electron inspector must open')
    connection = inspectorConnection(url)
    await connection.open()
    await connection.send('Runtime.enable')
    await connection.send('Debugger.enable')
    await connection.send('Runtime.runIfWaitingForDebugger')
    const paused = await connection.paused()
    const source = await readFile(guardPath, 'utf8')
    const installed = await connection.send('Runtime.evaluate', {
      expression: `(() => {\n${source}\nconst descriptor = Object.getOwnPropertyDescriptor(app, 'setAsDefaultProtocolClient'); return { guard: globalThis.__packagedRecoveryIsolation, ready: app.isReady(), isPackaged: app.isPackaged, descriptor: { writable: descriptor.writable, configurable: descriptor.configurable, type: typeof descriptor.value }, pid: process.pid }\n})()`,
      includeCommandLineAPI: true,
      returnByValue: true,
    })
    assert.equal(installed.exceptionDetails, undefined, 'OS guard evaluation must succeed before resume')
    const value = installed.result?.value
    assert.equal(value?.ready, false, 'app.ready must not have fired before the guard')
    assert.equal(value?.guard?.protocolRegistrationAttempts, 0)
    assert.deepEqual(value?.descriptor, { writable: false, configurable: false, type: 'function' })
    assert.ok(Number.isInteger(value.pid))
    const entryFrames = connection.entryFrames(paused.callFrames)
    // This is the only resume call, after the verified guard is installed.
    await connection.send('Debugger.resume')
    connection.close()
    connection = null
    const app = await launch
    return { app, startupGuard: {
      method: 'inspector-entry-breakpoint',
      entryPauseReason: paused.reason,
      entryFrames,
      appReadyBeforeResume: value.ready,
      appIsPackagedAtBreakpoint: value.isPackaged,
      guardDescriptor: value.descriptor,
      protocolRegistrationAttemptsBeforeResume: value.guard.protocolRegistrationAttempts,
      pid: value.pid,
    } }
  } catch (error) {
    // Exit while still paused; never resume an unguarded application. The
    // Playwright launch deadline is an additional scoped process-tree backstop.
    if (connection) {
      await connection.send('Runtime.evaluate', {
        expression: 'process.exit(86)', includeCommandLineAPI: true,
      }).catch(() => {})
      connection.close()
    }
    await launch.catch(() => {})
    throw error
  }
}
