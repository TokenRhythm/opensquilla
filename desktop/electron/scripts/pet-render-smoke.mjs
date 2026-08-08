// Renderer smoke test: boot the pet window in isolation with a stubbed
// window.pet bridge, capture any renderer console errors, then exit.
//
// Run: npx electron scripts/pet-render-smoke.mjs
// (app.getAppPath() is the directory of this script, so pet paths are resolved
// relative to scripts/.. — set the CWD to desktop/electron.)

import { app, BrowserWindow, ipcMain } from 'electron'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const petDir = path.join(root, 'pet')
const PRELOAD = path.join(root, 'dist', 'preload.cjs')

const EMPTY_STATS = {
  today: { input: 0, output: 0, cacheCreate: 0, cacheRead: 0, tokens: 0, cost: 0, messages: 0 },
  window5h: { tokens: 0, cost: 0, startTs: 0, resetTs: 0 },
  byModel: {},
  lastOps: [],
  active: null,
  sessions: [],
  waitingCount: 0, needsinputCount: 0, workingCount: 0, jugglingCount: 0,
  sweepingCount: 0, thinkingCount: 0, loafingCount: 0, errorCount: 0,
  todos: [], todosProject: '', hourly: new Array(24).fill(0), hourlyTok: new Array(24).fill(0),
  daily: {}, diagnostics: null, lastActivityTs: 0, idleMs: null,
  bg: { running: 0, zombie: 0, total: 0, items: [] }, context: null,
  codexLimits: null, codexUsage: null, usageProvider: 'squilla', ts: Date.now(),
}

const EMPTY_CONFIG = {
  mode: 'pet', skin: 'cat', petPosition: null, budget5h: 0, muted: false, permHook: '',
  territory: false, territorySupported: false, lootSupported: false, agent: 'squilla',
  petMode: 'single', lang: 'zh', pinnedSessions: [], archivedSessions: [],
  lootCapturedSessions: [], anticsEnabled: false, online: false,
}

const errors = []

app.setName('PetSmoke')
app.whenReady().then(() => {
  // invoke channels the renderer calls at init
  ipcMain.handle('get-config', () => EMPTY_CONFIG)
  ipcMain.handle('get-stats', () => EMPTY_STATS)
  ipcMain.handle('get-win-pos', () => [0, 0])
  ipcMain.handle('get-window-metrics', () => ({ window: { x: 0, y: 0, width: 320, height: 340 }, workArea: { x: 0, y: 0, width: 1920, height: 1080 } }))
  ipcMain.handle('meme-catalog', () => ({ items: [], revision: 0 }))
  ipcMain.handle('travel-get', () => ({ active: null, history: [] }))
  ipcMain.handle('travel-postcards', () => [])
  ipcMain.handle('start-travel', () => ({ ok: false, code: 'offline' }))
  ipcMain.handle('wander-travel', () => ({ ok: false, code: 'offline' }))
  ipcMain.handle('cancel-travel', () => ({ ok: true }))
  ipcMain.handle('meme-trigger', () => ({ ok: false, submitted: false, message: 'offline' }))
  // send channels — register empty handlers so nothing is "missing"
  ipcMain.on('set-ignore-mouse', (_e, ignore) => console.log('[smoke] set-ignore-mouse =', ignore))
  for (const ch of ['set-win-pos', 'open-panel', 'close-panel', 'set-mode', 'set-skin', 'set-budget',
    'toggle-mute', 'set-session-prefs', 'quit-app', 'close-pet', 'new-chat', 'permission-decide',
    'focus-session', 'primary-action', 'set-pet-tall', 'set-pet-big',
    'set-pet-size', 'set-panel-height', 'focus-pet', 'blur-pet', 'open-log', 'pet-log',
    'ui-busy', 'pet-visual-bounds', 'pet-dragging', 'antics-run-now', 'antics-loot', 'antics-toggle']) {
    ipcMain.on(ch, () => {})
  }

  const win = new BrowserWindow({
    width: 320, height: 340, frame: false, transparent: true, show: false,
    webPreferences: { preload: PRELOAD, contextIsolation: true, nodeIntegration: false, sandbox: false },
  })
  win.webContents.on('console-message', (_e, level, message) => {
    const tag = level === 3 ? 'ERROR' : level === 2 ? 'WARN' : 'info'
    console.log(`[renderer:${tag}] ${message}`)
    if (level >= 3) errors.push(message)
  })
  win.webContents.on('did-fail-load', (_e, code, desc) => { console.log(`[load-fail] ${code} ${desc}`) })
  win.loadFile(path.join(petDir, 'pet.html'), { query: { agent: 'all' } })
  win.webContents.on('did-finish-load', () => {
    console.log('[loaded] pet.html')
    win.webContents.executeJavaScript('JSON.stringify({ octo: typeof window.OctoI18n, pet: typeof window.pet, states: typeof window.OctoStates })')
      .then((r) => console.log('[globals]', r))
      .catch((e) => console.log('[globals-err]', e.message))
  })

  // panel window
  const panelWin = new BrowserWindow({
    width: 560, height: 720, show: false,
    webPreferences: { preload: PRELOAD, contextIsolation: true, nodeIntegration: false, sandbox: false },
  })
  panelWin.webContents.on('console-message', (_e, level, message) => {
    const tag = level === 3 ? 'ERROR' : level === 2 ? 'WARN' : 'info'
    console.log(`[panel:${tag}] ${message}`)
    if (level >= 3) errors.push(message)
  })
  panelWin.loadFile(path.join(petDir, 'panel.html'))
  panelWin.webContents.on('did-finish-load', () => console.log('[loaded] panel.html'))

  // Emotion → cat gif: push a say event carrying an emotion and confirm the cat
  // switches to the matching gif.
  setTimeout(async () => {
    win.webContents.send('pet:event', { kind: 'say', text: '抱歉 是我的疏忽', emotion: 'sorry', project: 'x', ts: Date.now() })
    await new Promise((r) => setTimeout(r, 800))
    const src = await win.webContents.executeJavaScript('document.getElementById("cat-img") ? document.getElementById("cat-img").src : "no-cat"')
    console.log('[emotion] cat-img =', src.split('/').pop() || src)
  }, 2500)

  // Hit-test diagnostic: dispatch synthetic mousemove over the cat, expect the
  // renderer to call setIgnoreMouse(false).
  setTimeout(async () => {
    await win.webContents.executeJavaScript(`
      (function(){
        const c = document.getElementById('cat');
        if (!c) return 'no-cat';
        const r = c.getBoundingClientRect();
        const cx = r.left + r.width/2, cy = r.top + r.height/2;
        const el = document.elementFromPoint(cx, cy);
        window.dispatchEvent(new MouseEvent('mousemove', { clientX: cx, clientY: cy, bubbles: true }));
        return JSON.stringify({ catRect: { x: r.left, y: r.top, w: r.width, h: r.height }, hitEl: el ? el.id || el.className : null, inCat: !!(el && el.closest && el.closest('#cat')) });
      })()
    `).then((r) => console.log('[hittest]', r)).catch((e) => console.log('[hittest-err]', e.message))
  }, 4000)

  setTimeout(() => {
    console.log(errors.length ? `[SMOKE-FAIL] ${errors.length} renderer error(s)` : '[SMOKE-PASS] no renderer errors')
    app.exit(errors.length ? 1 : 0)
  }, 8000)
})
