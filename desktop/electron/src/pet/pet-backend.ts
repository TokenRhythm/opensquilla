// PetBackend — orchestrates the embedded OpenSquilla desktop pet.
//
// Owns the pet + panel BrowserWindows, the `window.pet` IPC bridge, the pet
// tray, and the backend modules (core state machine, gateway watcher, approval
// cards, metering, launch actions). It is wired into desktop/electron/src/main.ts
// once the gateway is ready.

import { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, screen, clipboard, shell } from 'electron'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createPetCore, PetCore } from './core.js'
import { createSquillaWatch } from './squilla-watch.js'
import { createPermissions } from './permission.js'
import { createPetMetering } from './metering.js'
import { createLaunch } from './launch.js'
import { getPetConfig, savePetConfig } from './config.js'
import { loadPetI18n } from './i18n.js'
import { buildPetStats, activityToEvents } from './adapter.js'
import { PetEvent, PetStats, PetConfig, PendingApproval } from './types.js'
import { createAntics } from './antics.js'
import { setWin32Log } from './win32.js'

// ESM: __dirname is not defined in the compiled module — derive it from this
// file's location (dist/pet/), so the preload resolves to dist/preload.cjs.
const here = path.dirname(fileURLToPath(import.meta.url))

// Minimal file logger (diagnostics: renderer console + click-through state).
function petLog(msg: string): void {
  try {
    fs.appendFileSync(path.join(app.getPath('userData'), 'pet.log'), `${new Date().toISOString()} ${msg}\n`)
  } catch {}
}

export interface PetBackendDeps {
  gatewayUrl: string
  token?: string
  version: string
  getMainWindow: () => BrowserWindow | null
  /** Called when the pet menu needs the shell to rebuild its tray. */
  onTrayChanged?: () => void
}

const BASE_W = 320
const BASE_H = 340

