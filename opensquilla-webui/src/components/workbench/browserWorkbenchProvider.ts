import type {
  NativeWorkbenchSurfaceEvent,
  NativeWorkbenchSurfaceRectRequest,
  Platform,
} from '@/platform/types'
import {
  browserUrlFromWorkbenchItem,
  normalizeBrowserUrl,
} from '@/workbench/browserItems'
import type {
  NativeSurfaceRect,
  WorkbenchComponentEvent,
  WorkbenchItem,
  WorkbenchPanelDefinition,
  WorkbenchPanelRuntime,
  WorkbenchRuntimeContext,
} from '@/workbench/types'
import BrowserPreviewPanel from './BrowserPreviewPanel.vue'

interface BrowserAction {
  action: 'back' | 'forward' | 'reload' | 'stop' | 'navigate' | 'open-external'
    | 'find-open' | 'find-close' | 'find' | 'find-next' | 'find-stop' | 'zoom' | 'download-open'
  url?: string
  query?: string
  forward?: boolean
  zoomFactor?: number
  downloadId?: string
}

export interface BrowserWorkbenchProviderOptions {
  confirmPermission(request: {
    permission: string
    requestingOrigin: string
  }): Promise<boolean>
  openExternal(url: string): void
  platform: Platform
  t(key: string, params?: Record<string, unknown>): string
}

function browserAction(event: WorkbenchComponentEvent): BrowserAction | null {
  if (event.type !== 'browser-action' || !event.payload || typeof event.payload !== 'object') {
    return null
  }
  const raw = event.payload as Record<string, unknown>
  const action = raw.action
  if (!['back', 'forward', 'reload', 'stop', 'navigate', 'open-external',
    'find-open', 'find-close', 'find', 'find-next', 'find-stop', 'zoom', 'download-open'].includes(
    String(action),
  )) return null
  return {
    action: action as BrowserAction['action'],
    ...(typeof raw.url === 'string' ? { url: raw.url } : {}),
    ...(typeof raw.query === 'string' && raw.query.length <= 512 ? { query: raw.query } : {}),
    ...(typeof raw.forward === 'boolean' ? { forward: raw.forward } : {}),
    ...(typeof raw.zoomFactor === 'number' && Number.isFinite(raw.zoomFactor)
      && raw.zoomFactor >= 0.5 && raw.zoomFactor <= 3 ? { zoomFactor: raw.zoomFactor } : {}),
    ...(typeof raw.downloadId === 'string' && /^download-[0-9a-f-]{36}$/.test(raw.downloadId)
      ? { downloadId: raw.downloadId } : {}),
  }
}

function isReadingAction(action: BrowserAction['action']): boolean {
  return ['find', 'find-next', 'find-stop', 'zoom', 'download-open'].includes(action)
}

class BrowserWorkbenchRuntime implements WorkbenchPanelRuntime {
  private created = false
  private adoptOnInitialize: boolean
  private item: WorkbenchItem
  private rejectedContextTargetRef = ''
  private rect: NativeSurfaceRect | null = null

  constructor(
    item: WorkbenchItem,
    private readonly context: WorkbenchRuntimeContext,
    private readonly options: BrowserWorkbenchProviderOptions,
  ) {
    this.item = item
    this.adoptOnInitialize = item.payload.adoptedNativeSurface === true
    this.context.updateRenderState({
      canGoBack: false,
      canGoForward: false,
      currentUrl: browserUrlFromWorkbenchItem(item),
      errorMessage: '',
      loading: true,
      findOpen: false,
      findQuery: '',
      findMatches: null,
      findActiveMatch: 0,
      zoomFactor: 1,
      downloadId: '',
      downloadName: '',
      downloadState: '',
      downloadReceivedBytes: 0,
      downloadTotalBytes: 0,
      controlError: '',
    })
  }

