import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { _electron as electron } from 'playwright'

// Exercise the production isolated preload and file broker in Electron. OS
// launch calls are recorded so this offline check never opens a user application.
if (process.platform === 'linux' && !process.env.DISPLAY && !process.env.WAYLAND_DISPLAY
  && process.env.OPENSQUILLA_WORKSPACE_ACTIONS_XVFB !== '1') {
  const result = spawnSync('xvfb-run', ['-a', process.execPath, fileURLToPath(import.meta.url)], {
    env: { ...process.env, OPENSQUILLA_WORKSPACE_ACTIONS_XVFB: '1' }, stdio: 'inherit',
  })
  if (result.error) throw result.error
  process.exit(result.status ?? 1)
}
const root = await mkdtemp(join(tmpdir(), 'opensquilla-workspace-actions-electron-'))
let app
try {
  await writeFile(join(root, 'build.py'), 'print("workspace fixture")\n')
  await writeFile(join(root, 'main.mjs'), `
import { app, BrowserWindow, ipcMain } from 'electron'
import { lstat, realpath } from 'node:fs/promises'
import { join } from 'node:path'
import { performWorkspaceFileAction } from ${JSON.stringify(new URL('../dist/resource-file-actions.js', import.meta.url).href)}
const workspace = await realpath(${JSON.stringify(root)})
const sourcePath = join(workspace, 'build.py')
const info = await lstat(sourcePath, { bigint: true })
const identity = Object.fromEntries(['dev', 'ino', 'size', 'mtimeNs', 'ctimeNs'].map(key => [key, String(info[key])]))
let connection = { instanceId: 'fixture-instance', profile: 'fixture-profile',
  url: 'http://127.0.0.1:54321', authToken: 'fixture-token', nonce: 'fixture-private-nonce' }
globalThis.workspaceTest = { mode: 'local', calls: [], requests: [] }
let window
ipcMain.handle('desktop:workspace-file:action', async (event, payload) => {
  if (event.sender !== window?.webContents || event.senderFrame !== event.sender.mainFrame) throw new Error('Untrusted sender')
  const state = globalThis.workspaceTest
  return performWorkspaceFileAction(payload, {
    connection: () => state.mode === 'remote' || state.mode === 'disconnected' ? null : connection,
    fetch: async (url, options) => {
      state.requests.push({ path: new URL(url).searchParams.get('path'), binding: new URL(url).searchParams.get('workspaceBinding'),
        session: options.headers['x-opensquilla-session-key'], signed: /^[a-f0-9]{64}$/.test(options.headers['x-opensquilla-native-signature']) })
      if (state.mode === 'switched') connection = { ...connection, instanceId: 'replacement-instance' }
      return Response.json({ relativePath: 'build.py', workspaceBinding: 'fixture-binding', workspace, sourcePath, identity })
    },
    openPath: async path => { if (state.mode === 'failure') return 'launch failed'; state.calls.push({ action: 'open', name: path.split(/[\\\\/]/).pop() }); return '' },
    reveal: path => state.calls.push({ action: 'reveal', name: path.split(/[\\\\/]/).pop() }),
  }).catch(() => { throw new Error('Workspace file action failed') })
})
void app.whenReady().then(async () => {
  window = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, nodeIntegration: false,
    sandbox: true, preload: ${JSON.stringify(fileURLToPath(new URL('../dist/preload.cjs', import.meta.url)))} } })
  await window.loadURL('data:text/html,<title>Workspace file actions fixture</title>')
})
`)
  app = await electron.launch({ timeout: 30_000, args: [`--user-data-dir=${join(root, 'chromium')}`, join(root, 'main.mjs')],
    env: { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' } })
  const page = await app.firstWindow({ timeout: 30_000 })
  const request = { gatewayInstanceId: 'fixture-instance', sessionKey: 'fixture-session',
    path: 'build.py', workspaceBinding: 'fixture-binding', action: 'open' }
  for (const action of ['open', 'reveal']) {
    assert.deepEqual(await page.evaluate(payload => window.opensquillaDesktop.workspaceFileAction(payload), { ...request, action }), { ok: true })
  }
  const state = await app.evaluate(() => globalThis.workspaceTest)
  assert.deepEqual(state.calls, [{ action: 'open', name: 'build.py' }, { action: 'reveal', name: 'build.py' }])
  assert.deepEqual(state.requests, [0, 1].map(() => ({ path: 'build.py', binding: 'fixture-binding', session: 'fixture-session', signed: true })))
  for (const mode of ['remote', 'disconnected', 'failure', 'switched']) {
    await app.evaluate((_electron, value) => { globalThis.workspaceTest.mode = value }, mode)
    const result = await page.evaluate(async payload => {
      try { await window.opensquillaDesktop.workspaceFileAction(payload); return 'unexpected success' }
      catch (error) { return error.message }
    }, request)
    assert.match(result, /Workspace file action failed/)
    assert.ok(!result.includes(root))
  }
  assert.equal((await app.evaluate(() => globalThis.workspaceTest.calls)).length, 2)
  assert.equal(await page.evaluate(() => 'ipcRenderer' in window.opensquillaDesktop), false)
  console.log('Electron workspace files: isolated preload, signed broker, open/reveal, owner changes and failures passed')
} finally {
  await app?.close()
  await rm(root, { recursive: true, force: true })
}
