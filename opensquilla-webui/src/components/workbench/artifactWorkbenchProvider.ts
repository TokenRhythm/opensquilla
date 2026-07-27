import type { Platform } from '@/platform/types'
import type { ArtifactPayload } from '@/types/rpc'
import {
  fetchArtifactBlob,
  isActiveDocumentArtifactCandidate,
  openArtifactBlobUrl,
  openArtifactViaGateway,
} from '@/utils/chat/artifactAccess'
import {
  artifactFileSubtitle,
  artifactFileTitle,
  artifactIconName,
} from '@/utils/chat/artifacts'
import { downloadBlob } from '@/utils/browser'
import {
  artifactFromWorkbenchItem,
  artifactsFromWorkbenchItem,
  sessionKeyFromWorkbenchItem,
} from '@/workbench/artifactItems'
import type {
  NativeSurfaceRect,
  WorkbenchComponentEvent,
  WorkbenchItem,
  WorkbenchPanelDefinition,
  WorkbenchPanelRenderState,
  WorkbenchPanelRuntime,
  WorkbenchRuntimeContext,
  WorkbenchToolbarItem,
} from '@/workbench/types'
import type {
  NativeWorkbenchSurfaceEvent,
  NativeWorkbenchSurfaceRectRequest,
} from '@/platform/types'
import type {
  ArtifactPreviewResourceState,
  NativeHtmlArtifactResource,
} from '@/composables/workbench/useArtifactPreviewResource'
import ArtifactCollectionPanel from './ArtifactCollectionPanel.vue'
import ArtifactPreviewPanel from './ArtifactPreviewPanel.vue'

type Translate = (key: string, params?: Record<string, unknown>) => string

interface ArtifactPreviewPanelHandle {
  reload: () => Promise<void>
}

export interface ArtifactWorkbenchProviderOptions {
  authToken(): string
  baseOrigin: string
  confirmRemoteResources(): Promise<boolean>
  currentSessionId(): string
  openArtifact(
    artifact: ArtifactPayload,
    sessionKey: string,
    navigationArtifacts: readonly ArtifactPayload[],
  ): void
  platform: Platform
  pushToast(message: string, options?: { tone: 'danger' }): void
  t: Translate
}

function artifactEventPayload(event: WorkbenchComponentEvent): ArtifactPayload | null {
  if (!event.payload || typeof event.payload !== 'object') return null
  return event.payload as ArtifactPayload
}

function htmlResourcePayload(
  event: WorkbenchComponentEvent,
): NativeHtmlArtifactResource | null {
  if (!event.payload || typeof event.payload !== 'object') return null
  const payload = event.payload as Partial<NativeHtmlArtifactResource>
  return payload.data instanceof ArrayBuffer && payload.artifact
    ? payload as NativeHtmlArtifactResource
    : null
}

function previewStatePayload(
  event: WorkbenchComponentEvent,
): ArtifactPreviewResourceState | null {
  const state = event.payload
  return typeof state === 'string' && [
    'crashed',
    'error',
    'idle',
    'loading',
    'missing-resource',
    'offline',
    'ready',
    'suspended',
    'unsupported',
  ].includes(state)
    ? state as ArtifactPreviewResourceState
    : null
}

function surfaceError(operation: string, message?: string): Error {
  return new Error(message ? `${operation}: ${message}` : operation)
}

function artifactSessionKey(
  item: WorkbenchItem,
  options: ArtifactWorkbenchProviderOptions,
): string {
  return sessionKeyFromWorkbenchItem(item) || options.currentSessionId()
}

async function downloadArtifact(
  item: WorkbenchItem,
  artifact: ArtifactPayload,
  options: ArtifactWorkbenchProviderOptions,
) {
  const result = await fetchArtifactBlob(artifact, {
    authToken: options.authToken(),
    baseOrigin: options.baseOrigin,
    sessionKey: artifactSessionKey(item, options),
  })
  if (!result.ok) {
    options.pushToast(result.message || options.t('chat.toast.downloadFailed'), {
      tone: 'danger',
    })
    return
  }
  downloadBlob(result.blob, String(
    artifact.name || artifactFileTitle(artifact) || 'artifact',
  ))
}