  async initialize() {
    const adopt = this.adoptOnInitialize
    this.adoptOnInitialize = false
    const native = this.context.nativeWorkbenchApi
    this.context.updateRenderState({ errorMessage: '', loading: true })
    try {
      if (!native) throw new Error('The side browser requires OpenSquilla Desktop.')
      const capabilities = native.getCapabilities
        ? await native.getCapabilities()
        : { protocolVersions: [1] }
      if (!capabilities.protocolVersions.includes(2)) {
        throw new Error('Update OpenSquilla Desktop to use the side browser.')
      }
      const url = browserUrlFromWorkbenchItem(this.item)
      if (!adopt) {
        const contextTargetRef = typeof this.item.payload.contextTargetRef === 'string'
          ? this.item.payload.contextTargetRef : ''
        const scopeId = this.item.scope.type === 'session' ? this.item.scope.id : 'app'
        let result = await native.createSurface({
          version: 2,
          surfaceId: this.item.id,
          kind: 'url-preview',
          payload: {
            url,
            scopeId,
            ...(contextTargetRef ? { contextTargetRef } : {}),
          },
        })
        if (!result.ok && result.code === 'TARGET_NOT_FOUND' && contextTargetRef) {
          this.rejectedContextTargetRef = contextTargetRef
          const { contextTargetRef: _stale, ...payload } = this.item.payload
          this.item = { ...this.item, payload }
          result = await native.createSurface({ version: 2, surfaceId: this.item.id,
            kind: 'url-preview', payload: { url, scopeId } })
        }
        if (!result.ok) {
          throw new Error(result.message || 'Could not open the side browser.')
        }
        if (result.navigationError) this.showNavigationFailure(result.navigationError.message, result.navigationError.url)
      } else {
        const failure = this.item.payload.navigationError as { message?: string; url?: string } | undefined
        if (failure?.message) this.showNavigationFailure(failure.message, failure.url)
      }
      if (!this.context.isItemOpen()) {
        await this.hideAndDestroySurface()
        return
      }
      this.created = true
      if (adopt) this.context.updateRenderState({ loading: false })
      await this.syncRect()
    } catch (error) {
      await this.failSurface(error)
    }
  }

  update(item: WorkbenchItem) {
    if (this.rejectedContextTargetRef
      && item.payload.contextTargetRef === this.rejectedContextTargetRef) {
      const { contextTargetRef: _stale, ...payload } = item.payload
      this.item = { ...item, payload }
      return
    }
    this.item = item
  }

  async handleComponentEvent(event: WorkbenchComponentEvent) {
    const request = browserAction(event)
    if (!request) return
    const native = this.context.nativeWorkbenchApi
    if (request.action === 'find-open') {
      this.context.updateRenderState({ findOpen: true, controlError: '' })
      return
    }
    if (request.action === 'find-close') {
      this.context.updateRenderState({ findOpen: false, findQuery: '', findMatches: null,
        findActiveMatch: 0, controlError: '' })
    }
    const action = request.action === 'find-close' ? 'find-stop' : request.action
    if (action === 'find' && request.query === undefined) return
    if (action === 'find-next' && !String(this.context.getRenderState().findQuery || '')) return
    if (action === 'zoom' && request.zoomFactor === undefined) return
    if (action === 'download-open' && request.downloadId === undefined) return
    if (action === 'find') {
      this.context.updateRenderState({ findQuery: request.query, findMatches: null,
        findActiveMatch: 0, controlError: '' })
    } else if (action === 'find-stop') {
      this.context.updateRenderState({ findQuery: '', findMatches: null,
        findActiveMatch: 0, controlError: '' })
    }
    if (
      action === 'reload'
      && Boolean(this.context.getRenderState().errorMessage)
      && !this.created
    ) {
      await this.hideAndDestroySurface()
      if (!this.context.isItemOpen()) return
      this.context.updateRenderState({ errorMessage: '', loading: true })
      await this.initialize()
      return
    }
    if (action === 'open-external') {
      const current = String(this.context.getRenderState().currentUrl || '')
      if (normalizeBrowserUrl(current)) this.options.openExternal(current)
      return
    }
    if (!native?.navigateSurface) {
      if (isReadingAction(action)) this.context.updateRenderState({
        controlError: this.options.t('workbench.browser.controlUnavailable'),
      })
      return
    }
    const url = action === 'navigate' ? normalizeBrowserUrl(request.url || '') : ''
    if (action === 'navigate' && !url) return
    try {
      const result = await native.navigateSurface({
        version: 2,
        surfaceId: this.item.id,
        action,
        ...(url ? { url } : {}),
        ...(action === 'find' ? { query: request.query } : {}),
        ...(action === 'find-next' ? { forward: request.forward !== false } : {}),
        ...(action === 'zoom' ? { zoomFactor: request.zoomFactor } : {}),
        ...(action === 'download-open' ? { downloadId: request.downloadId } : {}),
      })
      if (!result.ok) {
        if (isReadingAction(action)) {
          this.context.updateRenderState({ controlError: result.message
            || this.options.t('workbench.browser.controlUnavailable') })
          return
        }
        if (result.navigationError) {
          this.showNavigationFailure(result.navigationError.message, result.navigationError.url)
          return
        }
        const error = new Error(result.message || this.options.t('workbench.browser.failedDetail'))
        if (result.code === 'TARGET_NOT_FOUND' || result.code === 'BROWSER_CRASHED') {
          await this.failSurface(error)
          return
        }
        throw error
      }
      if (isReadingAction(action)) {
        this.context.updateRenderState({ controlError: '',
          ...(action === 'zoom' ? { zoomFactor: request.zoomFactor } : {}) })
      } else {
        this.context.updateRenderState({ errorMessage: '', controlError: '' })
      }
    } catch (error) {
      if (isReadingAction(action)) {
        this.context.updateRenderState({ controlError: error instanceof Error ? error.message
          : this.options.t('workbench.browser.controlUnavailable') })
        return
      }
      this.showNavigationFailure(error instanceof Error ? error.message : this.options.t('workbench.browser.failedDetail'))
      if (this.rect) await this.setRect({ ...this.rect, visible: false })
    }
  }

