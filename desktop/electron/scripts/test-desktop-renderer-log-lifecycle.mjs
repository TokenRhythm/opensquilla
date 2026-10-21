import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createMainWindowConsoleListener } from './renderer-log-test-helpers.mjs'

if (!process.versions.electron) {
  const { default: electronBinary } = await import('electron')
  const userData = mkdtempSync(join(tmpdir(), 'opensquilla-renderer-log-lifecycle-'))
  const env = {
    ...process.env,
    ELECTRON_DISABLE_SECURITY_WARNINGS: 'true',
    OPENSQUILLA_RENDERER_LOG_TEST_USER_DATA: userData,
  }
  delete env.ELECTRON_RUN_AS_NODE
  try {
    const script = fileURLToPath(import.meta.url)
    const needsDisplay = process.platform === 'linux' && !env.DISPLAY && !env.WAYLAND_DISPLAY
    const result = spawnSync(
      needsDisplay ? 'xvfb-run' : electronBinary,
      needsDisplay ? ['-a', electronBinary, script] : [script],
      { env, stdio: 'inherit', timeout: 30_000, windowsHide: true },
    )
    if (result.error) throw result.error
    assert.equal(result.status, 0, 'Renderer log lifecycle regression failed.')
  } finally {
    rmSync(userData, { recursive: true, force: true })
  }
} else {
  const { app, BrowserWindow } = await import('electron')
  app.setPath('userData', process.env.OPENSQUILLA_RENDERER_LOG_TEST_USER_DATA)
  if (process.platform === 'darwin') app.setActivationPolicy('prohibited')
  app.commandLine.appendSwitch('disable-gpu')
  app.on('window-all-closed', () => {})
  void (async () => {
    let window
    try {
      await app.whenReady()
      window = new BrowserWindow({
        show: false,
        webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
      })
      await window.loadURL('data:text/html,<title>Console lifecycle fixture</title><iframe srcdoc="child"></iframe>')
      const contents = window.webContents
      const mainFrame = contents.mainFrame
      const childFrame = mainFrame.frames[0]
      assert.ok(childFrame, 'The fixture must exercise a real child frame.')
      const records = []
      const listener = createMainWindowConsoleListener(window, records)
      let deliveries = 0
      const deliver = (details) => {
        deliveries += 1
        listener(details)
      }
      contents.on('console-message', deliver)
      const message = {
        frame: mainFrame, level: 'error', message: 'synthetic renderer lifecycle error',
        sourceId: 'https://example.test/main.js', lineNumber: 1,
      }
      assert.equal(contents.emit('console-message', message), true)
      assert.equal(records.length, 1)
      contents.emit('console-message', { ...message, frame: childFrame })
      assert.equal(deliveries, 2)
      assert.equal(records.length, 1, 'Child frames must remain excluded.')

      // Full document navigation must neither reset the main-owned recovery
      // budget nor let distinct chats consume the genuine error allowance.
      for (let chat = 0; chat < 20; chat++) {
        await window.loadURL(`data:text/html,<title>Chat document ${chat}</title>`)
        contents.emit('console-message', {
          ...message, frame: contents.mainFrame, level: 'warning',
          message: '[opensquilla-recovery] ' + JSON.stringify({
            phase: 'failed', reason: 'metadata_hydration_failed',
            session_key_sha256: chat.toString(16).padStart(16, '0'), error_kind: 'busy',
          }),
        })
      }
      assert.equal(records.filter(record => record.event === 'session_recovery').length, 20)
      for (let reload = 0; reload < 10; reload++) {
        await window.loadURL(`data:text/html,<title>Same chat reload ${reload}</title>`)
        contents.emit('console-message', {
          ...message, frame: contents.mainFrame, level: 'warning',
          message: '[opensquilla-recovery] ' + JSON.stringify({
            phase: 'failed', reason: 'metadata_hydration_failed',
            session_key_sha256: '0000000000000000', error_kind: 'busy',
          }),
        })
      }
      assert.equal(records.filter(record => record.event === 'session_recovery').length, 27)
      contents.emit('console-message', { ...message, frame: contents.mainFrame })
      assert.equal(records.filter(record => record.event === 'renderer_console').length, 2)
      const recordsBeforeDestroy = records.length
      const deliveriesBeforeDestroy = deliveries

      await new Promise(resolveDestroyed => {
        contents.once('destroyed', resolveDestroyed)
        window.destroy()
      })
      assert.equal(window.isDestroyed(), true)
      assert.equal(contents.isDestroyed(), true)
      assert.throws(() => contents.mainFrame, /Object has been destroyed/)
      // Electron may remove event listeners during destruction. Invoke the
      // saved production callback to exercise an already queued late delivery.
      assert.doesNotThrow(() => deliver(message))
      assert.equal(deliveries, deliveriesBeforeDestroy + 1)
      assert.equal(records.length, recordsBeforeDestroy, 'A late error must not log after destruction.')
      console.log('desktop renderer log lifecycle: real Electron teardown passed.')
      app.exit(0)
    } catch (error) {
      console.error(error)
      if (window && !window.isDestroyed()) window.destroy()
      app.exit(1)
    }
  })()
}