async function openArtifactExternally(
  item: WorkbenchItem,
  artifact: ArtifactPayload,
  options: ArtifactWorkbenchProviderOptions,
) {
  const sessionKey = artifactSessionKey(item, options)
  const authToken = options.authToken()
  const { platform } = options
  if (platform.capabilities.canOpenArtifactsNatively && platform.files.openArtifact) {
    const fetched = await fetchArtifactBlob(artifact, {
      authToken,
      baseOrigin: options.baseOrigin,
      sessionKey,
    })
    if (!fetched.ok) {
      options.pushToast(fetched.message, { tone: 'danger' })
      return
    }
    const opened = await platform.files.openArtifact({
      data: await fetched.blob.arrayBuffer(),
      name: String(artifact.name || artifactFileTitle(artifact) || 'artifact'),
      mime: fetched.blob.type || String(artifact.mime || ''),
    })
    if (!opened.ok) {
      options.pushToast(
        opened.message || options.t('chat.toast.artifactOpenFailed'),
        { tone: 'danger' },
      )
    }
    return
  }

  const opened = isActiveDocumentArtifactCandidate(artifact)
    ? await openArtifactViaGateway(artifact, {
      authToken,
      baseOrigin: options.baseOrigin,
      sessionKey,
    })
    : await openArtifactBlobUrl(artifact, {
      authToken,
      baseOrigin: options.baseOrigin,
      sessionKey,
    })
  if (!opened.ok) options.pushToast(opened.message, { tone: 'danger' })
}

function runtimeStateValue<T>(
  state: WorkbenchPanelRenderState,
  key: string,
  fallback: T,
): T {
  const value = state.runtimeState[key]
  return value === undefined ? fallback : value as T
}

class ArtifactPreviewRuntime implements WorkbenchPanelRuntime {
  private component: ArtifactPreviewPanelHandle | null = null
  private createdSurface = false
  private generation = 0
  private item: WorkbenchItem
  private rect: NativeSurfaceRect | null = null
  private resource: NativeHtmlArtifactResource | null = null

  constructor(
    item: WorkbenchItem,
    private readonly context: WorkbenchRuntimeContext,
    private readonly options: ArtifactWorkbenchProviderOptions,
  ) {
    this.item = item
    this.context.updateRenderState({
      missingResources: false,
      nativeSurfaceState: 'loading',
      previewState: 'idle',
      remoteResourcesEnabled: false,
    })
  }

  setComponentHandle(handle: unknown) {
    this.component = handle
      && typeof handle === 'object'
      && 'reload' in handle
      && typeof (handle as ArtifactPreviewPanelHandle).reload === 'function'
      ? handle as ArtifactPreviewPanelHandle
      : null
  }

  update(item: WorkbenchItem) {
    this.item = item
  }

  async handleComponentEvent(event: WorkbenchComponentEvent, item: WorkbenchItem) {
    this.item = item
    if (event.type === 'artifact-download') {
      const artifact = artifactEventPayload(event)
      if (artifact) await downloadArtifact(item, artifact, this.options)
      return
    }
    if (event.type === 'artifact-external-open') {
      const artifact = artifactEventPayload(event)
      if (artifact) await openArtifactExternally(item, artifact, this.options)
      return
    }
    if (event.type === 'preview-state-change') {
      const state = previewStatePayload(event)
      if (state) {
        this.context.updateRenderState({ previewState: state })
        await this.handlePreviewStateChange(state)
      }
      return
    }
    if (event.type === 'native-html-ready') {
      const resource = htmlResourcePayload(event)
      if (resource) await this.createNativeSurface(resource)
    }
  }