  async handleNativeSurfaceEvent(event: NativeWorkbenchSurfaceEvent) {
    if (!this.created) return
    if (event.type === 'escape') {
      if (this.context.getRenderState().findOpen === true) {
        await this.handleComponentEvent({ type: 'browser-action', payload: { action: 'find-close' } })
        return
      }
      this.context.setExpanded(false)
      return
    }
    if (event.type === 'find-requested') {
      this.context.updateRenderState({ findOpen: true, controlError: '' })
      return
    }
    if (event.type === 'find-state') {
      const detail = event.detail
      const currentQuery = String(this.context.getRenderState().findQuery || '')
      if (detail?.zoomFactor !== undefined) {
        this.context.updateRenderState({ zoomFactor: detail.zoomFactor })
      }
      if (detail?.findQuery !== undefined && detail.findQuery !== currentQuery) return
      if (detail?.findMatches !== undefined || detail?.findActiveMatch !== undefined) {
        this.context.updateRenderState({
          ...(detail.findMatches !== undefined ? { findMatches: detail.findMatches } : {}),
          ...(detail.findActiveMatch !== undefined ? { findActiveMatch: detail.findActiveMatch } : {}),
        })
      }
      return
    }
    if (event.type === 'download-state') {
      const detail = event.detail
      if (!detail?.downloadId || !detail.downloadState) return
      const currentId = String(this.context.getRenderState().downloadId || '')
      if (detail.downloadState === 'progressing' || detail.downloadId === currentId) {
        this.context.updateRenderState({ downloadId: detail.downloadId,
          downloadName: detail.downloadName || '', downloadState: detail.downloadState,
          downloadReceivedBytes: detail.receivedBytes || 0,
          downloadTotalBytes: detail.totalBytes || 0 })
      }
      return
    }
    if (event.type === 'loading') {
      this.context.updateRenderState({ loading: true, errorMessage: '' })
      return
    }
    if (event.type === 'ready') {
      this.context.updateRenderState({ loading: false, errorMessage: '' })
      return
    }
    if (event.type === 'navigation-state') {
      const changedPage = Boolean(event.detail?.url
        && event.detail.url !== this.context.getRenderState().currentUrl)
      this.context.updateRenderState({
        canGoBack: event.detail?.canGoBack === true,
        canGoForward: event.detail?.canGoForward === true,
        currentUrl: event.detail?.url || '',
        loading: event.detail?.loading === true,
        pageTitle: event.detail?.title || '',
        ...(changedPage ? { findMatches: null, findActiveMatch: 0 } : {}),
        ...(event.detail?.navigationError !== undefined
          ? { errorMessage: event.detail.navigationError?.message || '' } : {}),
      })
      return
    }
    if (event.type === 'permission-request') {
      const requestId = event.detail?.requestId || ''
      const native = this.context.nativeWorkbenchApi
      if (!requestId || !native?.respondToPermission) return
      const allow = await this.options.confirmPermission({
        permission: event.detail?.permission || 'unknown',
        requestingOrigin: event.detail?.requestingOrigin || '',
      })
      await native.respondToPermission({
        version: 2,
        surfaceId: this.item.id,
        requestId,
        allow,
      })
      return
    }
    if (
      event.type === 'error'
      || event.type === 'crashed'
      || event.type === 'unresponsive'
    ) {
      await this.failSurface(new Error(
        event.detail?.message
          || event.detail?.reason
          || this.options.t('workbench.browser.failedDetail'),
      ))
    }
  }

