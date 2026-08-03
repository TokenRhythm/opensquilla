import type {
  Platform,
  WorkbenchPreviewMode,
} from '@/platform/types'
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
import {
  ArtifactPreviewLeaseError,
  createArtifactPreviewLease,
  renewArtifactPreviewLease,
  revokeArtifactPreviewLease,
  type ArtifactPreviewLease,
} from '@/utils/workbench/artifactPreviewLease'
import ArtifactCollectionPanel from './ArtifactCollectionPanel.vue'
import ArtifactPreviewPanel from './ArtifactPreviewPanel.vue'

type Translate = (key: string, params?: Record<string, unknown>) => string

interface ArtifactPreviewPanelHandle {
  reload: () => Promise<void>
}

export interface ArtifactWorkbenchProviderOptions {
  authToken(): string
  baseOrigin: string
  confirmPermission?(request: {
    permission: string
    requestingOrigin: string
  }): Promise<boolean>
  confirmRemoteResources(): Promise<boolean>
  currentSessionId(): string
  getPreviewPreferences?(): Promise<{
    mode: WorkbenchPreviewMode
    noticeShown: boolean
  }>
  savePreviewPreferences?(preferences: {
    mode: WorkbenchPreviewMode
    noticeShown: boolean
  }): Promise<void>
  showFullPreviewNotice?(): void
  openArtifact(
    artifact: ArtifactPayload,
    sessionKey: string,
    navigationArtifacts: readonly ArtifactPayload[],
  ): void
  platform: Platform
  previewLeasesEnabled?: boolean
  pushToast(message: string, options?: {
    tone?: 'info' | 'ok' | 'warn' | 'danger'
    duration?: number
  }): void
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
    'ready-with-warnings',
    'suspended',
    'unsupported',
  ].includes(state)
    ? state as ArtifactPreviewResourceState
    : null
}

function surfaceError(operation: string, message?: string): Error {
  return new Error(message ? `${operation}: ${message}` : operation)
}

