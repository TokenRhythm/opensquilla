import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'

// Inject a crash into the packaged renderer and observe the production
// main process's bounded reload. The test helper does not reload the window.
export async function crashAndReloadRenderer(app, page, waitForRecovery) {
  const window = await app.browserWindow(page)
  const before = await window.evaluate(window => ({
    webContentsId: window.webContents.id,
    rendererPid: window.webContents.getOSProcessId(),
    url: window.webContents.getURL(),
  }))
  assert.ok(before.rendererPid > 0)
  const sentinel = randomUUID()
  await page.evaluate(value => { window.__rendererFaultDocument = value }, sentinel)
  const crashed = page.waitForEvent('crash', { timeout: 15_000 })
  await window.evaluate(window => window.webContents.forcefullyCrashRenderer()).catch(() => undefined)
  await crashed
  // Recovery belongs to the production main-process handler. Waiting on its
  // durable log avoids evaluating through Playwright's intentionally dead
  // target after a crash; the callback must prove renderer_interactive and
  // renderer_recovery_ready for this generation.
  const recovery = await waitForRecovery()
  const evidence = { injected: true, crashObserved: true,
    recovery: 'main_process_bounded_reload', oldRendererPid: before.rendererPid,
    newRendererPid: recovery.newRendererPid ?? null, sameWindow: recovery.sameWindow === true,
    sameRoute: recovery.sameRoute === true, documentRebuilt: recovery.documentRebuilt === true,
    recoveryGeneration: recovery.generation, routeBeforeCrash: recovery.routeBeforeCrash || null,
    recoveredRoute: recovery.recoveredRoute || null, sessionKey: recovery.sessionKey || null,
    recoveredSessionKey: recovery.recoveredSessionKey || null }
  validateRendererFaultEvidence(evidence)
  return { evidence }
}

// Kill the renderer from outside Electron.  forcefullyCrashRenderer() exercises
// Chromium's crash path; this probe must also cover the Windows process-kill
// path that reports render-process-gone.reason = "killed".  The exact PID was
// read from the BrowserWindow that Playwright attached to, so taskkill cannot
// target the Gateway or the Electron main process by accident.
export async function hardKillAndReloadRenderer(app, page, waitForRecovery) {
  const window = await app.browserWindow(page)
  const before = await window.evaluate(window => ({
    webContentsId: window.webContents.id,
    rendererPid: window.webContents.getOSProcessId(),
    url: window.webContents.getURL(),
  }))
  assert.ok(before.rendererPid > 0)
  const sentinel = randomUUID()
  await page.evaluate(value => { window.__rendererFaultDocument = value }, sentinel)
  const gone = page.waitForEvent('crash', { timeout: 15_000 })
  const kill = spawnSync('taskkill', ['/PID', String(before.rendererPid), '/F'], {
    encoding: 'utf8',
    windowsHide: true,
  })
  assert.equal(kill.error, undefined, `taskkill failed to start: ${kill.error?.message || ''}`)
  assert.equal(kill.status, 0, `taskkill did not terminate renderer PID ${before.rendererPid}`)
  await gone
  const recovery = await waitForRecovery()
  const evidence = { injected: true, hardKill: true, killCommand: 'taskkill /PID /F',
    killExitCode: kill.status, crashObserved: true,
    processGoneReason: recovery.processGoneReason || null,
    recovery: 'main_process_bounded_reload', oldRendererPid: before.rendererPid,
    newRendererPid: recovery.newRendererPid ?? null, sameWindow: recovery.sameWindow === true,
    sameRoute: recovery.sameRoute === true, documentRebuilt: recovery.documentRebuilt === true,
    recoveryGeneration: recovery.generation, routeBeforeCrash: recovery.routeBeforeCrash || null,
    recoveredRoute: recovery.recoveredRoute || null, sessionKey: recovery.sessionKey || null,
    recoveredSessionKey: recovery.recoveredSessionKey || null }
  validateRendererFaultEvidence(evidence)
  assert.equal(evidence.hardKill, true)
  assert.equal(evidence.killExitCode, 0)
  assert.equal(evidence.processGoneReason, 'killed')
  return { evidence }
}

export function validateRendererFaultEvidence(value) {
  assert.equal(value.crashObserved, true, 'Renderer crash must actually be observed')
  assert.ok(value.oldRendererPid > 0)
  assert.ok(value.newRendererPid > 0)
  assert.notEqual(value.oldRendererPid, value.newRendererPid, 'Renderer must use a new OS process')
  assert.equal(value.sameWindow, true, 'Renderer recovery must retain the native window')
  assert.equal(value.sameRoute, true, 'Renderer recovery must retain the session route')
  assert.equal(value.documentRebuilt, true, 'Renderer recovery must rebuild the document')
}