export function createPetBackend(deps: PetBackendDeps) {
  const appRoot = app.getAppPath()
  const petDir = path.join(appRoot, 'pet')
  const PRELOAD = path.join(here, '..', 'preload.cjs')

  let petWin: BrowserWindow | null = null
  let panelWin: BrowserWindow | null = null
  // Note: no pet-owned tray. Menu items flow into the OpenSquilla shell tray
  // via getTrayMenuItems() (registered on the backend return object).
  let tray: Tray | null = null // kept for legacy null-checks; never assigned.
  let core: PetCore | null = null
  let watch: ReturnType<typeof createSquillaWatch> | null = null
  let permissions: ReturnType<typeof createPermissions> | null = null
  let metering: ReturnType<typeof createPetMetering> | null = null
  let launch: ReturnType<typeof createLaunch> | null = null
  let lastStats: PetStats | null = null
  let emitTimer: ReturnType<typeof setTimeout> | null = null
  let statsTimer: ReturnType<typeof setInterval> | null = null
  let online = false
  let stopped = false
  let ipcRegistered = false
  const recentOps: PetStats['lastOps'] = []
  interface AnticsApi {
    runNow?: () => void
    perchAt?: (x: number, y: number) => void
  }
  let anticsApi: AnticsApi | null = null
  let antics: ReturnType<typeof createAntics> | null = null
  // Attention-seeking: track the last done event + the last time the user
  // showed any signs of life in the shell UI. When both grow stale, the pet
  // nudges the user — see antics.attentionTick().
  let lastDoneTs = 0
  let lastDoneText = ''
  let lastUserActivityTs = Date.now()
  let focusBlurHandlers: { focus: () => void; blur: () => void } | null = null

  // ── click-through (transparent pet window) ────────────────────────────────
  // The renderer reports the pet's visible rect + whether a popup is open, and
  // the main process polls the real cursor position to decide pass-through. This
  // does NOT rely on Electron's setIgnoreMouseEvents forward (which is flaky on
  // Windows/transparent windows) — the pet is clickable over its body and over
  // any open popup, transparent elsewhere.
  let petVisualRect: { x: number; y: number; width: number; height: number } | null = null
  let uiBusy = false
  let dragging = false
  let ignoreTimer: ReturnType<typeof setInterval> | null = null
  let lastIgnoreState: boolean | null = null

  function applyIgnore(inside: boolean): void {
    // No isVisible() guard here: on Windows a transparent always-on-top pet
    // window can report isVisible()==false even though it's on screen, and that
    // guard silently kept the window in its initial pass-through state forever.
    if (!petWin || petWin.isDestroyed()) return
    const ignore = !inside
    if (ignore === lastIgnoreState) return
    lastIgnoreState = ignore
    petLog(`[click] applyIgnore inside=${inside}`)
    try { petWin.setIgnoreMouseEvents(ignore, { forward: true }) } catch (e) { petLog(`[click] setIgnoreMouseEvents threw: ${String(e)}`) }
  }

  function ignoreTick(): void {
    if (!petWin || petWin.isDestroyed()) return
    // While dragging (or a popup is open) the whole window must stay clickable.
    if (uiBusy || dragging) { applyIgnore(true); return }
    if (!petVisualRect || petVisualRect.width <= 0 || petVisualRect.height <= 0) {
      // No body rect yet — stay clickable so the user isn't stuck with an
      // inert pet; the renderer's first report will tighten it.
      applyIgnore(true)
      return
    }
    const b = petWin.getBounds()
    const absX = b.x + petVisualRect.x
    const absY = b.y + petVisualRect.y
    const p = screen.getCursorScreenPoint()
    const inside = p.x >= absX && p.x <= absX + petVisualRect.width
      && p.y >= absY && p.y <= absY + petVisualRect.height
    applyIgnore(inside)
  }

  // ── antics (Windows mischief mode) wiring ─────────────────────────────────
  function syncAntics(): void {
    const enabled = getPetConfig().anticsEnabled === true
    const hasPet = !!(petWin && !petWin.isDestroyed())
    // The antics instance is always created on Windows so the one-shot 巡视
    // action (anticsApi) works regardless of the continuous mischief toggle;
    // only the continuous crawl/nab/presence behaviors require anticsEnabled.
    if (process.platform === 'win32' && hasPet) {
      if (!antics) {
        try {
          antics = createAntics({
            getPetWin: () => petWin,
            getPetVisualRect: () => petVisualRect,
            emit: (ev) => sendPet('pet:event', ev),
            shouldAbort: () => dragging,
            log: petLog,
            getLastDone: () => ({ ts: lastDoneTs, text: lastDoneText }),
            getLastUserActivity: () => lastUserActivityTs,
            markUserSaw: () => {
              // Once the user actually engaged, clear the pending done so we
              // don't nudge again until the next assistant turn finishes.
              lastDoneTs = 0
              lastDoneText = ''
              lastUserActivityTs = Date.now()
            },
            getMischiefEnabled: () => getPetConfig().anticsEnabled === true,
            showPetBubble: (text) => sendPet('pet:bubble', { text, ts: Date.now() }),
          })
        } catch {
          antics = null
        }
      }
      if (antics) {
        try { enabled ? antics.start() : antics.stop() } catch {}
      }
    } else if (antics) {
      try { antics.stop() } catch {}
      antics = null
    }
    anticsApi = antics ? {
      runNow: () => { void antics!.runNow() },
      perchAt: (x: number, y: number) => { void antics!.perchAt(x, y) },
    } : null
  }

  const t = () => loadPetI18n()
  const lang = () => loadPetI18n().getLang()

  // ── frontend config shape ─────────────────────────────────────────────────
  function frontendConfig(): PetConfig {
    const c = getPetConfig()
    return {
      mode: c.mode,
      skin: c.skin,
      petPosition: c.petPosition,
      budget5h: 0,
      muted: c.muted,
      permHook: '',
      territory: c.anticsEnabled,
      territorySupported: process.platform === 'win32',
      agent: 'squilla',
      petMode: 'single',
      lang: 'zh',
      pinnedSessions: c.pinnedSessions || [],
      archivedSessions: c.archivedSessions || [],
      anticsEnabled: c.anticsEnabled,
      online,
    }
  }

  // ── windows ───────────────────────────────────────────────────────────────
  // Verify a saved window rect actually intersects some display's work area
  // (with a generous margin). Prevents the pet from booting off-screen after
  // a crawl / drag / display change.
  function isPointVisible(x: number, y: number): boolean {
    try {
      const displays = screen.getAllDisplays()
      const rect = { x, y, width: BASE_W, height: BASE_H }
      for (const d of displays) {
        const wa = d.workArea
        const overlapX = Math.max(0, Math.min(rect.x + rect.width, wa.x + wa.width) - Math.max(rect.x, wa.x))
        const overlapY = Math.max(0, Math.min(rect.y + rect.height, wa.y + wa.height) - Math.max(rect.y, wa.y))
        // Need at least ~80px of visible area on some display.
        if (overlapX > 80 && overlapY > 80) return true
      }
    } catch {}
    return false
  }

  function defaultCorner(): { x: number; y: number } {
    try {
      const wa = screen.getPrimaryDisplay().workArea
      return { x: wa.x + wa.width - BASE_W - 24, y: wa.y + wa.height - BASE_H - 24 }
    } catch { return { x: 100, y: 100 } }
  }

  function createPetWindow(): BrowserWindow {
    const c = getPetConfig()
    let x = 0
    let y = 0
    if (c.petPosition && isPointVisible(c.petPosition.x, c.petPosition.y)) {
      x = c.petPosition.x
      y = c.petPosition.y
    } else {
      const d = defaultCorner()
      x = d.x
      y = d.y
      if (c.petPosition && !isPointVisible(c.petPosition.x, c.petPosition.y)) {
        petLog(`[start] saved petPosition offscreen (${c.petPosition.x},${c.petPosition.y}) → reset to ${x},${y}`)
        savePetConfig({ petPosition: { x, y } })
      }
    }
    const win = new BrowserWindow({
      width: BASE_W,
      height: BASE_H,
      x, y,
      frame: false,
      transparent: true,
      hasShadow: false,
      resizable: false,
      alwaysOnTop: true,
      skipTaskbar: true,
      fullscreenable: false,
      webPreferences: {
        preload: PRELOAD,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: false,
        autoplayPolicy: 'no-user-gesture-required',
      },
    })
    petWin = win
    win.setAlwaysOnTop(true, 'floating')
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    win.webContents.on('will-navigate', (e, url) => {
      if (!url.startsWith('file://')) e.preventDefault()
    })
    win.webContents.on('console-message', (_e, level, message) => {
      petLog(`[renderer:${level}] ${message}`)
    })
    win.setIgnoreMouseEvents(true, { forward: true })
    win.loadFile(path.join(petDir, 'pet.html'), { query: { agent: 'all' } })
    win.webContents.on('did-finish-load', () => {
      sendWin(win, 'pet:config', frontendConfig())
      if (lastStats) sendWin(win, 'pet:stats', lastStats)
    })
    win.on('closed', () => { petWin = null })
    win.on('moved', () => {
      if (win.isDestroyed()) return
      const b = win.getBounds()
      // Only persist a position we can actually recover to. If a crawl / drag
      // parked the pet in a bad spot, next launch will still boot in the
      // default corner instead of a hidden one.
      if (isPointVisible(b.x, b.y)) savePetConfig({ petPosition: { x: b.x, y: b.y } })
    })
    // Make sure the window is on-screen right after load, in case a runtime
    // motion left it in a bad spot before the save-throttle fired.
    win.once('ready-to-show', () => {
      const b = win.getBounds()
      if (!isPointVisible(b.x, b.y)) {
        const d = defaultCorner()
        petLog(`[start] ready-to-show fixed bad pos ${b.x},${b.y} → ${d.x},${d.y}`)
        win.setBounds({ x: d.x, y: d.y, width: b.width, height: b.height })
        savePetConfig({ petPosition: d })
      }
      try { win.show() } catch {}
    })
    return win
  }

  /** Force the pet back into view — used by tray "显示桌宠" and by re-init. */
  function showPetSafely(): void {
    if (!petWin || petWin.isDestroyed()) {
      if (getPetConfig().petEnabled !== false) createPetWindow()
      return
    }
    const b = petWin.getBounds()
    if (!isPointVisible(b.x, b.y)) {
      const d = defaultCorner()
      petWin.setBounds({ x: d.x, y: d.y, width: b.width, height: b.height })
      savePetConfig({ petPosition: d })
    }
    try { petWin.show(); petWin.setAlwaysOnTop(true, 'floating'); petWin.moveTop() } catch {}
    // Restore the mischief/behavior loop that closePet stopped on hide.
    syncAntics()
  }

  function openPanel(): void {
    if (panelWin && !panelWin.isDestroyed()) { panelWin.show(); panelWin.focus(); return }
    panelWin = new BrowserWindow({
      width: 560,
      height: 720,
      frame: false,
      transparent: false,
      resizable: true,
      skipTaskbar: false,
      show: false,
      backgroundColor: '#2c1f1a',
      webPreferences: {
        preload: PRELOAD,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: false,
      },
    })
    panelWin.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    panelWin.webContents.on('will-navigate', (e, url) => {
      if (!url.startsWith('file://')) e.preventDefault()
    })
    panelWin.loadFile(path.join(petDir, 'panel.html'))
    panelWin.webContents.on('did-finish-load', () => {
      sendPanel('panel:config', frontendConfig())
      if (lastStats) sendPanel('panel:stats', lastStats)
      setTimeout(() => { try { if (panelWin && !panelWin.isDestroyed()) panelWin.show() } catch {} }, 90)
    })
    panelWin.on('closed', () => { panelWin = null })
  }

  // ── push helpers ──────────────────────────────────────────────────────────
  function sendWin(win: BrowserWindow | null, channel: string, payload: unknown): void {
    if (win && !win.isDestroyed()) { try { win.webContents.send(channel, payload) } catch {} }
  }
  function sendPet(channel: string, payload: unknown): void { sendWin(petWin, channel, payload) }
  function sendPanel(channel: string, payload: unknown): void { sendWin(panelWin, channel, payload) }

  function recordOp(ev: PetEvent): void {
    if (ev.kind === 'operation') {
      recentOps.unshift({ tool: ev.tool || '', icon: ev.icon || '🔧', detail: ev.detail || '', file: '', project: ev.project, agent: 'squilla', ts: ev.ts })
    } else if (ev.kind === 'say') {
      recentOps.unshift({ tool: 'say', icon: '💬', detail: ev.text || '', file: '', project: ev.project, agent: 'squilla', ts: ev.ts })
    } else return
    if (recentOps.length > 50) recentOps.length = 50
  }

  function emitStats(): void {
    if (!core) return
    const snap = core.buildSnapshot()
    lastStats = buildPetStats(snap, permissions ? permissions.getPending() : [], metering ? metering.getStats() : null, { lastOps: recentOps })
    sendPet('pet:stats', lastStats)
    sendPanel('panel:stats', lastStats)
  }
  function scheduleEmit(): void {
    if (emitTimer) return
    emitTimer = setTimeout(() => { emitTimer = null; emitStats() }, 150)
  }

  function onActivity(act: any): void {
    if (act.event === 'SessionEnd') permissions?.sweepForSessionEvent(act.session.id)
    for (const ev of activityToEvents(act)) {
      recordOp(ev)
      sendPet('pet:event', ev)
    }
  }

  // ── IPC (mirrors OpenSquilla pet preload.js, minus claude/codex) ───────────────────
  function registerIpc(): void {
    if (ipcRegistered) return
    ipcRegistered = true

    ipcMain.handle('get-config', () => frontendConfig())
    ipcMain.handle('get-stats', () => lastStats || buildStatsNow())
    ipcMain.handle('get-win-pos', () => {
      if (petWin && !petWin.isDestroyed()) { const b = petWin.getBounds(); return [b.x, b.y] }
      return [0, 0]
    })
    ipcMain.handle('get-window-metrics', () => {
      if (!petWin || petWin.isDestroyed()) return null
      const b = petWin.getBounds()
      let workArea = null
      try { workArea = screen.getDisplayMatching(b).workArea } catch {}
      return { window: b, workArea }
    })
    ipcMain.on('set-win-pos', (_e, x: number, y: number) => {
      if (petWin && !petWin.isDestroyed() && Number.isFinite(x) && Number.isFinite(y)) {
        const b = petWin.getBounds()
        petWin.setBounds({ x: Math.round(x), y: Math.round(y), width: b.width, height: b.height })
      }
    })
    ipcMain.on('open-panel', openPanel)
    ipcMain.on('close-panel', () => { if (panelWin && !panelWin.isDestroyed()) panelWin.close() })
    ipcMain.on('set-panel-height', (_e, h: number) => {
      if (!panelWin || panelWin.isDestroyed() || !Number.isFinite(h)) return
      const b = panelWin.getBounds()
      const wa = screen.getDisplayMatching(b).workArea
      panelWin.setBounds({ x: b.x, y: b.y, width: b.width, height: Math.max(320, Math.min(Math.round(h), wa.height - 24)) })
    })
    ipcMain.on('set-mode', (_e, mode: string) => applyMode(mode))
    ipcMain.on('set-skin', (_e, skin: string) => { savePetConfig({ skin }); broadcastConfig() })
    ipcMain.on('set-budget', () => {})
    ipcMain.on('toggle-mute', () => { savePetConfig({ muted: !getPetConfig().muted }); broadcastConfig(); refreshTray() })
    ipcMain.on('set-session-prefs', (_e, pinned: string[], archived: string[]) => {
      savePetConfig({ pinnedSessions: pinned, archivedSessions: archived })
      broadcastConfig()
    })
    ipcMain.on('quit-app', () => app.quit())
    ipcMain.on('close-pet', () => {
      // Hide (not destroy) so the window keeps its click-through state — a
      // destroyed + recreated window loses the renderer's petVisualRect until
      // it re-reports, leaving it click-through and breaking right-click.
      if (petWin && !petWin.isDestroyed()) petWin.hide()
      if (antics) { try { antics.stop() } catch {} }
    })

    ipcMain.on('new-chat', () => { void newChat() })
    ipcMain.on('permission-decide', (_e, permId: string, behavior: string) => {
      if (behavior === 'allow' || behavior === 'travel:always-web') permissions?.decide(permId, true)
      else permissions?.decide(permId, false)
    })
    ipcMain.on('focus-session', () => { if (launch) launch.focusSession() })

    ipcMain.handle('meme-catalog', () => memoizedMemeCatalog())
    ipcMain.handle('meme-trigger', async (_e, sessionId: string, memeId: string) => {
      return triggerMeme(sessionId, memeId)
    })
    ipcMain.handle('travel-get', () => travelPublicState())
    ipcMain.handle('travel-postcards', () => [])
    ipcMain.handle('travel-start', async (_e, _sessionId, _templateId, mission: string) => {
      if (!launch) return { ok: false, code: 'not-ready' }
      const res = await launch.newChatWith(String(mission || '').trim() || '出去随便逛逛，回来告诉我你的见闻。')
      if (res.ok && res.key) addTravelLedger(res.key, String(mission || '').trim())
      return res.ok ? { ok: true, key: res.key } : { ok: false, code: res.error }
    })
    ipcMain.handle('travel-wander', async () => {
      if (!launch) return { ok: false, code: 'not-ready' }
      const res = await launch.newChatWith('出去随便逛逛，回来告诉我你的见闻。', '🧳 旅行')
      if (res.ok && res.key) addTravelLedger(res.key, 'wander')
      return res.ok ? { ok: true, key: res.key } : { ok: false, code: res.error }
    })
    ipcMain.handle('travel-cancel', () => ({ ok: true }))

    ipcMain.on('primary-action', async () => {
      await primaryAction()
    })
    ipcMain.on('set-ignore-mouse', (_e, ignore: boolean) => {
      // Renderer hit-test intent; the cursor poll re-derives it every 100ms so
      // this is only an immediate-response accelerator.
      petLog(`[click] renderer set-ignore-mouse=${ignore}`)
      applyIgnore(!ignore)
    })
    ipcMain.on('set-pet-size', (_e, w: number, h: number) => {
      if (petWin && !petWin.isDestroyed()) {
        const b = petWin.getBounds()
        const nw = (Number(w) > 0) ? Math.min(900, Math.max(BASE_W, Number(w))) : BASE_W
        const nh = (Number(h) > 0) ? Math.max(BASE_H, Number(h)) : BASE_H
        petWin.setBounds({ x: b.x, y: b.y, width: nw, height: nh })
      }
    })
    ipcMain.on('pet-tall', () => {})
    ipcMain.on('pet-big', () => {})
    ipcMain.on('pet-focus', () => { if (petWin && !petWin.isDestroyed()) { petWin.setFocusable(true); petWin.focus() } })
    ipcMain.on('pet-blur', () => { if (petWin && !petWin.isDestroyed()) petWin.blur() })
    ipcMain.on('ui-busy', (_e, on: boolean) => { uiBusy = !!on })
    ipcMain.on('pet-dragging', (_e, on: boolean) => {
      const wasDragging = dragging
      dragging = !!on
      petLog(`[click] dragging=${on}`)
      // Drag finished — if the cat was dropped on a window edge, perch on it.
      if (!on && wasDragging && anticsApi?.perchAt && petWin && !petWin.isDestroyed() && petVisualRect) {
        const b = petWin.getBounds()
        const px = Math.round(b.x + petVisualRect.x + petVisualRect.width / 2)
        const py = Math.round(b.y + petVisualRect.y + petVisualRect.height)
        setTimeout(() => { try { anticsApi?.perchAt?.(px, py) } catch {} }, 60)
      }
    })
    ipcMain.on('pet-visual-bounds', (_e, rect: { x: number; y: number; width: number; height: number }) => {
      if (rect && [rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) && rect.width > 0 && rect.height > 0) {
        petVisualRect = { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) }
      }
    })
    ipcMain.on('open-log', () => {})
    ipcMain.on('pet-log', (_e, tag: string, msg: string) => { try { console.log(`[pet:${tag}] ${msg}`) } catch {} })

    // antics (Windows mischief mode) — wired by the antics module
    ipcMain.on('antics-run-now', () => { if (anticsApi?.runNow) anticsApi.runNow() })
    ipcMain.on('antics-toggle', () => {
      savePetConfig({ anticsEnabled: !getPetConfig().anticsEnabled })
      broadcastConfig()
      syncAntics()
      refreshTray()
    })
  }

  function buildStatsNow(): PetStats {
    if (!core) return buildPetStats({ sessions: [], active: null, idleMs: null, lastActivityTs: 0, ts: Date.now() }, [], null, { lastOps: recentOps })
    return buildPetStats(core.buildSnapshot(), permissions ? permissions.getPending() : [], metering ? metering.getStats() : null, { lastOps: recentOps })
  }

  function broadcastConfig(): void {
    const cfg = frontendConfig()
    sendPet('pet:config', cfg)
    sendPanel('panel:config', cfg)
  }

  // 形态已移除：桌宠只有「显示 / 隐藏」两种状态。applyMode 只保留 show
  // 语义（SettingsPetPanel 的「显示桌宠」走 setMode('pet')）；hide 走 closePet。
  function applyMode(mode: string): void {
    if (mode === 'pet') showPetSafely()
    broadcastConfig()
    refreshTray()
  }

  // ── actions ───────────────────────────────────────────────────────────────
  async function newChat(): Promise<void> {
    // 「新开会话」＝ 唤起桌面壳控制 UI，让用户直接输入。
    if (launch) launch.focusSession()
  }

  async function primaryAction(): Promise<void> {
    if (!core) return
    const all = [...core.sessions.values()].filter((s) => !s.headless && s.state !== 'sleeping')
    if (all.length) { if (launch) launch.focusSession(); return }
    await newChat()
  }

  async function triggerMeme(sessionId: string, memeId: string): Promise<Record<string, unknown>> {
    const meme = memoizedMemeCatalog().items.find((m: any) => m.id === memeId) as any
    if (!meme) return { ok: false, submitted: false, message: t().t('meme.unknown') }
    if (!launch) return { ok: false, submitted: false, message: t().t('meme.noDispatcher') }
    const prompt = meme.prompt && meme.prompt.text ? meme.prompt.text : meme.prompt_text
    if (!prompt) return { ok: false, submitted: false, message: t().t('meme.unknown') }
    let key = sessionId || ''
    if (key) {
      const res = await launch.sendMessage(key, prompt)
      if (res.ok) return { ok: true, submitted: true, inputSent: true, memeId, sessionId: key }
    }
    const created = await launch.newChatWith(prompt, '😄 ' + memeId)
    return created.ok
      ? { ok: true, submitted: true, inputSent: true, memeId, sessionId: created.key }
      : { ok: false, submitted: false, message: created.error, memeId, sessionId: key }
  }

  // ── meme catalog (reads pet/assets/memes/catalog.json) ────────────────────
  let memeCache: { items: unknown[]; revision: number } | null = null
  function memoizedMemeCatalog(): { items: unknown[]; revision: number } {
    if (memeCache) return memeCache
    try {
      const raw = JSON.parse(fs.readFileSync(path.join(petDir, 'assets', 'memes', 'catalog.json'), 'utf8'))
      const items = Array.isArray(raw.items) ? raw.items : []
      memeCache = { items, revision: raw.revision || 0 }
    } catch {
      memeCache = { items: [], revision: 0 }
    }
    return memeCache
  }

  // ── travel ledger ─────────────────────────────────────────────────────────
  function addTravelLedger(key: string, mission: string): void {
    const c = getPetConfig()
    savePetConfig({ travelLedger: [...(c.travelLedger || []), { key, mission, startedAt: Date.now(), agent: 'squilla' }].slice(-50) })
  }
  function travelPublicState(): Record<string, unknown> {
    const active = (getPetConfig().travelLedger || []).filter((e: any) => e.key && core && core.getSession(e.key)).slice(-1)[0] || null
    return {
      active: active ? { sessionKey: active.key, mission: active.mission } : null,
      history: getPetConfig().travelLedger || [],
    }
  }

  // ── tray items (contributed to the OpenSquilla shell's tray) ──────────────
  // We don't own a tray icon — the shell has one. Expose the pet's menu items
  // via getTrayMenuItems() so main.ts can splice them into the shell's menu.
  // Deliberately minimal: all pet configuration now lives in the OpenSquilla
  // Settings → Pet section, so the tray only carries the two day-to-day actions.
  function getTrayMenuItems(): Electron.MenuItemConstructorOptions[] {
    return [
      { label: '🐙 显示桌宠', click: () => showPetSafely() },
      { label: '📊 桌宠详情面板', click: openPanel },
    ]
  }

  function refreshTray(): void { deps.onTrayChanged?.() }

  // ── lifecycle ─────────────────────────────────────────────────────────────
  function start(): void {
    if (stopped) return
    core = createPetCore({ onActivity, onDirty: scheduleEmit })
    core.startStaleCleanup()
    permissions = createPermissions({ rpc: () => watch?.rpc ?? null, onChange: scheduleEmit })
    metering = createPetMetering({ rpc: () => watch?.rpc ?? null })
    launch = createLaunch({
      rpc: () => watch?.rpc ?? null,
      focusMainWindow: () => {
        const w = deps.getMainWindow()
        if (w && !w.isDestroyed()) { if (w.isMinimized()) w.restore(); w.show(); w.focus() }
      },
    })
    try { loadPetI18n().setLang('zh') } catch {}

    watch = createSquillaWatch({
      core,
      gatewayUrl: deps.gatewayUrl,
      token: deps.token,
      version: deps.version,
      onApproval: (kind, payload) => permissions!.ingest(kind, payload),
      onDoneUsage: (info) => metering!.onDone(info),
      onSessionDone: (info) => {
        lastDoneTs = Date.now()
        lastDoneText = info.text || ''
        // reset any pending attention-seeking so a fresh reply starts a fresh timer
        try { antics?.onNewReply?.() } catch {}
      },
    })

    registerIpc()
    if (getPetConfig().petEnabled !== false) createPetWindow()
    // Track user activity via the main window's focus lifecycle so the pet
    // knows when the user actually looked at the app.  Stored so stop() can
    // remove them and prevent listener leaks on restart.
    const bumpActivity = (): void => { lastUserActivityTs = Date.now() }
    app.on('browser-window-focus', bumpActivity)
    app.on('browser-window-blur', bumpActivity)
    focusBlurHandlers = { focus: bumpActivity, blur: bumpActivity }
    deps.onTrayChanged?.()
    metering.start()
    void watch.start()
    syncAntics()
    if (!ignoreTimer) {
      ignoreTimer = setInterval(ignoreTick, 100)
      if (ignoreTimer.unref) ignoreTimer.unref()
    }
    setWin32Log(petLog)
    petLog(`[start] pet backend running url=${deps.gatewayUrl}`)

    statsTimer = setInterval(emitStats, 4000)
    if (statsTimer.unref) statsTimer.unref()
    online = true
  }

  function stop(): void {
    stopped = true
    if (ignoreTimer) { clearInterval(ignoreTimer); ignoreTimer = null }
    if (statsTimer) { clearInterval(statsTimer); statsTimer = null }
    if (watch) { watch.stop(); watch = null }
    if (metering) { metering.stop(); metering = null }
    if (core) { core.stopStaleCleanup(); core = null }
    if (antics) { try { antics.stop() } catch {} antics = null }
    anticsApi = null
    if (focusBlurHandlers) {
      app.removeListener('browser-window-focus', focusBlurHandlers.focus)
      app.removeListener('browser-window-blur', focusBlurHandlers.blur)
      focusBlurHandlers = null
    }
    if (tray) { try { tray.destroy() } catch {} tray = null }
    if (petWin && !petWin.isDestroyed()) petWin.close()
    if (panelWin && !panelWin.isDestroyed()) panelWin.close()
  }

  /** Called by main.ts after reconnect to a gateway; refresh subscriptions. */
  function refresh(): void {
    if (watch) watch.refreshNow()
    online = true
    broadcastConfig()
  }

  /** The antics module attaches itself here after the backend starts. */
  function attachAntics(api: AnticsApi): void {
    anticsApi = api
  }

  return { start, stop, refresh, attachAntics, openPanel, getTrayMenuItems }
}

export type PetBackend = ReturnType<typeof createPetBackend>