  async performAction(actionId: string, item: WorkbenchItem) {
    this.item = item
    const artifact = artifactFromWorkbenchItem(item)
    if (actionId === 'refresh') {
      if (!await this.prepareForReload()) return
      await this.component?.reload()
    } else if (actionId === 'toggle-remote-resources') {
      const enabled = !this.remoteResourcesEnabled()
      if (enabled && !await this.options.confirmRemoteResources()) return
      if (!this.context.isItemOpen()) return
      this.context.updateRenderState({ remoteResourcesEnabled: enabled })
      if (this.resource) {
        const resource = this.resource
        if (!await this.releaseNativeSurface(false)) {
          await this.failNativeSurface(
            surfaceError('Failed to replace the native Workbench surface'),
          )
          return
        }
        await this.createNativeSurface(resource)
      } else {
        if (!await this.prepareForReload()) return
        await this.component?.reload()
      }
    } else if (actionId === 'open-external' && artifact) {
      await openArtifactExternally(item, artifact, this.options)
    } else if (actionId === 'download' && artifact) {
      await downloadArtifact(item, artifact, this.options)
    }
  }

  async handleSurfaceRect(rect: NativeSurfaceRect, item: WorkbenchItem) {
    this.item = item
    this.rect = rect
    await this.syncSurfaceRect()
  }

  async handleNativeSurfaceEvent(
    event: NativeWorkbenchSurfaceEvent,
    item: WorkbenchItem,
  ) {
    this.item = item
    if (!this.createdSurface) return
    if (event.type === 'escape') {
      this.context.setExpanded(false)
    } else if (event.type === 'missing-resource') {
      this.context.updateRenderState({ missingResources: true })
    } else if (event.type === 'loading') {
      this.context.updateRenderState({ nativeSurfaceState: 'loading' })
    } else if (event.type === 'ready') {
      this.context.updateRenderState({ nativeSurfaceState: 'ready' })
    } else if (event.type === 'error') {
      await this.showNativeFailure('error')
    } else if (event.type === 'crashed') {
      await this.showNativeFailure('crashed')
    }
  }

  async suspend() {
    if (!this.rect) return
    await this.setSurfaceRect({ ...this.rect, visible: false })
  }

  async resume() {
    await this.syncSurfaceRect()
  }

  async dispose() {
    this.component = null
    await this.releaseNativeSurface(true)
    this.rect = null
  }

  private remoteResourcesEnabled(): boolean {
    return this.context.getRenderState().remoteResourcesEnabled === true
  }

  private async createNativeSurface(resource: NativeHtmlArtifactResource) {
    const nativeApi = this.context.nativeWorkbenchApi
    if (!nativeApi || this.item.hostKind !== 'native-webcontents') return
    if (this.createdSurface && !await this.releaseNativeSurface(false)) {
      await this.failNativeSurface(
        surfaceError('Failed to replace the native Workbench surface'),
      )
      return
    }
    this.resource = resource
    const generation = this.generation + 1
    this.generation = generation
    this.createdSurface = true
    this.context.updateRenderState({
      missingResources: resource.hasRelativeResources,
      nativeSurfaceState: 'loading',
    })

    let result
    try {
      result = await nativeApi.createSurface({
        version: 1,
        surfaceId: this.item.id,
        kind: 'artifact-html',
        payload: {
          data: resource.data.slice(0),
          name: artifactFileTitle(resource.artifact),
          mime: 'text/html',
          scopeId: resource.sessionKey,
          allowRemoteResources: this.remoteResourcesEnabled(),
        },
      })
    } catch (error) {
      if (this.generation === generation && this.context.isItemOpen()) {
        await this.failNativeSurface(error)
      }
      return
    }
    if (this.generation !== generation) return
    if (!this.context.isItemOpen()) {
      this.createdSurface = false
      if (result.ok) {
        try { await nativeApi.destroySurface(this.item.id) } catch {}
      }
      return
    }
    if (!result.ok) {
      await this.failNativeSurface(
        surfaceError('Failed to create the native Workbench surface', result.message),
      )
      return
    }
    await this.syncSurfaceRect()
  }

  private async syncSurfaceRect() {
    if (!this.rect) return
    await this.setSurfaceRect(this.rect)
  }

