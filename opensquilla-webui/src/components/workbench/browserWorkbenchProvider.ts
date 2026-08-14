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
  url?: string
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
  if (!['back', 'forward', 'reload', 'stop', 'navigate', 'open-external'].includes(
    String(action),
  )) return null
  return {
    action: action as BrowserAction['action'],
    ...(typeof raw.url === 'string' ? { url: raw.url } : {}),
  }
}

class BrowserWorkbenchRuntime implements WorkbenchPanelRuntime {
  private created = false
  private item: WorkbenchItem
  private rect: NativeSurfaceRect | null = null

  constructor(
    item: WorkbenchItem,
    private readonly context: WorkbenchRuntimeContext,
    private readonly options: BrowserWorkbenchProviderOptions,
  ) {
    this.item = item
    this.context.updateRenderState({
      canGoBack: false,
      canGoForward: false,
      currentUrl: browserUrlFromWorkbenchItem(item),
      errorMessage: '',
      loading: true,
    })
  }

  async initialize() {
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
      const result = await native.createSurface({
        version: 2,
        surfaceId: this.item.id,
        kind: 'url-preview',
        payload: {
          url,
          scopeId: this.item.scope.type === 'session' ? this.item.scope.id : 'app',
        },
      })
      if (!result.ok) {
        throw new Error(result.message || 'Could not open the side browser.')
      }
      if (!this.context.isItemOpen()) {
        await this.hideAndDestroySurface()
        return
      }
      this.created = true
      await this.syncRect()
    } catch (error) {
      await this.failSurface(error)
    }
  }

  update(item: WorkbenchItem) {
    this.item = item
  }

  async handleComponentEvent(event: WorkbenchComponentEvent) {
    const request = browserAction(event)
    if (!request) return
    const native = this.context.nativeWorkbenchApi
    if (
      request.action === 'reload'
      && Boolean(this.context.getRenderState().errorMessage)
    ) {
      await this.hideAndDestroySurface()
      if (!this.context.isItemOpen()) return
      this.context.updateRenderState({ errorMessage: '', loading: true })
      await this.initialize()
      return
    }
    if (!native?.navigateSurface) return
    if (request.action === 'open-external') {
      const current = String(this.context.getRenderState().currentUrl || '')
      if (normalizeBrowserUrl(current)) this.options.openExternal(current)
      return
    }
    const url = request.action === 'navigate' ? normalizeBrowserUrl(request.url || '') : ''
    if (request.action === 'navigate' && !url) return
    try {
      const result = await native.navigateSurface({
        version: 2,
        surfaceId: this.item.id,
        action: request.action,
        ...(url ? { url } : {}),
      })
      if (!result.ok) {
        throw new Error(result.message || this.options.t('workbench.browser.failedDetail'))
      }
      this.context.updateRenderState({ errorMessage: '' })
    } catch (error) {
      await this.failSurface(error)
    }
  }

  async handleNativeSurfaceEvent(event: NativeWorkbenchSurfaceEvent) {
    if (!this.created) return
    if (event.type === 'escape') {
      this.context.setExpanded(false)
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
      this.context.updateRenderState({
        canGoBack: event.detail?.canGoBack === true,
        canGoForward: event.detail?.canGoForward === true,
        currentUrl: event.detail?.url || '',
        loading: event.detail?.loading === true,
        pageTitle: event.detail?.title || '',
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
        if (!activated.ok) {
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
    }),
    async createRuntime(item, context) {
      const runtime = new BrowserWorkbenchRuntime(item, context, options)
      await runtime.initialize()
      return runtime
    },
  }
}