function isLoopbackPreviewOrigin(value: string): boolean {
  try {
    const hostname = new URL(value).hostname
      .replace(/^\[|\]$/g, '')
      .replace(/\.$/, '')
      .toLowerCase()
    return hostname === 'localhost'
      || hostname.endsWith('.localhost')
      || hostname === '::1'
      || /^127(?:\.\d{1,3}){3}$/.test(hostname)
  } catch {
    return false
  }
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
  private lease: ArtifactPreviewLease | null = null
  private leaseRenewTimer: ReturnType<typeof setInterval> | null = null
  private defaultMode: WorkbenchPreviewMode
  private mode: WorkbenchPreviewMode
  private nativeProtocolVersion: 1 | 2 = 1
  private noticeShown: boolean
  private rect: NativeSurfaceRect | null = null
  private resource: NativeHtmlArtifactResource | null = null

  constructor(
    item: WorkbenchItem,
    private readonly context: WorkbenchRuntimeContext,
    private readonly options: ArtifactWorkbenchProviderOptions,
    preferences: { mode: WorkbenchPreviewMode; noticeShown: boolean },
  ) {
    this.item = item
    this.defaultMode = preferences.mode
    this.mode = preferences.mode
    this.noticeShown = preferences.noticeShown
    const artifact = artifactFromWorkbenchItem(item)
    const leasePending = Boolean(
      this.options.previewLeasesEnabled
      && artifact
      && isActiveDocumentArtifactCandidate(artifact),
    )
    this.context.updateRenderState({
      effectiveMode: this.mode,
      missingResources: false,
      nativeSurfaceState: 'loading',
      previewBlocked: leasePending,
      previewCollectionStatus: 'not_applicable',
      previewDefaultMode: this.defaultMode,
      previewLeaseError: '',
      previewLaunchUrl: '',
      previewMode: this.mode,
      previewReadiness: 'loading',
      previewState: leasePending ? 'loading' : 'idle',
      remoteResourcesEnabled: false,
    })
  }

  async initialize() {
    const artifact = artifactFromWorkbenchItem(this.item)
    if (
      !this.options.previewLeasesEnabled
      || !artifact
      || !isActiveDocumentArtifactCandidate(artifact)
    ) return
    try {
      await this.prepareLeasePreview()
    } catch (error) {
      await this.handleLeaseFailure(error)
    }
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
        this.context.updateRenderState({
          previewReadiness: state === 'ready-with-warnings'
            || state === 'missing-resource'
            ? 'ready-with-warnings'
            : state,
          previewState: state,
        })
        await this.handlePreviewStateChange(state)
      }
      return
    }
    if (event.type === 'native-html-ready') {
      const resource = htmlResourcePayload(event)
      if (resource && this.nativeProtocolVersion === 1) {
        await this.createNativeSurface(resource)
      }
      return
    }
    if (event.type === 'preview-retry') {
      await this.retryLeasePreview()
    }
  }

  async performAction(actionId: string, item: WorkbenchItem) {
    this.item = item
    const artifact = artifactFromWorkbenchItem(item)
    if (actionId === 'refresh') {
      if (this.context.getRenderState().previewBlocked === true) {
        await this.retryLeasePreview()
        return
      }
      if (
        this.nativeProtocolVersion === 2
        && this.createdSurface
        && this.context.nativeWorkbenchApi?.navigateSurface
      ) {
        await this.context.nativeWorkbenchApi.navigateSurface({
          version: 2,
          surfaceId: this.item.id,
          action: 'reload',
        })
      } else if (this.lease) {
        await this.component?.reload()
      } else {
        if (!await this.prepareForReload()) return
        await this.component?.reload()
      }
    } else if (
      actionId === 'toggle-preview-mode'
      || actionId === 'set-preview-mode-full'
      || actionId === 'set-preview-mode-offline'
    ) {
      const nextMode: WorkbenchPreviewMode = actionId === 'set-preview-mode-full'
        ? 'full'
        : actionId === 'set-preview-mode-offline'
          ? 'offline'
          : this.mode === 'full' ? 'offline' : 'full'
      if (nextMode === this.mode) return
      if (
        nextMode === 'full'
        && this.context.getRenderState().fullModeAvailable === false
      ) return
      this.mode = nextMode
      await this.replaceLeasePreview()
    } else if (actionId === 'set-default-preview-mode') {
      if (this.mode === this.defaultMode) return
      await this.options.savePreviewPreferences?.({
        mode: this.mode,
        noticeShown: this.noticeShown,
      })
      this.defaultMode = this.mode
      this.context.updateRenderState({ previewDefaultMode: this.defaultMode })
      this.options.pushToast(
        this.options.t('workbench.artifactPreview.defaultModeSaved'),
        { tone: 'ok' },
      )
    } else if (actionId === 'restore-default-preview-mode') {
      if (this.mode === this.defaultMode) return
      this.mode = this.defaultMode
      await this.replaceLeasePreview()
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
      this.context.updateRenderState({
        missingResources: true,
        previewReadiness: 'ready-with-warnings',
      })
    } else if (event.type === 'loading') {
      this.context.updateRenderState({
        nativeSurfaceState: 'loading',
        previewReadiness: 'loading',
      })
    } else if (event.type === 'ready') {
      const current = this.context.getRenderState()
      this.context.updateRenderState({
        nativeSurfaceState: 'ready',
        previewReadiness: current.missingResources === true
          || current.networkBlocked === true
          ? 'ready-with-warnings'
          : 'ready',
      })
    } else if (event.type === 'navigation-state') {
      this.context.updateRenderState({
        canGoBack: event.detail?.canGoBack === true,
        canGoForward: event.detail?.canGoForward === true,
        currentUrl: event.detail?.url || '',
        loading: event.detail?.loading === true,
        pageTitle: event.detail?.title || '',
      })
    } else if (event.type === 'permission-request') {
      const requestId = event.detail?.requestId || ''
      if (!requestId || !this.context.nativeWorkbenchApi?.respondToPermission) return
      const allow = this.options.confirmPermission
        ? await this.options.confirmPermission({
            permission: event.detail?.permission || 'unknown',
            requestingOrigin: event.detail?.requestingOrigin || '',
          })
        : false
      await this.context.nativeWorkbenchApi.respondToPermission({
        version: 2,
        surfaceId: this.item.id,
        requestId,
        allow,
      })
    } else if (event.type === 'blocked-action') {
      this.context.updateRenderState({
        blockedAction: event.detail?.action || event.detail?.reason || 'blocked',
        ...(event.detail?.action === 'network'
          ? {
              networkBlocked: true,
              previewReadiness: 'ready-with-warnings',
            }
          : {}),
      })
    } else if (event.type === 'capability-expired') {
      await this.replaceLeasePreview()
    } else if (event.type === 'unresponsive') {
      this.context.updateRenderState({ nativeSurfaceState: 'error' })
    } else if (event.type === 'responsive') {
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
    await this.releaseLease()
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

  private async prepareLeasePreview(): Promise<boolean> {
    const artifact = artifactFromWorkbenchItem(this.item)
    if (!artifact) return false
    const nativeApi = this.context.nativeWorkbenchApi
    const capabilities = nativeApi?.getCapabilities
      ? await nativeApi.getCapabilities()
      : { protocolVersions: [1] as Array<1 | 2>, modes: ['offline'] as WorkbenchPreviewMode[] }
    const fullModeAvailable = this.options.platform.id === 'desktop'
      ? capabilities.modes.includes('full')
      : isLoopbackPreviewOrigin(this.options.baseOrigin)
    this.context.updateRenderState({ fullModeAvailable })
    const hasNativeLeaseBroker = Boolean(
      nativeApi?.createArtifactPreviewLease
      && nativeApi.renewArtifactPreviewLease
      && nativeApi.revokeArtifactPreviewLease,
    )
    if (
      this.item.hostKind === 'native-webcontents'
      && (!capabilities.protocolVersions.includes(2) || !hasNativeLeaseBroker)
    ) {
      this.nativeProtocolVersion = 1
      this.context.updateRenderState({
        compatibilityFallback: true,
        previewBlocked: false,
        previewLeaseError: '',
        previewMode: 'offline',
      })
      return false
    }

    let lease: ArtifactPreviewLease
    try {
      lease = await createArtifactPreviewLease(
        artifact,
        this.mode,
        this.options.platform.id,
        {
          authToken: this.options.authToken(),
          baseOrigin: this.options.baseOrigin,
          nativeBroker: nativeApi,
          sessionKey: artifactSessionKey(this.item, this.options),
        },
      )
    } catch (error) {
      if (
        error instanceof ArtifactPreviewLeaseError
        && (
          (error.status === 404 && !error.code)
          || error.status === 405
          || error.status === 501
        )
      ) {
        this.context.updateRenderState({
          compatibilityFallback: true,
          previewBlocked: false,
          previewLeaseError: '',
          previewMode: 'offline',
        })
        return false
      }
      throw error
    }

    this.lease = lease
    this.mode = lease.effective_mode
    this.context.updateRenderState({
      compatibilityFallback: false,
      effectiveMode: lease.effective_mode,
      missingResources: lease.source.collection_status === 'partial',
      previewBlocked: false,
      previewCollectionStatus: lease.source.collection_status,
      previewLeaseError: '',
      previewLaunchUrl: lease.launch_url,
      previewMode: lease.effective_mode,
      previewReadiness: lease.source.collection_status === 'partial'
        || lease.source.warning_codes.length > 0
        ? 'ready-with-warnings'
        : 'loading',
      previewSourceKind: lease.source.kind,
      previewWarnings: lease.source.warning_codes,
    })
    this.startLeaseRenewal()

    if (lease.effective_mode === 'full' && !this.noticeShown) {
      this.noticeShown = true
      this.options.showFullPreviewNotice?.()
      try {
        await this.options.savePreviewPreferences?.({
          mode: this.defaultMode,
          noticeShown: true,
        })
      } catch {}
    }

    if (this.item.hostKind !== 'native-webcontents' || !nativeApi) return true
    this.nativeProtocolVersion = 2
    await this.createNativeLeaseSurface(lease)
    return true
  }

  private async createNativeLeaseSurface(lease: ArtifactPreviewLease) {
    const nativeApi = this.context.nativeWorkbenchApi
    if (!nativeApi) return
    if (this.createdSurface && !await this.releaseNativeSurface(false)) {
      throw surfaceError('Failed to replace the native Workbench surface')
    }
    const generation = ++this.generation
    this.createdSurface = true
    this.context.updateRenderState({ nativeSurfaceState: 'loading' })
    let expectedOrigin = ''
    try {
      expectedOrigin = new URL(lease.launch_url).origin
    } catch {
      throw surfaceError('Preview lease returned an invalid origin')
    }
    const result = await nativeApi.createSurface({
      version: 2,
      surfaceId: this.item.id,
      kind: 'artifact-preview',
      payload: {
        launchUrl: lease.launch_url,
        expectedOrigin,
        scopeId: artifactSessionKey(this.item, this.options),
        mode: lease.effective_mode,
      },
    })
    if (generation !== this.generation) return
    if (!result.ok) {
      this.createdSurface = false
      throw surfaceError('Failed to create the native Workbench surface', result.message)
    }
    await this.syncSurfaceRect()
  }

  private async replaceLeasePreview() {
    if (!await this.releaseNativeSurface(true)) {
      throw surfaceError('Failed to replace the native Workbench surface')
    }
    await this.releaseLease()
    this.context.updateRenderState({
      effectiveMode: this.mode,
      missingResources: false,
      nativeSurfaceState: 'loading',
      previewBlocked: true,
      previewCollectionStatus: 'not_applicable',
      previewLeaseError: '',
      previewLaunchUrl: '',
      previewMode: this.mode,
      previewReadiness: 'loading',
      previewState: 'loading',
      networkBlocked: false,
    })
    try {
      const created = await this.prepareLeasePreview()
      if (!created) await this.component?.reload()
    } catch (error) {
      await this.handleLeaseFailure(error)
    }
  }

  private startLeaseRenewal() {
    if (this.leaseRenewTimer) clearInterval(this.leaseRenewTimer)
    this.leaseRenewTimer = setInterval(() => {
      void this.renewLease()
    }, 15 * 60 * 1000)
  }

  private async renewLease() {
    const lease = this.lease
    if (!lease || !this.context.isItemOpen()) return
    try {
      const renewal = await renewArtifactPreviewLease(lease.lease_id, {
        authToken: this.options.authToken(),
        baseOrigin: this.options.baseOrigin,
        nativeBroker: this.context.nativeWorkbenchApi,
        sessionKey: artifactSessionKey(this.item, this.options),
      })
      if (renewal.lease_id !== lease.lease_id) {
        throw new ArtifactPreviewLeaseError('Preview lease identity changed.', 502)
      }
      this.lease = {
        ...lease,
        expires_at: renewal.expires_at,
      }
    } catch (error) {
      if (
        error instanceof ArtifactPreviewLeaseError
        && (error.status === 404 || error.status === 410)
      ) {
        await this.replaceLeasePreview()
      } else {
        await this.handleLeaseFailure(error)
      }
    }
  }

  private async releaseLease() {
    if (this.leaseRenewTimer) {
      clearInterval(this.leaseRenewTimer)
      this.leaseRenewTimer = null
    }
    const lease = this.lease
    this.lease = null
    if (!lease) return
    if (this.options.platform.id !== 'desktop' && lease.preview_origin) {
      try {
        const clearUrl = new URL('/.opensquilla/clear-site-data', lease.preview_origin)
        await fetch(clearUrl, {
          method: 'GET',
          cache: 'no-store',
          credentials: 'omit',
          keepalive: true,
          mode: 'no-cors',
          redirect: 'error',
          referrerPolicy: 'no-referrer',
          signal: AbortSignal.timeout(2_000),
        })
      } catch {}
    }
    try {
      await revokeArtifactPreviewLease(lease.lease_id, {
        authToken: this.options.authToken(),
        baseOrigin: this.options.baseOrigin,
        nativeBroker: this.context.nativeWorkbenchApi,
        sessionKey: artifactSessionKey(this.item, this.options),
      })
    } catch {}
  }

  private async retryLeasePreview() {
    if (!this.options.previewLeasesEnabled) {
      await this.component?.reload()
      return
    }
    try {
      this.context.updateRenderState({
        previewBlocked: true,
        previewLeaseError: '',
        previewReadiness: 'loading',
        previewState: 'loading',
      })
      if (!await this.releaseNativeSurface(true)) {
        throw surfaceError('Failed to reset the native Workbench surface')
      }
      await this.releaseLease()
      const created = await this.prepareLeasePreview()
      if (!created) await this.component?.reload()
    } catch (error) {
      await this.handleLeaseFailure(error)
    }
  }

  private async handleLeaseFailure(error: unknown) {
    if (this.leaseRenewTimer) {
      clearInterval(this.leaseRenewTimer)
      this.leaseRenewTimer = null
    }
    await this.releaseNativeSurface(false)
    const message = error instanceof Error
      ? error.message
      : this.options.t('workbench.artifactPreview.failedDetail')
    this.context.updateRenderState({
      nativeSurfaceState: 'error',
      previewBlocked: true,
      previewLeaseError: message,
      previewReadiness: 'error',
      previewState: 'error',
    })
    this.context.reportError(error)
    this.options.pushToast(message, { tone: 'danger' })
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
    if (this.nativeProtocolVersion === 2) return
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
    'ready-with-warnings',
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
  if (
    runtimeStateValue<string>(state, 'previewReadiness', '') === 'ready-with-warnings'
    && !runtimeStateValue(state, 'missingResources', false)
  ) {
    items.push({
      kind: 'status',
      id: 'preview-warnings',
      icon: 'info',
      label: options.t('workbench.artifactPreview.readyWithWarnings'),
      text: options.t('workbench.artifactPreview.warningsShort'),
    })
  }
  const hasLease = Boolean(runtimeStateValue(state, 'previewLaunchUrl', ''))
  if (hasLease) {
    const mode = runtimeStateValue<WorkbenchPreviewMode>(state, 'previewMode', 'offline')
    const defaultMode = runtimeStateValue<WorkbenchPreviewMode>(
      state,
      'previewDefaultMode',
      'full',
    )
    const fullModeLabel = options.t('workbench.artifactPreview.fullMode')
    const offlineModeLabel = options.t('workbench.artifactPreview.offlineMode')
    const currentModeLabel = mode === 'full' ? fullModeLabel : offlineModeLabel
    items.push({
      kind: 'select',
      id: 'preview-mode',
      label: options.t('workbench.artifactPreview.modeControl', {
        mode: currentModeLabel,
      }),
      value: mode,
      options: [
        {
          value: 'full',
          label: defaultMode === 'full'
            ? options.t('workbench.artifactPreview.modeDefaultOption', {
                mode: fullModeLabel,
              })
            : fullModeLabel,
          actionId: 'set-preview-mode-full',
          disabled: runtimeStateValue<boolean>(
            state,
            'fullModeAvailable',
            true,
          ) === false,
        },
        {
          value: 'offline',
          label: defaultMode === 'offline'
            ? options.t('workbench.artifactPreview.modeDefaultOption', {
                mode: offlineModeLabel,
              })
            : offlineModeLabel,
          actionId: 'set-preview-mode-offline',
        },
      ],
      ...(mode !== defaultMode
        ? {
            actionGroupLabel: options.t('workbench.artifactPreview.modeDefaults'),
            actionOptions: [{
              value: 'set-current-as-default',
              label: options.t('workbench.artifactPreview.setDefaultMode'),
              actionId: 'set-default-preview-mode',
            }],
          }
        : {}),
    })
  } else if (
    item.hostKind === 'native-webcontents'
    && !runtimeStateValue(state, 'previewBlocked', false)
  ) {
    if (runtimeStateValue(state, 'compatibilityFallback', false)) {
      items.push({
        kind: 'status',
        id: 'compatibility-fallback',
        icon: 'info',
        label: options.t('workbench.artifactPreview.compatibilityFallback'),
        text: options.t('workbench.artifactPreview.upgradeDesktopShort'),
      })
    } else {
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
        previewCollectionStatus: runtimeStateValue(
          state,
          'previewCollectionStatus',
          'not_applicable',
        ),
        previewBlocked: runtimeStateValue(state, 'previewBlocked', false),
        previewErrorMessage: runtimeStateValue(state, 'previewLeaseError', ''),
        previewLaunchUrl: runtimeStateValue(state, 'previewLaunchUrl', ''),
        previewMode: runtimeStateValue(state, 'previewMode', 'offline'),
        sessionKey: sessionKeyFromWorkbenchItem(item),
        showHeader: false,
        suspended: !state.hostAvailable || !state.active,
      }),
      async createRuntime(item, context) {
        const runtime = new ArtifactPreviewRuntime(
          item,
          context,
          options,
          options.getPreviewPreferences
            ? await options.getPreviewPreferences()
            : { mode: 'full', noticeShown: false },
        )
        await runtime.initialize()
        return runtime
      },
    },
  ]
}
