import { setTimeout as delay } from 'node:timers/promises'

/** Acquire the exact control page's native owner once, then observe without retries. */
export async function requireDesktopForeground(electronApp, controlPage, { timeoutMs = 5000, now = Date.now, pause = delay } = {}) {
  const handle = await electronApp.browserWindow(controlPage)
  let last
  try {
    const ownerId = await handle.evaluate(owner => owner.id)
    await electronApp.evaluate(({ app, BrowserWindow }, id) => {
      const owner = BrowserWindow.fromId(id)
      if (!owner || owner.isDestroyed() || owner.webContents.isDestroyed()) throw new Error('DESKTOP_FOREGROUND_OWNER_LOST')
      if (owner.isMinimized()) owner.restore()
      owner.show()
      if (process.platform === 'darwin') app.focus({ steal: true })
      owner.focus()
      owner.webContents.focus()
    }, ownerId)
    const deadline = now() + timeoutMs
    do {
      last = await handle.evaluate(owner => {
        if (owner.isDestroyed() || owner.webContents.isDestroyed()) return { destroyed: true }
        return { ownerId: owner.id, webContentsId: owner.webContents.id, visible: owner.isVisible(), minimized: owner.isMinimized(), ownerFocused: owner.isFocused(), contentsFocused: owner.webContents.isFocused() }
      })
      if (last.destroyed) throw new Error('DESKTOP_FOREGROUND_OWNER_LOST')
      if (last.visible && !last.minimized && last.ownerFocused && last.contentsFocused) return last
      if (now() >= deadline) break
      await pause(50)
    } while (true)
    const error = new Error('DESKTOP_FOREGROUND_REQUIRED')
    error.diagnostic = { reason: 'Exact native owner and control WebContents did not both receive foreground focus.', timeoutMs, observed: last }
    throw error
  } finally {
    await handle.dispose()
  }
}

/** Gate only user-action methods; observations never steal focus. */
export function withForegroundActions(target, names, beforeAction) {
  return new Proxy(target, {
    get(object, key) {
      const value = Reflect.get(object, key)
      if (!names.includes(key) || typeof value !== 'function') return value
      return async (...args) => {
        await beforeAction(String(key))
        return value.apply(object, args)
      }
    },
  })
}