  private async setSurfaceRect(rect: NativeSurfaceRect): Promise<boolean> {
    const nativeApi = this.context.nativeWorkbenchApi
    if (!nativeApi || !this.createdSurface) return true
    const request: NativeWorkbenchSurfaceRectRequest = {
      surfaceId: this.item.id,
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      visible: rect.visible,
    }
    try {
      const positioned = await nativeApi.setSurfaceRect(request)
      if (!positioned.ok) {
        throw surfaceError('Failed to position the native Workbench surface', positioned.message)
      }
      if (request.visible) {
        const activated = await nativeApi.activateSurface(this.item.id)
        // The scoped API rejects activation after a tab has already suspended.
        // That means the surface is safely hidden, not that the preview failed.
        const becameInactive = activated.message === 'Workbench surface is no longer active'
        if (!activated.ok && !becameInactive) {
          throw surfaceError('Failed to activate the native Workbench surface', activated.message)
        }
      }
      return true
    } catch (error) {
      if (this.context.isItemOpen()) await this.failNativeSurface(error)
      return false
    }
  }

  private async handlePreviewStateChange(state: ArtifactPreviewResourceState) {
    if (this.item.hostKind !== 'native-webcontents') return
    if (state === 'loading') {
      if (!await this.releaseNativeSurface(true)) {
        await this.failNativeSurface(
          surfaceError('Failed to reset the native Workbench surface'),
        )
        return
      }
      this.context.updateRenderState({
        missingResources: false,
        nativeSurfaceState: 'loading',
      })
      return
    }
    if (state === 'error' || state === 'offline' || state === 'unsupported') {
      await this.showNativeFailure('error')
    } else if (state === 'crashed') {
      await this.showNativeFailure('crashed')
    } else if (state === 'suspended' && this.rect) {
      await this.setSurfaceRect({ ...this.rect, visible: false })
    }
  }

  private async prepareForReload(): Promise<boolean> {
    if (!await this.releaseNativeSurface(true)) {
      await this.failNativeSurface(
        surfaceError('Failed to reset the native Workbench surface'),
      )
      return false
    }
    this.context.updateRenderState({
      missingResources: false,
      nativeSurfaceState: 'loading',
    })
    return true
  }

