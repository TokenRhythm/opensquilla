import { contextBridge, ipcRenderer } from 'electron'

// ── Pet bridge (embedded desktop pet) ───────────────────────────────────────
// Mirrors OpenSquilla pet's preload.js contract so the pet renderer (pet/pet.html) works
// as-is. Channel names use pet:/panel: prefixes and never collide with the
// opensquillaDesktop bridge below.
contextBridge.exposeInMainWorld('pet', {
  onEvent: (cb: (data: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('pet:event', listener)
    return () => ipcRenderer.removeListener('pet:event', listener)
  },
  onBubble: (cb: (data: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('pet:bubble', listener)
    return () => ipcRenderer.removeListener('pet:bubble', listener)
  },
  onStats: (cb: (data: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('pet:stats', listener)
    return () => ipcRenderer.removeListener('pet:stats', listener)
  },
  onMeme: (cb: (data: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('pet:meme', listener)
    return () => ipcRenderer.removeListener('pet:meme', listener)
  },
  onTravel: (cb: (data: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('pet:travel', listener)
    return () => ipcRenderer.removeListener('pet:travel', listener)
  },
  onMemeCatalogChanged: (cb: (data: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('pet:meme-catalog-changed', listener)
    return () => ipcRenderer.removeListener('pet:meme-catalog-changed', listener)
  },
  onPanelStats: (cb: (data: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('panel:stats', listener)
    return () => ipcRenderer.removeListener('panel:stats', listener)
  },
  onConfig: (cb: (data: unknown) => void) => {
    const a = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('pet:config', a)
    ipcRenderer.on('panel:config', a)
    return () => {
      ipcRenderer.removeListener('pet:config', a)
      ipcRenderer.removeListener('panel:config', a)
    }
  },
  onPrice: (cb: (data: unknown) => void) => {
    const listener = (_e: Electron.IpcRendererEvent, data: unknown) => cb(data)
    ipcRenderer.on('panel:price', listener)
    return () => ipcRenderer.removeListener('panel:price', listener)
  },
  getConfig: () => ipcRenderer.invoke('get-config'),
  getStats: () => ipcRenderer.invoke('get-stats'),
  openPanel: () => ipcRenderer.send('open-panel'),
  closePanel: () => ipcRenderer.send('close-panel'),
  setMode: (m: unknown) => ipcRenderer.send('set-mode', m),
  setSkin: (s: unknown) => ipcRenderer.send('set-skin', s),
  setBudget: (v: unknown) => ipcRenderer.send('set-budget', v),
  toggleMute: () => ipcRenderer.send('toggle-mute'),
  setSessionPrefs: (pinned: unknown, archived: unknown) => ipcRenderer.send('set-session-prefs', pinned, archived),
  quit: () => ipcRenderer.send('quit-app'),
  closePet: () => ipcRenderer.send('close-pet'),
  getWinPos: () => ipcRenderer.invoke('get-win-pos'),
  getWindowMetrics: () => ipcRenderer.invoke('get-window-metrics'),
  setWinPos: (x: number, y: number) => ipcRenderer.send('set-win-pos', x, y),
  newChat: () => ipcRenderer.send('new-chat'),
  decidePermission: (permId: unknown, behavior: unknown) => ipcRenderer.send('permission-decide', permId, behavior),
  focusSession: (sessionId: unknown) => ipcRenderer.send('focus-session', sessionId),
  getMemeCatalog: () => ipcRenderer.invoke('meme-catalog'),
  triggerMeme: (sessionId: unknown, memeId: unknown) => ipcRenderer.invoke('meme-trigger', sessionId, memeId),
  getTravel: () => ipcRenderer.invoke('travel-get'),
  getTravelPostcards: () => ipcRenderer.invoke('travel-postcards'),
  startTravel: (sessionId: unknown, templateId: unknown, mission: unknown) => ipcRenderer.invoke('travel-start', sessionId, templateId, mission),
  wanderTravel: () => ipcRenderer.invoke('travel-wander'),
  cancelTravel: () => ipcRenderer.invoke('travel-cancel'),
  primaryAction: () => ipcRenderer.send('primary-action'),
  setIgnoreMouse: (ignore: unknown) => ipcRenderer.send('set-ignore-mouse', ignore),
  setPetTall: (tall: unknown) => ipcRenderer.send('pet-tall', tall),
  setPetBig: (on: unknown) => ipcRenderer.send('pet-big', on),
  setPetSize: (w: unknown, h: unknown, anchor: unknown) => ipcRenderer.send('set-pet-size', w, h, anchor),
  setPanelHeight: (h: unknown) => ipcRenderer.send('set-panel-height', h),
  focusPet: () => ipcRenderer.send('pet-focus'),
  blurPet: () => ipcRenderer.send('pet-blur'),
  openLog: () => ipcRenderer.send('open-log'),
  petLog: (tag: unknown, msg: unknown) => ipcRenderer.send('pet-log', tag, msg),
  uiBusy: (on: unknown) => ipcRenderer.send('ui-busy', on),
  petVisualBounds: (rect: unknown) => ipcRenderer.send('pet-visual-bounds', rect),
  petDragging: (on: unknown) => ipcRenderer.send('pet-dragging', on),
  anticsRunNow: () => ipcRenderer.send('antics-run-now'),
  anticsToggle: () => ipcRenderer.send('antics-toggle'),
})

contextBridge.exposeInMainWorld('opensquillaDesktop', {
  getOsLocale: () => ipcRenderer.invoke('desktop:os-locale'),
  isAutoUpdateEnabled: () => ipcRenderer.invoke('desktop:update:supported'),
  isDesktopUpdateManaged: () => ipcRenderer.invoke('desktop:update:managed'),
  getUpdateState: () => ipcRenderer.invoke('desktop:update:state'),
  checkForUpdates: () => ipcRenderer.invoke('desktop:update:check'),
  downloadUpdate: () => ipcRenderer.invoke('desktop:update:download'),
  relaunchToUpdate: () => ipcRenderer.invoke('desktop:update:relaunch'),
  dismissUpdate: () => ipcRenderer.invoke('desktop:update:dismiss'),
  getGatewayStatus: () => ipcRenderer.invoke('gateway:status'),
  getCliInvocation: () => ipcRenderer.invoke('gateway:cli-invocation'),
  revealGatewayLog: () => ipcRenderer.invoke('gateway:reveal-log'),
  getDesktopSettings: () => ipcRenderer.invoke('desktop:settings:get'),
  saveDesktopSettings: (payload: unknown) => ipcRenderer.invoke('desktop:settings:save', payload),
  resetDesktopSettings: () => ipcRenderer.invoke('desktop:settings:reset'),
  getDesktopPreferences: () => ipcRenderer.invoke('desktop:preferences:get'),
  saveDesktopPreferences: (payload: unknown) => ipcRenderer.invoke('desktop:preferences:save', payload),
  setNativeTheme: (payload: unknown) => ipcRenderer.invoke('desktop:theme:set', payload),
  openArtifact: (payload: unknown) => ipcRenderer.invoke('desktop:artifact:open', payload),
  chooseProjectDirectory: (payload: unknown) => (
    ipcRenderer.invoke('desktop:workspace:choose-directory', payload)
  ),
  getWorkbenchCapabilities: () => ipcRenderer.invoke('desktop:workbench:capabilities'),
  createArtifactPreviewLease: (payload: unknown) => (
    ipcRenderer.invoke('desktop:workbench:preview-lease:create', payload)
  ),
  renewArtifactPreviewLease: (payload: unknown) => (
    ipcRenderer.invoke('desktop:workbench:preview-lease:renew', payload)
  ),
  revokeArtifactPreviewLease: (payload: unknown) => (
    ipcRenderer.invoke('desktop:workbench:preview-lease:revoke', payload)
  ),
  createWorkbenchSurface: (payload: unknown) => (
    ipcRenderer.invoke('desktop:workbench:surface:create', payload)
  ),
  navigateWorkbenchSurface: (payload: unknown) => (
    ipcRenderer.invoke('desktop:workbench:surface:navigate', payload)
  ),
  respondToWorkbenchPermission: (payload: unknown) => (
    ipcRenderer.invoke('desktop:workbench:permission:respond', payload)
  ),
  setWorkbenchSurfaceRect: (payload: unknown) => (
    ipcRenderer.invoke('desktop:workbench:surface:set-rect', payload)
  ),
  activateWorkbenchSurface: (surfaceId: unknown) => (
    ipcRenderer.invoke('desktop:workbench:surface:activate', surfaceId)
  ),
  destroyWorkbenchSurface: (surfaceId: unknown) => (
    ipcRenderer.invoke('desktop:workbench:surface:destroy', surfaceId)
  ),
  getOnboardingDefaults: () => ipcRenderer.invoke('desktop:onboarding:defaults'),
  probeOnboarding: (payload: unknown) => ipcRenderer.invoke('desktop:onboarding:probe', payload),
  saveOnboarding: (payload: unknown) => ipcRenderer.invoke('desktop:onboarding:save', payload),
  cancelOnboarding: () => ipcRenderer.invoke('desktop:onboarding:cancel'),
  getBootState: () => ipcRenderer.invoke('desktop:boot:state'),
  retryStartup: () => ipcRenderer.invoke('desktop:boot:retry'),
  quitApp: () => ipcRenderer.invoke('desktop:boot:quit'),
  getRecoveryState: () => ipcRenderer.invoke('desktop:recovery:state'),
  retryProfileConsolidation: () => ipcRenderer.invoke('desktop:recovery:retry-consolidation'),
  chooseRecoveryWorkspace: (payload: unknown) => ipcRenderer.invoke('desktop:recovery:choose-workspace', payload),
  chooseLegacyAgentDataLocation: (payload: unknown) => ipcRenderer.invoke('desktop:recovery:choose-legacy-agent-data', payload),
  recoverProfileTransaction: () => ipcRenderer.invoke('desktop:recovery:recover-transaction'),
  revealRecoveryPath: (payload: unknown) => ipcRenderer.invoke('desktop:recovery:reveal-path', payload),
  copyRecoveryDiagnostics: () => ipcRenderer.invoke('desktop:recovery:copy-diagnostics'),
  openLatestDownloadPage: () => ipcRenderer.invoke('desktop:recovery:open-download'),
  inspectDesktopCleanup: (payload: unknown) => ipcRenderer.invoke('desktop:cleanup:inspect', payload),
  discardDesktopCleanup: (payload: unknown) => ipcRenderer.invoke('desktop:cleanup:discard', payload),
  applyDesktopCleanup: (payload: unknown) => ipcRenderer.invoke('desktop:cleanup:apply', payload),
  revealDesktopUserData: () => ipcRenderer.invoke('desktop:cleanup:reveal-user-data'),
  migrationSummary: (payload?: unknown) => ipcRenderer.invoke('desktop:migration:summary', payload),
  migrationBrowseSource: (payload: unknown) => ipcRenderer.invoke('desktop:migration:browse-source', payload),
  migrationRun: (payload: unknown) => ipcRenderer.invoke('desktop:migration:run', payload),
  migrationTakeLastResult: () => ipcRenderer.invoke('desktop:migration:last-result'),
  migrationPeekLastResult: () => ipcRenderer.invoke('desktop:migration:peek-last-result'),
  migrationDismissLastResult: () => ipcRenderer.invoke('desktop:migration:dismiss-last-result'),
  onBootStatus: (callback: (payload: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => callback(payload)
    ipcRenderer.on('desktop:boot:status', listener)
    return () => ipcRenderer.removeListener('desktop:boot:status', listener)
  },
  onBootError: (callback: (payload: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => callback(payload)
    ipcRenderer.on('desktop:boot:error', listener)
    return () => ipcRenderer.removeListener('desktop:boot:error', listener)
  },
  onRecoveryState: (callback: (payload: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => callback(payload)
    ipcRenderer.on('desktop:recovery:state-changed', listener)
    return () => ipcRenderer.removeListener('desktop:recovery:state-changed', listener)
  },
  onUpdateState: (callback: (payload: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => callback(payload)
    ipcRenderer.on('desktop:update:state-changed', listener)
    return () => ipcRenderer.removeListener('desktop:update:state-changed', listener)
  },
  onMigrationProgress: (callback: (payload: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => callback(payload)
    ipcRenderer.on('desktop:migration:progress', listener)
    return () => ipcRenderer.removeListener('desktop:migration:progress', listener)
  },
  onWindowHidden: (callback: () => void) => {
    const listener = () => callback()
    ipcRenderer.on('desktop:window:hidden', listener)
    return () => ipcRenderer.removeListener('desktop:window:hidden', listener)
  },
  onWorkbenchSurfaceEvent: (callback: (payload: unknown) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, payload: unknown) => callback(payload)
    ipcRenderer.on('desktop:workbench:surface-event', listener)
    return () => ipcRenderer.removeListener('desktop:workbench:surface-event', listener)
  },
})