  async handleSurfaceRect(rect: NativeSurfaceRect) {
    this.rect = rect
    await this.syncRect()
  }

  async suspend() {
    if (this.rect) await this.setRect({ ...this.rect, visible: false })
  }

  async resume() {
    await this.syncRect()
  }

  async dispose() {
    await this.hideAndDestroySurface()
  }

  private async syncRect() {
    if (this.rect) await this.setRect(this.rect)
  }

  private async setRect(rect: NativeSurfaceRect) {
    if (!this.created || !this.context.nativeWorkbenchApi) return
    const request: NativeWorkbenchSurfaceRectRequest = {
      surfaceId: this.item.id,
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      visible: rect.visible,
    }
    try {
      const positioned = await this.context.nativeWorkbenchApi.setSurfaceRect(request)
      if (!positioned.ok) {
        throw new Error(positioned.message || 'Could not position the side browser.')
      }
      if (request.visible) {
        const activated = await this.context.nativeWorkbenchApi.activateSurface(this.item.id)
        // A tab may suspend while its layout request is pending. The scoped
        // API keeps that page hidden; it must remain available for the next resume.
        if (!activated.ok && activated.message !== 'Workbench surface is no longer active') {
          throw new Error(activated.message || 'Could not activate the side browser.')
        }
      }
    } catch (error) {
      await this.failSurface(error)
    }
  }

  private async failSurface(error: unknown) {
    const message = error instanceof Error
      ? error.message
      : this.options.t('workbench.browser.failedDetail')
    if (this.context.isItemOpen()) {
      this.context.updateRenderState({
        errorMessage: message || this.options.t('workbench.browser.failedDetail'),
        loading: false,
      })
      this.context.reportError(error)
    }
    await this.hideAndDestroySurface()
  }

  private showNavigationFailure(message: string, url?: string) {
    this.context.updateRenderState({ errorMessage: message, loading: false,
      ...(url ? { currentUrl: url } : {}) })
  }

  private async hideAndDestroySurface() {
    const native = this.context.nativeWorkbenchApi
    this.created = false
    if (!native) return
    if (this.rect) {
      try {
        await native.setSurfaceRect({
          surfaceId: this.item.id,
          x: this.rect.x,
          y: this.rect.y,
          width: this.rect.width,
          height: this.rect.height,
          visible: false,
        })
      } catch {}
    }
    try {
      await native.destroySurface(this.item.id)
    } catch {}
  }
}

export function createBrowserWorkbenchDefinition(
  options: BrowserWorkbenchProviderOptions,
): WorkbenchPanelDefinition {
  return {
    kind: 'browser',
    component: BrowserPreviewPanel,
    supports: item => item.kind === 'browser' && Boolean(browserUrlFromWorkbenchItem(item)),
    getHeader: (item, state) => ({
      icon: 'languages',
      title: String(state.runtimeState.pageTitle || item.title),
      subtitle: String(state.runtimeState.currentUrl || browserUrlFromWorkbenchItem(item)),
    }),
    getProps: (_item, state) => ({
      canGoBack: state.runtimeState.canGoBack === true,
      canGoForward: state.runtimeState.canGoForward === true,
      currentUrl: String(state.runtimeState.currentUrl || ''),
      errorMessage: String(state.runtimeState.errorMessage || ''),
      loading: state.runtimeState.loading === true,
      findOpen: state.runtimeState.findOpen === true,
      findQuery: String(state.runtimeState.findQuery || ''),
      findMatches: typeof state.runtimeState.findMatches === 'number'
        ? state.runtimeState.findMatches : null,
      findActiveMatch: Number(state.runtimeState.findActiveMatch || 0),
      zoomFactor: Number(state.runtimeState.zoomFactor || 1),
      downloadId: String(state.runtimeState.downloadId || ''),
      downloadName: String(state.runtimeState.downloadName || ''),
      downloadState: String(state.runtimeState.downloadState || ''),
      downloadReceivedBytes: Number(state.runtimeState.downloadReceivedBytes || 0),
      downloadTotalBytes: Number(state.runtimeState.downloadTotalBytes || 0),
      controlError: String(state.runtimeState.controlError || ''),
    }),
    async createRuntime(item, context) {
      const runtime = new BrowserWorkbenchRuntime(item, context, options)
      await runtime.initialize()
      return runtime
    },
  }
}