  private async releaseNativeSurface(clearResource: boolean): Promise<boolean> {
    this.generation += 1
    if (clearResource) this.resource = null
    const nativeApi = this.context.nativeWorkbenchApi
    if (!nativeApi || !this.createdSurface) {
      this.createdSurface = false
      return true
    }

    if (this.rect) {
      try {
        await nativeApi.setSurfaceRect({
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
      const result = await nativeApi.destroySurface(this.item.id)
      if (!result.ok) return false
      this.createdSurface = false
      return true
    } catch {
      return false
    }
  }

  private async showNativeFailure(state: 'crashed' | 'error') {
    await this.releaseNativeSurface(false)
    this.context.updateRenderState({ nativeSurfaceState: state })
  }

  private async failNativeSurface(error: unknown) {
    await this.releaseNativeSurface(false)
    if (!this.context.isItemOpen()) return
    this.context.updateRenderState({ nativeSurfaceState: 'error' })
    this.context.reportError(error)
    this.options.pushToast(
      this.options.t('workbench.artifactPreview.failedDetail'),
      { tone: 'danger' },
    )
  }
}

class ArtifactCollectionRuntime implements WorkbenchPanelRuntime {
  constructor(
    private readonly options: ArtifactWorkbenchProviderOptions,
  ) {}

  handleComponentEvent(event: WorkbenchComponentEvent, item: WorkbenchItem) {
    if (event.type !== 'artifact-open') return
    const artifact = artifactEventPayload(event)
    if (!artifact) return
    this.options.openArtifact(
      artifact,
      artifactSessionKey(item, this.options),
      artifactsFromWorkbenchItem(item),
    )
  }
}

function artifactHeader(
  item: WorkbenchItem,
): { title: string; subtitle?: string; icon?: ReturnType<typeof artifactIconName> } {
  const artifact = artifactFromWorkbenchItem(item)
  if (!artifact) return { title: item.title }
  return {
    icon: artifactIconName(artifact),
    subtitle: artifactFileSubtitle(artifact),
    title: artifactFileTitle(artifact),
  }
}

function artifactToolbarItems(
  item: WorkbenchItem,
  state: WorkbenchPanelRenderState,
  options: ArtifactWorkbenchProviderOptions,
): readonly WorkbenchToolbarItem[] {
  const artifact = artifactFromWorkbenchItem(item)
  if (!artifact) return []
  const items: WorkbenchToolbarItem[] = []
  if (runtimeStateValue(state, 'missingResources', false)) {
    items.push({
      kind: 'status',
      id: 'missing-resources',
      icon: 'info',
      label: options.t('workbench.artifactPreview.missingResources'),
      text: options.t('workbench.artifactPreview.missingShort'),
    })
  }
  const previewState = runtimeStateValue<ArtifactPreviewResourceState>(
    state,
    'previewState',
    'idle',
  )
  if ([
    'idle',
    'loading',
    'ready',
    'missing-resource',
    'error',
    'offline',
    'crashed',
  ].includes(previewState)) {
    items.push({
      kind: 'action',
      id: 'refresh',
      icon: 'refresh',
      label: options.t('workbench.refresh'),
      disabled: previewState === 'loading',
    })
  }
  if (item.hostKind === 'native-webcontents') {
    const enabled = runtimeStateValue(state, 'remoteResourcesEnabled', false)
    items.push({
      kind: 'action',
      id: 'toggle-remote-resources',
      icon: 'languages',
      label: options.t(enabled
        ? 'workbench.artifactPreview.blockRemoteResources'
        : 'workbench.artifactPreview.allowRemoteResources'),
      pressed: enabled,
    })
  }
  items.push(
    {
      kind: 'action',
      id: 'open-external',
      icon: 'externalLink',
      label: options.t('workbench.openExternal'),
    },
    {
      kind: 'action',
      id: 'download',
      icon: 'download',
      label: options.t('chat.downloadTitle', { title: item.title }),
    },
  )
  return items
}

export function createArtifactWorkbenchDefinitions(
  options: ArtifactWorkbenchProviderOptions,
): readonly WorkbenchPanelDefinition[] {
  return [
    {
      kind: 'artifact-collection',
      component: ArtifactCollectionPanel,
      supports: item => item.kind === 'artifact-collection',
      getHeader: item => ({
        title: options.t('chat.deliverablesCount', {
          count: artifactsFromWorkbenchItem(item).length,
        }),
      }),
      getProps: item => ({
        artifacts: artifactsFromWorkbenchItem(item),
        emptyLabel: options.t('chat.noDeliverables'),
        label: options.t('chat.sessionDeliverables'),
        openArtifactLabel: (artifact: ArtifactPayload) => options.t(
          'chat.openArtifact',
          {
            title: artifactFileTitle(artifact),
            subtitle: artifactFileSubtitle(artifact),
          },
        ),
      }),
      createRuntime: () => new ArtifactCollectionRuntime(options),
    },
    {
      kind: 'artifact-preview',
      component: ArtifactPreviewPanel,
      supports: item => artifactFromWorkbenchItem(item) !== null,
      getHeader: artifactHeader,
      getToolbarItems: (item, state) => artifactToolbarItems(item, state, options),
      getProps: (item, state) => ({
        artifact: artifactFromWorkbenchItem(item),
        authToken: options.authToken(),
        baseOrigin: options.baseOrigin,
        nativeHtml: state.nativeSurface,
        nativeSurfaceState: runtimeStateValue(
          state,
          'nativeSurfaceState',
          'loading',
        ),
        sessionKey: sessionKeyFromWorkbenchItem(item),
        showHeader: false,
        suspended: !state.hostAvailable || !state.active,
      }),
      createRuntime: (item, context) =>
        new ArtifactPreviewRuntime(item, context, options),
    },
  ]
}
