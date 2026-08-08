// Antics — the pet's Windows mischief mode.
//
// Ports OpenSquilla pet's macOS territory.js orchestration (cat-paw-above, patrol/push,
// loot) to Win32 via win32.ts, and adds the two playful behaviors the user
// asked for, modeled on open-source desktop pets:
//   • 偷鼠标  — pounce and snatch the cursor (PyGoose's "Nab Mouse").
//   • 趴窗口  — glide onto the foreground app window and hang there, following
//               it as it moves (Shimeji-EE / desktop-pet style).
// Reference APIs, not copied code — the Win32 calls themselves are unlicenced.

import { BrowserWindow, screen } from 'electron'
import {
  setCursorPos, cursorPull, getForegroundWindow, getWindowRect, getCursorPos,
  enumTopWindows, Win32Window,
} from './win32.js'

export interface AnticsDeps {
  getPetWin: () => BrowserWindow | null
  /** Sprite's visible bounds in window-local coords (from the renderer). */
  getPetVisualRect?: () => { x: number; y: number; width: number; height: number } | null
  emit: (ev: Record<string, unknown>) => void
  /** true while the user is actively using the app (avoid hijacking mid-task). */
  shouldAbort: () => boolean
  /** diagnostic log sink (pet.log) */
  log?: (msg: string) => void
  /** last assistant `done` event: ts=0 means "no pending reply". */
  getLastDone?: () => { ts: number; text: string }
  /** ms timestamp of the last time the user showed signs of life in the shell. */
  getLastUserActivity?: () => number
  /** Called once the user has actually engaged after a nudge — clears state. */
  markUserSaw?: () => void
  /** Whether the mischief mode toggle is on — gates cursor-stealing. */
  getMischiefEnabled?: () => boolean
  /** Push a text bubble to the pet renderer. */
  showPetBubble?: (text: string) => void
}

