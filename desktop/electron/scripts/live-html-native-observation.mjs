// Diagnostic metadata only: page content and capability URL tokens stay in the client.
export async function installNativeObservation(electronApp) {
  await electronApp.evaluate(({ app, webContents }) => {
    const state = { events: [], incomplete: false }
    globalThis.__htmlJourneyNativeObservation = state
    const append = row => {
      if (state.events.length < 10000) state.events.push({ at: Date.now(), ...row })
      else state.incomplete = true
    }
    const watch = contents => {
      const id = contents.id
      contents.on('did-fail-load', (_event, code, description, _url, mainFrame) => {
        append({ event: 'did-fail-load', id, code, mainFrame, reason: /^ERR_[A-Z_]+$/.test(description) ? description : 'LOAD_FAILED' })
      })
      contents.on('render-process-gone', (_event, detail) => append({ event: 'render-process-gone', id, reason: detail.reason, exitCode: detail.exitCode }))
      contents.on('destroyed', () => append({ event: 'destroyed', id }))
      contents.on('console-message', (_event, levelOrDetails, message) => {
        const detail = typeof levelOrDetails === 'object' ? levelOrDetails : { level: levelOrDetails, message }
        if (detail.level !== 'error' && detail.level !== 3) return
        const code = String(detail.message || '').match(/\bERR_[A-Z_]{1,80}\b/)?.[0]
        append({ event: 'console-error', id, code: code || 'RENDERER_CONSOLE_ERROR' })
      })
    }
    for (const contents of webContents.getAllWebContents()) watch(contents)
    app.on('web-contents-created', (_event, contents) => watch(contents))
  })
}

export async function nativeObservation(electronApp) {
  return electronApp.evaluate(({ BrowserWindow, webContents }) => {
    const urlShape = value => {
      try {
        const url = new URL(value)
        if (!['http:', 'https:', 'about:'].includes(url.protocol)) return { protocol: url.protocol, path: '[redacted]' }
        return { protocol: url.protocol, path: url.pathname.replace(/(\/api\/v1\/artifact-preview\/)[^/]+/, '$1[capability]').replace(/(\/api\/v1\/artifact-preview-leases\/)[^/]+/, '$1[lease]') }
      } catch { return { protocol: '', path: '' } }
    }
    const windows = BrowserWindow.getAllWindows().map(owner => ({ id: owner.id, visible: owner.isVisible(), focused: owner.isFocused(), minimized: owner.isMinimized(), bounds: owner.getBounds(), mainWebContentsId: owner.webContents.id }))
    const contents = webContents.getAllWebContents().map(item => {
      if (item.isDestroyed()) return { id: item.id, destroyed: true }
      const owner = item.getOwnerBrowserWindow()
      const view = owner?.contentView.children.find(child => child.webContents?.id === item.id)
      return { id: item.id, type: item.getType(), destroyed: false, ownerId: owner?.id ?? null, ownerFocused: owner?.isFocused() ?? null, focused: item.isFocused(), visible: view?.getVisible?.() ?? null, bounds: view?.getBounds?.() ?? null, loading: item.isLoading(), ...urlShape(item.getURL()) }
    })
    return { windows, contents, ...(globalThis.__htmlJourneyNativeObservation || { events: [], incomplete: true }) }
  })
}