export function createAntics(deps: AnticsDeps) {
  let running = false
  let episode: null | 'patrol' = null
  let behaviorTimer: ReturnType<typeof setInterval> | null = null
  let presenceTimer: ReturnType<typeof setInterval> | null = null
  let nabTimer: ReturnType<typeof setInterval> | null = null
  const log = deps.log || (() => {})

  // ── attention-seeking state ──────────────────────────────────────────────
  // Phase transitions (only advance when the user hasn't engaged):
  //   idle → 'nudge' (after NUDGE_AFTER_DONE_MS since done) — approach cursor + bubble
  //   nudge → 'nab' (after NAB_AFTER_NUDGE_MS) — steal the cursor
  //   any → idle when a fresh reply arrives OR the user engages
  let attentionPhase: 'idle' | 'nudge' | 'nab' = 'idle'
  let nudgeStartedAt = 0
  let attentionTimer: ReturnType<typeof setInterval> | null = null
  const NUDGE_AFTER_DONE_MS = 60_000 // 1 min after done → nudge
  const NAB_AFTER_NUDGE_MS = 30_000  // then 30s → nab

  function petWin(): BrowserWindow | null { return deps.getPetWin() }

  // Sprite's absolute screen rect (falls back to the window bounds if the
  // renderer hasn't reported the visual bounds yet).
  function petSpriteRect(): { x: number; y: number; width: number; height: number } | null {
    const w = petWin()
    if (!w || w.isDestroyed()) return null
    const b = w.getBounds()
    const local = deps.getPetVisualRect?.() || null
    if (local && local.width > 0 && local.height > 0) {
      return { x: b.x + local.x, y: b.y + local.y, width: local.width, height: local.height }
    }
    return { x: b.x, y: b.y, width: b.width, height: b.height }
  }

  // Given a "perch on top edge" target for the pet WINDOW (not sprite), clamp
  // it so the visible cat sprite lands inside the work area of the display the
  // target window sits on. If the top edge won't fit, perch on the right/left
  // edge (whichever has room); if neither fits, fall back to workarea top.
  async function clampPerch(
    targetX: number, targetY: number,
    ref: { x: number; y: number; w: number; h: number },
  ): Promise<{ x: number; y: number }> {
    const win = petWin()
    if (!win || win.isDestroyed()) return { x: targetX, y: targetY }
    const wb = win.getBounds()
    const sprite = petSpriteRect()
    // Local offset: how far the sprite sits inside the window (top-left).
    const sx = sprite ? sprite.x - wb.x : 0
    const sy = sprite ? sprite.y - wb.y : 0
    const sw = sprite ? sprite.width : wb.width
    const sh = sprite ? sprite.height : wb.height
    try {
      const wa = screen.getDisplayMatching({
        x: ref.x, y: ref.y, width: Math.max(1, ref.w), height: Math.max(1, ref.h),
      }).workArea
      // Convert to "sprite must stay inside wa" constraints.
      const minWinX = wa.x - sx + 4
      const maxWinX = wa.x + wa.width - sw - sx - 4
      const minWinY = wa.y - sy + 4
      const maxWinY = wa.y + wa.height - sh - sy - 4
      // If perching above ref would put the sprite above the work area, try
      // perching on the right of ref; then left; then fall back to sitting on
      // top of the window (inside the work area) instead of off-screen.
      if (targetY < minWinY) {
        const rightWinX = ref.x + ref.w + 4 - sx
        const leftWinX = ref.x - sw - sx - 4
        if (rightWinX >= minWinX && rightWinX <= maxWinX) {
          return { x: rightWinX, y: Math.max(minWinY, Math.min(maxWinY, ref.y - sy)) }
        }
        if (leftWinX >= minWinX && leftWinX <= maxWinX) {
          return { x: leftWinX, y: Math.max(minWinY, Math.min(maxWinY, ref.y - sy)) }
        }
        // Neither side fits — sit ON the window's top edge (just inside),
        // like a cat sitting on a windowsill from the inside.
        return {
          x: Math.max(minWinX, Math.min(maxWinX, targetX)),
          y: Math.max(minWinY, Math.min(maxWinY, ref.y - sy + 8)),
        }
      }
      return {
        x: Math.max(minWinX, Math.min(maxWinX, targetX)),
        y: Math.max(minWinY, Math.min(maxWinY, targetY)),
      }
    } catch {
      return { x: targetX, y: targetY }
    }
  }

  // Force any target coord to keep the SPRITE inside SOME display's work
  // area, regardless of what the caller passed. This is the last line of
  // defense against off-screen drift from tween interpolation, stale clamp
  // references, or bounds surviving a display disconnect.
  function safeBounds(rawX: number, rawY: number): { x: number; y: number } {
    const win = petWin()
    if (!win || win.isDestroyed()) return { x: rawX, y: rawY }
    const wb = win.getBounds()
    const sprite = petSpriteRect()
    const sx = sprite ? sprite.x - wb.x : 0
    const sy = sprite ? sprite.y - wb.y : 0
    const sw = sprite ? sprite.width : wb.width
    const sh = sprite ? sprite.height : wb.height
    try {
      const displays = screen.getAllDisplays()
      if (!displays.length) return { x: rawX, y: rawY }
      // Pick the display whose work area contains the sprite center for the
      // proposed position, or nearest by center distance.
      const wantSpriteX = rawX + sx
      const wantSpriteY = rawY + sy
      let best = displays[0]
      let bestDist = Number.POSITIVE_INFINITY
      for (const d of displays) {
        const wa = d.workArea
        const cx = wa.x + wa.width / 2
        const cy = wa.y + wa.height / 2
        const dist = Math.hypot(wantSpriteX + sw / 2 - cx, wantSpriteY + sh / 2 - cy)
        if (dist < bestDist) { bestDist = dist; best = d }
      }
      const wa = best.workArea
      const minX = wa.x - sx + 4
      const maxX = wa.x + wa.width - sw - sx - 4
      const minY = wa.y - sy + 4
      const maxY = wa.y + wa.height - sh - sy - 4
      return {
        x: Math.max(minX, Math.min(maxX, rawX)),
        y: Math.max(minY, Math.min(maxY, rawY)),
      }
    } catch { return { x: rawX, y: rawY } }
  }

  function tweenWin(toX: number, toY: number, ms: number): Promise<void> {
    const win = petWin()
    if (!win || win.isDestroyed()) return Promise.resolve()
    // Clamp the destination too, in case the caller forgot.
    const dest = safeBounds(toX, toY)
    const from = win.getBounds()
    const dur = Math.max(80, ms)
    const t0 = Date.now()
    return new Promise((resolve) => {
      const step = () => {
        const w = petWin()
        if (!w || w.isDestroyed()) return resolve()
        const t = Math.min(1, (Date.now() - t0) / dur)
        const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2
        const b = w.getBounds()
        // Clamp EVERY intermediate step, not just the destination — an
        // interpolated point can be off-screen even when both ends are safe.
        const s = safeBounds(
          Math.round(from.x + (dest.x - from.x) * e),
          Math.round(from.y + (dest.y - from.y) * e),
        )
        w.setBounds({ x: s.x, y: s.y, width: b.width, height: b.height })
        if (t >= 1) resolve(); else setTimeout(step, 16)
      }
      step()
    })
  }

  // ── cat-paw-above: stay above other windows ─────────────────────────────
  async function presenceTick(): Promise<void> {
    const win = petWin()
    if (!win || win.isDestroyed()) return
    try {
      win.setAlwaysOnTop(true, 'floating')
      win.moveTop()
    } catch {}
  }

  // ── behavior system ─────────────────────────────────────────────────────
  // Entry point: behaviorTick() runs every 3s (via behaviorTimer).
  // Each behavior has its own per-tick logic and duration.
  let behaviorBusy = false // reentrancy guard — Win32 calls take 1-4s each
  async function behaviorTick(): Promise<void> {
    if (episode || deps.shouldAbort() || behaviorBusy) return
    behaviorBusy = true
    try {
      const now = Date.now()
      // sleep/dangle are window-perch finales that must keep following the
      // window. They still enter tickWindow every tick to re-anchor — but the
      // window behavior is wrapped up when their own end timestamp fires.
      if (behavior === 'window' && (winPhase === 'sleep' || winPhase === 'dangle')) {
        const fin = winPhase === 'sleep' ? winSleepUntil : dangleUntil
        if (fin && now >= fin) {
          lastBehavior = 'window'
          winHwnd = ''
          winRect = null
          winPhase = 'approach'
          dangleUntil = 0
          winSleepUntil = 0
          behavior = pickBehavior()
          behaviorEndsAt = now + enterBehavior(behavior)
          log(`[beh] → ${behavior} (last=window)`)
        }
        await tickBehavior()
        return
      }
      if (now >= behaviorEndsAt) {
        lastBehavior = behavior
        behavior = pickBehavior()
        behaviorEndsAt = now + enterBehavior(behavior)
        log(`[beh] → ${behavior} (last=${lastBehavior})`)
      }
      await tickBehavior()
    } catch (e) { log(`[beh] tick ${behavior} err: ${e}`) }
    finally { behaviorBusy = false }
  }

  function rand(lo: number, hi: number): number { return lo + Math.random() * (hi - lo) }
  function randInt(lo: number, hi: number): number { return Math.floor(rand(lo, hi + 1)) }
  function pick<T>(arr: T[]): T { return arr[Math.floor(Math.random() * arr.length)] }

  function enterBehavior(beh: Behavior): number /* duration ms */ {
    switch (beh) {
      case 'idle': return rand(4000, 8000)
      case 'walk': {
        facing = Math.random() < 0.5 ? 1 : -1
        return rand(2000, 6000)
      }
      case 'watch': return rand(8000, 18000)
      case 'window': {
        winPhase = 'approach'
        winStepsLeft = randInt(4, 10)
        winHwnd = ''
        winRect = null
        winAcquireRetries = 0
        return rand(20000, 50000) // generous — window lifecycle is multi-phase
      }
      case 'sleep': {
        deps.showPetBubble?.(pick(['zzz...', '困了~ 💤', '呼噜噜...']))
        return rand(15000, 40000)
      }
    }
  }

  async function tickBehavior(): Promise<void> {
    switch (behavior) {
      case 'idle': break // do nothing, wait for timeout
      case 'walk': await tickWalk(); break
      case 'watch': await tickWatch(); break
      case 'window': await tickWindow(); break
      case 'sleep': break // do nothing
    }
  }

  // ── walk ───────────────────────────────────────────────────────────────
  async function tickWalk(): Promise<void> {
    const win = petWin()
    if (!win || win.isDestroyed()) return
    const b = win.getBounds()
    const sprite = petSpriteRect()
    const sw = sprite ? sprite.width : b.width
    const dx = sprite ? sprite.x - b.x : 0
    // Pick a point 200-400px in facing direction.
    let wantX = b.x + facing * rand(200, 400)
    // Clamp to the work area of the display we're on.
    try {
      const wa = screen.getDisplayMatching({
        x: b.x + dx, y: b.y, width: sw, height: b.height,
      }).workArea
      const minX = wa.x - dx + 4
      const maxX = wa.x + wa.width - sw - dx - 4
      if (wantX < minX || wantX > maxX) {
        facing = -facing
        wantX = b.x + facing * rand(200, 400)
      }
      wantX = Math.max(minX, Math.min(maxX, wantX))
    } catch {}
    await tweenWin(wantX, b.y, randInt(800, 1600))
  }

  // ── watch (看鼠标) ─────────────────────────────────────────────────────
  // Approach the cursor at a polite distance, stare, occasionally bob/nudge.
  // If the cursor gets too close (<45px), flee in the opposite direction.
  const WATCH_DIST_MIN = 100
  const WATCH_DIST_MAX = 160
  const WATCH_FLEE_DIST = 45
  let watchCooldown = 0
  async function tickWatch(): Promise<void> {
    const win = petWin()
    if (!win || win.isDestroyed()) return
    const sprite = petSpriteRect()
    if (!sprite) return
    // Cursor pos from Electron (free, DIP) — no PowerShell spawn per tick.
    let cursor: { x: number; y: number } | null = null
    try { const p = screen.getCursorScreenPoint(); cursor = { x: p.x, y: p.y } } catch {}
    if (!cursor) return
    const cx = sprite.x + sprite.width / 2
    const cy = sprite.y + sprite.height / 2
    const dist = Math.hypot(cursor.x - cx, cursor.y - cy)
    const b = win.getBounds()
    const dx = sprite ? sprite.x - b.x : 0
    const dy = sprite ? sprite.y - b.y : 0
    const sw = sprite ? sprite.width : b.width
    const sh = sprite ? sprite.height : b.height

    // Too close → flee (with cooldown so we don't jitter).
    if (dist < WATCH_FLEE_DIST && Date.now() > watchCooldown) {
      const awayX = cx + (cx - cursor.x) * 1.5
      const awayY = cy + (cy - cursor.y) * 0.5
      const wantX = Math.round(awayX - dx - sw / 2)
      const wantY = Math.round(awayY - dy - sh / 2)
      const ref = { x: cursor.x, y: cursor.y, w: 1, h: 1 }
      const c = await clampPerch(wantX, wantY, ref)
      await tweenWin(c.x, c.y, 600)
      watchCooldown = Date.now() + 3000
      return
    }

    // Too far → approach politely.
    if (dist > WATCH_DIST_MAX + 60) {
      const dirX = (cursor.x - cx) / dist
      const dirY = (cursor.y - cy) / dist
      const wantX = Math.round(cursor.x - dirX * rand(WATCH_DIST_MIN, WATCH_DIST_MAX) - dx - sw / 2)
      const wantY = Math.round(cursor.y - dirY * rand(WATCH_DIST_MIN, WATCH_DIST_MAX) - dy - sh / 2)
      const ref = { x: cursor.x, y: cursor.y, w: 1, h: 1 }
      const c = await clampPerch(wantX, wantY, ref)
      await tweenWin(c.x, c.y, 1200)
      return
    }

    // In range — face cursor (set facing for renderer).
    facing = cursor.x >= cx ? 1 : -1
    // Occasional nudge bubble.
    if (Math.random() < 0.08) {
      deps.showPetBubble?.(pick(['喵~', '👀', '你忙吧我看着', '嘿嘿', '👀👀', '有我在']))
    }
  }

  // ── window (趴窗口完整生命周期) ─────────────────────────────────────────
  // desktop-pet style: approach → climb → walk along top → finale (sleep/dangle/leap).
  let winAcquireRetries = 0
  let staleRectCount = 0
  async function tickWindow(): Promise<void> {
    const win = petWin()
    if (!win || win.isDestroyed()) return

    // Acquire target foreground window on first tick.
    if (!winHwnd) {
      if (winAcquireRetries >= 3) { log('[beh-win] gave up acquiring fg window'); behavior = 'idle'; behaviorEndsAt = Date.now() + rand(3000, 6000); return }
      winAcquireRetries++
      let fg: string | null
      try { fg = await withTimeout(getForegroundWindow(), 4000).catch(() => null) } catch { return }
      if (!fg || fg === '0') { log('[beh-win] no fg window'); return } // retry next tick
      let rect: { x: number; y: number; w: number; h: number } | null
      try { rect = await withTimeout(getWindowRect(fg), 4000).catch(() => null) } catch { return }
      if (!rect || rect.w < 200 || rect.h < 200) { log(`[beh-win] fg too small: ${JSON.stringify(rect)}`); return }
      winHwnd = fg
      winRect = rect
      winPhase = 'approach'
      staleRectCount = 0
      log(`[beh-win] acquired fg=${fg} rect=${rect.x},${rect.y},${rect.w}x${rect.h}`)
    }

    // Re-fetch window rect (may have moved). If it repeatedly returns null the
    // window closed / hwnd went stale — re-acquire a fresh foreground window.
    let rect = winRect
    try {
      const fresh = await withTimeout(getWindowRect(winHwnd), 3000).catch(() => null)
      if (fresh && fresh.w >= 200) { rect = fresh; winRect = fresh; staleRectCount = 0 }
      else {
        staleRectCount++
        if (staleRectCount > 3) {
          log(`[beh-win] window ${winHwnd} stale after ${staleRectCount} nulls, re-acquiring`)
          winHwnd = ''
          winRect = null
          winAcquireRetries = 0
          return
        }
        if (!fresh) log(`[beh-win] getWindowRect returned null for ${winHwnd} (${staleRectCount})`)
      }
    } catch {}
    if (!rect) return // keep trying, don't kill the behavior

    const sprite = petSpriteRect()
    const b = win.getBounds()
    const dx = sprite ? sprite.x - b.x : 0
    const dy = sprite ? sprite.y - b.y : 0
    const sw = sprite ? sprite.width : b.width
    const sh = sprite ? sprite.height : b.height

    switch (winPhase) {
      case 'approach': {
        // Walk to the nearer side of the window.
        const catCx = b.x + dx + sw / 2
        const nearLeft = catCx < rect.x + rect.w / 2
        const wantX = nearLeft
          ? Math.round(rect.x - sw - dx - 4)
          : Math.round(rect.x + rect.w - dx + 4)
        const c = await clampPerch(wantX, b.y, rect)
        log(`[beh-win] approach → ${c.x},${c.y}`)
        await tweenWin(c.x, c.y, 800)
        winPhase = 'climb'
        break
      }
      case 'climb': {
        // Climb from current position up to the top edge.
        const wantX = b.x
        const wantY = Math.round(rect.y - sh - dy + 2)
        const c = await clampPerch(wantX, wantY, rect)
        log(`[beh-win] climb → ${c.x},${c.y}`)
        await tweenWin(c.x, c.y, 600)
        winPhase = 'walk'
        break
      }
      case 'walk': {
        if (winStepsLeft <= 0) {
          // Pick finale.
          const r = Math.random()
          if (r < 0.4) {
            winPhase = 'sleep'
            winSleepUntil = 0
            deps.showPetBubble?.(pick(['困了~ 在这睡一会儿', 'zzz...', '呼噜噜...', '就趴这里了~']))
          } else if (r < 0.7) {
            winPhase = 'dangle'
            dangleUntil = 0
          } else {
            winPhase = 'leap'
          }
          log(`[beh-win] finale → ${winPhase}`)
          break
        }
        winStepsLeft--
        // Walk a random 30-70px step along the top edge.
        const step = rand(30, 70) * (Math.random() < 0.5 ? 1 : -1)
        let wantX = b.x + step
        // Bounce at edge of window.
        const spriteX = wantX + dx
        if (spriteX < rect.x + 10 || spriteX + sw > rect.x + rect.w - 10) {
          wantX = b.x - step
        }
        const wantY = Math.round(rect.y - sh - dy + 2)
        const c = await clampPerch(wantX, wantY, rect)
        await tweenWin(c.x, c.y, randInt(350, 600))
        break
      }
      case 'sleep': {
        // Sleep ON the window edge and keep following the window as it moves.
        // First entry sets winSleepUntil; behaviorTick wraps up when it fires.
        if (!winSleepUntil) {
          winSleepUntil = Date.now() + rand(15000, 40000)
          deps.showPetBubble?.(pick(['困了~ 在这睡一会儿', 'zzz...', '呼噜噜...', '就趴这里了~']))
          log('[beh-win] → sleep on window edge')
        }
        // Follow the window: keep the cat's feet on the current top edge.
        const wantY = Math.round(rect.y - sh - dy + 2)
        const c = await clampPerch(b.x, wantY, rect)
        await tweenWin(c.x, c.y, 300)
        break
      }
      case 'dangle': {
        // Hang off a corner of the window, following it as it moves; after the
        // dangle window elapses, leap off. Uses dangleUntil (set on first tick)
        // instead of a blocking setTimeout so each tick re-anchors.
        if (!dangleUntil) {
          dangleUntil = Date.now() + rand(5000, 15000)
          deps.showPetBubble?.(pick(['挂住了~', '有点高...', '喵~']))
        }
        const nearRight = (b.x + dx + sw / 2) > rect.x + rect.w / 2
        const cornerX = nearRight
          ? Math.round(rect.x + rect.w - sw / 2 - dx)
          : Math.round(rect.x - sw / 2 - dx)
        const cornerY = Math.round(rect.y - dy + 2)
        const c = await clampPerch(cornerX, cornerY, rect)
        log(`[beh-win] dangle → ${c.x},${c.y}`)
        await tweenWin(c.x, c.y, 300)
        if (Date.now() >= dangleUntil) {
          // Dangle finished — leap away.
          const awayX = b.x + (nearRight ? 1 : -1) * rand(100, 300)
          const awayY = b.y + rand(60, 200)
          const lc = await safeBounds(awayX, awayY)
          await tweenWin(lc.x, lc.y, 600)
          winHwnd = ''
          winRect = null
          dangleUntil = 0
          winAcquireRetries = 0
          behaviorEndsAt = 0
          log('[beh-win] dangle → leap done')
        }
        break
      }
      case 'leap': {
        // Leap off the window: jump to a random direction away from the window.
        const awayX = b.x + (Math.random() < 0.5 ? -1 : 1) * rand(100, 300)
        const awayY = b.y + rand(60, 200) // "fall" downward
        const c = await safeBounds(awayX, awayY)
        log(`[beh-win] leap → ${c.x},${c.y}`)
        await tweenWin(c.x, c.y, 600)
        winHwnd = ''
        winRect = null
        winAcquireRetries = 0
        behaviorEndsAt = 0 // end this behavior immediately
        break
      }
    }
  }

  // ── 偷鼠标: pounce when the cursor comes near, pull it onto the pet ───────
  async function nabTick(): Promise<void> {
    if (episode || deps.shouldAbort()) return
    const win = petWin()
    if (!win || win.isDestroyed() || !win.isVisible()) return
    const sprite = petSpriteRect()
    if (!sprite) return
    const cx = sprite.x + sprite.width / 2
    const cy = sprite.y + sprite.height / 2
    const reach = Math.max(24, Math.round(Math.min(sprite.width, sprite.height) * 0.4))
    // Cheap Electron cursor check first — only reach for the (slower) Win32
    // precise read when the cursor is plausibly near the sprite. Saves one
    // PowerShell spawn per tick in the common far case.
    let near = false
    try {
      const p = screen.getCursorScreenPoint()
      near = Math.hypot(p.x - cx, p.y - cy) <= reach + 6
    } catch { return }
    if (!near) return
    const pos = await getCursorPos()
    if (!pos) return
    // Trigger only when the cursor is genuinely on/next to the sprite: it must
    // be inside a small "reach" band around the visible cat, not the full
    // transparent window bounds. Radius = 40% of the smaller sprite axis.
    const dist = Math.hypot(pos.x - cx, pos.y - cy)
    if (dist > reach || dist < 12) return
    log(`[nab] cursor ${pos.x},${pos.y} sprite ${Math.round(cx)},${Math.round(cy)} reach=${reach} dist=${Math.round(dist)}`)
    deps.emit({ kind: 'antics', phase: 'nab', ts: Date.now() })
    deps.showPetBubble?.(pick(NAB_QUIPS_ANTICS))
    await cursorPull(cx, cy, 6, 22)
    // Release: nudge the cursor a little away so the user regains it.
    await setCursorPos(cx + 60, cy + 20)
  }

  // ── 巡视 (patrol): crawl onto the foreground app window and hang there ────
  // Desktop-pet style: find the active window, glide to its top edge, perch.
  // One-shot and always available (does not require the mischief mode toggle).
  async function runNow(): Promise<string> {
    if (episode) { log('[patrol] busy'); return 'busy' }
    episode = 'patrol'
    log('[patrol] runNow start')
    try {
      const win = petWin()
      if (!win || win.isDestroyed()) { log('[patrol] no pet window'); return 'no-pet' }
      let fg: string | null = null
      try { fg = await withTimeout(getForegroundWindow(), 6000) } catch (e) { log(`[patrol] getForegroundWindow failed: ${e}`) }
      log(`[patrol] fg=${fg}`)
      let rect: { x: number; y: number; w: number; h: number } | null = null
      if (fg && fg !== '0') {
        try { rect = await withTimeout(getWindowRect(fg), 6000) } catch (e) { log(`[patrol] getWindowRect failed: ${e}`) }
        log(`[patrol] rect=${JSON.stringify(rect)}`)
      }
      // Fallback when the Win32 bridge is slow/unavailable: use Electron's
      // screen API to at least perch the pet at the top of the display that
      // contains the cursor (better than nothing, always works).
      if (!rect || rect.w < 200 || rect.h < 200) {
        log('[patrol] falling back to Electron screen API')
        try {
          const { screen } = await import('electron')
          const p = screen.getCursorScreenPoint()
          const wa = screen.getDisplayMatching({ x: p.x, y: p.y, width: 1, height: 1 }).workArea
          rect = { x: wa.x, y: wa.y, w: wa.width, h: wa.height }
          log(`[patrol] fallback rect=${JSON.stringify(rect)}`)
        } catch (e) {
          log(`[patrol] screen fallback failed: ${e}`)
        }
      }
      if (!rect) { log('[patrol] no rect, abort'); deps.emit({ kind: 'antics', phase: 'patrol-none', ts: Date.now() }); return 'no-window' }
      const b = win.getBounds()
      const sprite = petSpriteRect()
      const spriteW = sprite ? sprite.width : b.width
      const spriteH = sprite ? sprite.height : b.height
      const dx = sprite ? sprite.x - b.x : 0
      const dy = sprite ? sprite.y - b.y : 0
      const wantWinX = Math.round(rect.x + (rect.w - spriteW) / 2 - dx)
      const wantWinY = Math.round(rect.y - spriteH - dy + 2)
      const clamped = await clampPerch(wantWinX, wantWinY, rect)
      log(`[patrol] tween to ${clamped.x},${clamped.y} from ${b.x},${b.y}`)
      deps.emit({ kind: 'antics', phase: 'patrol', ts: Date.now() })
      await tweenWin(clamped.x, clamped.y, 900)
      log('[patrol] done')
      return 'ok'
    } finally { episode = null }
  }

  // ── perchAt: user dragged the cat onto a window edge — perch on that window ─
  // Find the topmost normal-sized window under/near (x, y) (the cat's drop
  // point) and glide up onto its top edge, following it afterwards.
  async function perchAt(x: number, y: number): Promise<string> {
    if (episode) return 'busy'
    let wins: Win32Window[] = []
    try { wins = await enumTopWindows() } catch { wins = [] }
    const candidates = wins.filter((w) => {
      if (w.pid === process.pid) return false // never perch on ourselves
      if (w.w < 200 || w.h < 200 || w.w > 3200 || w.h > 2200) return false
      // window must be under/near the drop point: sprite bottom overlaps the
      // window's top band (≤60px above) OR the point is inside the window.
      const overTop = Math.abs(y - w.y) <= 60 && x >= w.x - 40 && x <= w.x + w.w + 40
      const inside = x >= w.x && x <= w.x + w.w && y >= w.y && y <= w.y + w.h
      return overTop || inside
    })
    if (!candidates.length) { log(`[perch] no window near ${x},${y}`); return 'none' }
    // Prefer the candidate whose top edge is closest to the drop point.
    candidates.sort((a, b) => Math.abs(y - a.y) - Math.abs(y - b.y))
    const target = candidates[0]
    log(`[perch] target=${target.hwnd} ${target.x},${target.y},${target.w}x${target.h}`)
    episode = 'patrol'
    try {
      const win = petWin()
      if (!win || win.isDestroyed()) return 'no-pet'
      const rect = { x: target.x, y: target.y, w: target.w, h: target.h }
      const b = win.getBounds()
      const sprite = petSpriteRect()
      const dx = sprite ? sprite.x - b.x : 0
      const dy = sprite ? sprite.y - b.y : 0
      const sw = sprite ? sprite.width : b.width
      const sh = sprite ? sprite.height : b.height
      // approach: walk to the nearer side of the window
      const catCx = b.x + dx + sw / 2
      const nearLeft = catCx < rect.x + rect.w / 2
      const wantX = nearLeft
        ? Math.round(rect.x - sw - dx - 4)
        : Math.round(rect.x + rect.w - dx + 4)
      const c1 = await clampPerch(wantX, b.y, rect)
      await tweenWin(c1.x, c1.y, 600)
      // climb onto the top edge
      const wantY = Math.round(rect.y - sh - dy + 2)
      const c2 = await clampPerch(wantX, wantY, rect)
      await tweenWin(c2.x, c2.y, 500)
      // Hand off to the window behavior so the cat keeps following this window.
      winHwnd = target.hwnd
      winRect = rect
      winPhase = 'walk'
      winStepsLeft = randInt(4, 10)
      behavior = 'window'
      behaviorEndsAt = Date.now() + rand(20000, 50000)
      lastBehavior = 'idle'
      log('[perch] handed off to window behavior')
      return 'ok'
    } finally { episode = null }
  }

  function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
    return Promise.race([p, new Promise<never>((_, rej) => setTimeout(() => rej(new Error('timeout')), ms))])
  }

  // ── attention-seeking ─────────────────────────────────────────────────────
  // Every 5s: if a reply landed a while ago AND the user still hasn't touched
  // the app, come to the cursor with a "completed — come see!" bubble; if
  // that's still ignored after another 30s, nab the cursor. This runs even
  // when the mischief toggle is off — the whole point is to grab attention
  // when the user has been AFK past a reply.
  // Random cheeky lines the cat says while snatching the mouse. Kept short so
  // they fit in the transient bubble.
  const NAB_QUIPS_ATTENTION = [
    '喵！！过来看看我啊！',
    '鼠标借我玩玩 ~',
    '不来看我？那我先没收鼠标！',
    '哼，让我抓一下就还你',
    '看这里！你的鼠标被我叼走了！',
    '还不理我？那这个爪子先归我',
    '嘿~ 光标是我的了，快来抢！',
  ]
  const NAB_QUIPS_ANTICS = [
    '鼠标借我玩玩 ~',
    '抓到啦！',
    '喵爪快过~',
    '这个圆圆的能吃吗？',
    '嘿嘿嘿光标归我！',
    '溜~~~',
    '给我给我给我！',
  ]
  const NUDGE_QUIPS_NO_TEXT = [
    '完成了~ 怎么不来看看我？',
    '喵，任务搞定啦 快看结果！',
    '写完了哦~ 主人来验收',
    '结果准备好啦，过来瞅一眼？',
  ]
  // pick() is defined earlier in the behavior section — reuse it here.

  async function attentionTick(): Promise<void> {
    if (episode) return
    const last = deps.getLastDone?.()
    if (!last || !last.ts) { attentionPhase = 'idle'; return }
    const lastAct = deps.getLastUserActivity?.() ?? 0
    // If the user engaged after the reply, this cycle is done.
    if (lastAct >= last.ts) { attentionPhase = 'idle'; return }
    const sinceDone = Date.now() - last.ts
    if (attentionPhase === 'idle' && sinceDone >= NUDGE_AFTER_DONE_MS) {
      attentionPhase = 'nudge'
      nudgeStartedAt = Date.now()
      log(`[attention] nudge (idle for ${Math.round(sinceDone / 1000)}s since done)`)
      await approachCursorAndNudge(last.text)
      return
    }
    if (attentionPhase === 'nudge' && Date.now() - nudgeStartedAt >= NAB_AFTER_NUDGE_MS) {
      attentionPhase = 'nab'
      // If mischief mode is off, degrade the "nab" to a gentle bubble only —
      // never steal the cursor unless the user opted into mischief.
      if (deps.getMischiefEnabled?.() !== true) {
        log('[attention] nab degraded to bubble (mischief off)')
        deps.showPetBubble?.(pick(['喵~ 还在吗？回复好啦', '完成了，看到的话吱一声~', '来看我一眼嘛~']))
        attentionPhase = 'idle' // wait for a fresh reply / engagement
        return
      }
      log('[attention] nab (still no reply from user)')
      await nabForAttention()
      // After the nab, wait for real engagement or a fresh reply.
    }
  }

  async function approachCursorAndNudge(replyText: string): Promise<void> {
    const win = petWin()
    if (!win || win.isDestroyed()) return
    // Electron screen API for cursor — free, no PowerShell spawn.
    let cursor: { x: number; y: number } | null = null
    try { const p = screen.getCursorScreenPoint(); cursor = { x: p.x, y: p.y } } catch {}
    if (!cursor) return
    const sprite = petSpriteRect()
    const b = win.getBounds()
    const dx = sprite ? sprite.x - b.x : 0
    const dy = sprite ? sprite.y - b.y : 0
    const sw = sprite ? sprite.width : b.width
    const sh = sprite ? sprite.height : b.height
    // Land a short distance away from the cursor (not on top of it, so the
    // user can still click things) and clamp to the display containing it.
    let wantWinX = Math.round(cursor.x + 24 - dx)
    let wantWinY = Math.round(cursor.y - sh - dy - 12)
    try {
      const wa = screen.getDisplayMatching({ x: cursor.x, y: cursor.y, width: 1, height: 1 }).workArea
      const minX = wa.x - dx + 4
      const maxX = wa.x + wa.width - sw - dx - 4
      const minY = wa.y - dy + 4
      const maxY = wa.y + wa.height - sh - dy - 4
      if (wantWinY < minY) wantWinY = Math.round(cursor.y + 24 - dy)
      wantWinX = Math.max(minX, Math.min(maxX, wantWinX))
      wantWinY = Math.max(minY, Math.min(maxY, wantWinY))
    } catch {}
    log(`[attention] approaching cursor at ${cursor.x},${cursor.y} → ${wantWinX},${wantWinY}`)
    deps.emit({ kind: 'antics', phase: 'attention-nudge', ts: Date.now() })
    await tweenWin(wantWinX, wantWinY, 700)
    // Preview the reply if it's short enough, otherwise a generic prompt.
    const preview = (replyText || '').trim().replace(/\s+/g, ' ').slice(0, 42)
    const msg = preview.length > 6
      ? `完成啦~ 快看看 → ${preview}${preview.length >= 42 ? '…' : ''}`
      : pick(NUDGE_QUIPS_NO_TEXT)
    deps.showPetBubble?.(msg)
  }

  async function nabForAttention(): Promise<void> {
    const sprite = petSpriteRect()
    if (!sprite) return
    const cx = sprite.x + sprite.width / 2
    const cy = sprite.y + sprite.height / 2
    deps.emit({ kind: 'antics', phase: 'attention-nab', ts: Date.now() })
    deps.showPetBubble?.(pick(NAB_QUIPS_ATTENTION))
    // Pull the cursor to the pet, then release a little away.
    await cursorPull(cx, cy, 10, 22)
    await setCursorPos(cx + 50, cy + 30)
  }

  function onNewReply(): void {
    attentionPhase = 'idle'
    nudgeStartedAt = 0
  }

  // ── behavior state machine ───────────────────────────────────────────────
  // Replaces the old crawlTick with a proper weighted-random behavior system
  // modeled on desktop-pet (window lifecycle) and PyGoose (watch mouse).
  type Behavior = 'idle' | 'walk' | 'watch' | 'window' | 'sleep'
  type WinPhase = 'approach' | 'climb' | 'walk' | 'sleep' | 'dangle' | 'leap'
  const REST_STATES = new Set<Behavior>(['idle', 'sleep'])
  const BEH_WEIGHTS: Array<{ beh: Behavior; weight: number }> = [
    { beh: 'idle', weight: 3 },
    { beh: 'walk', weight: 2 },
    { beh: 'watch', weight: 3 },
    { beh: 'window', weight: 2 },
    { beh: 'sleep', weight: 1 },
  ]
  let behavior: Behavior = 'idle'
  let behaviorEndsAt = 0
  let lastBehavior: Behavior | null = null
  let facing = 1 // 1=right, -1=left

  // window-perch sub-state
  let winPhase: WinPhase = 'approach'
  let winStepsLeft = 0
  let winHwnd = ''
  let winRect: { x: number; y: number; w: number; h: number } | null = null
  let dangleUntil = 0 // window-edge dangle end timestamp (0 = not dangling)
  let winSleepUntil = 0 // window-edge sleep end timestamp (0 = not asleep)

  function pickBehavior(): Behavior {
    const options = BEH_WEIGHTS.filter((o) => o.beh !== lastBehavior || REST_STATES.has(o.beh))
    const total = options.reduce((s, o) => s + o.weight, 0)
    let r = Math.random() * total
    for (const o of options) {
      r -= o.weight
      if (r <= 0) return o.beh
    }
    return 'idle'
  }

  // ── lifecycle ─────────────────────────────────────────────────────────────
  function start(): void {
    if (running) return
    running = true
    presenceTimer = setInterval(() => { void presenceTick() }, 5000)
    behaviorTimer = setInterval(() => { void behaviorTick() }, 3000)
    nabTimer = setInterval(() => { void nabTick() }, 3000)
    if (presenceTimer.unref) presenceTimer.unref()
    if (behaviorTimer.unref) behaviorTimer.unref()
    if (nabTimer.unref) nabTimer.unref()
  }

  function stop(): void {
    running = false
    if (presenceTimer) { clearInterval(presenceTimer); presenceTimer = null }
    if (behaviorTimer) { clearInterval(behaviorTimer); behaviorTimer = null }
    if (nabTimer) { clearInterval(nabTimer); nabTimer = null }
  }

  // Attention timer runs INDEPENDENTLY of the mischief toggle — the user
  // explicitly asked for the "nudge me after a reply" behavior to always work.
  attentionTimer = setInterval(() => { void attentionTick() }, 5000)
  if (attentionTimer.unref) attentionTimer.unref()

  function shutdown(): void {
    stop()
    if (attentionTimer) { clearInterval(attentionTimer); attentionTimer = null }
  }

  return {
    start,
    stop: shutdown,
    runNow,
    perchAt,
    onNewReply,
    get busy() { return episode !== null },
  }
}

export type Antics = ReturnType<typeof createAntics>
