import type {
  Platform,
  WorkbenchPreviewMode,
} from '@/platform/types'
import type { ArtifactPayload } from '@/types/rpc'
import type { ArtifactDocumentWorkspaceSnapshot } from '@/types/artifactDocuments'
import type { ArtifactDocumentActions } from '@/types/artifactDocuments'
import type {
  PromptAnnotation,
  PromptAnnotationCreateRequest,
} from '@/types/promptAnnotations'
import { promptAnnotationBodyWithinLimit } from '@/types/promptAnnotations'
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
import { isMacPlatform } from '@/utils/browser'
import { promptAnnotationTargetLabel } from '@/utils/chat/promptAnnotationPresentation'
import { classifyArtifactProductError } from '@/utils/artifactProductErrors'
import {
  artifactFromWorkbenchItem,
  artifactsFromWorkbenchItem,
  initialSectionFromWorkbenchItem,
  initialSectionRequestIdFromWorkbenchItem,
  preparedPreviewFromWorkbenchItem,
  sessionKeyFromWorkbenchItem,
} from '@/workbench/artifactItems'
import type {
  NativeSurfaceRect,
  WorkbenchBeforeCloseOptions,
  WorkbenchComponentEvent,
  WorkbenchItem,
  WorkbenchPanelDefinition,
  WorkbenchPanelRenderState,
  WorkbenchPanelRuntime,
  WorkbenchRuntimeContext,
  WorkbenchToolbarItem,
} from '@/workbench/types'
import type {
  NativeArtifactAnnotationSelection,
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
import ArtifactDocumentPanel from './ArtifactDocumentPanel.vue'

type Translate = (key: string, params?: Record<string, unknown>) => string

interface ArtifactPreviewPanelHandle {
  beforeClose?: (options?: WorkbenchBeforeCloseOptions) => Promise<boolean>
  reload: () => Promise<void>
}

export interface ArtifactWorkbenchProviderOptions {
  artifactDocuments?: {
    load(
      artifact: ArtifactPayload,
      sessionKey: string,
      options?: { force?: boolean },
    ): Promise<unknown>
    snapshot(
      artifact: ArtifactPayload,
      sessionKey: string,
    ): ArtifactDocumentWorkspaceSnapshot
    headArtifact(artifact: ArtifactPayload, sessionKey: string): ArtifactPayload
    restoreRevision?: ArtifactDocumentActions['restoreRevision']
    revertChangeSet?: ArtifactDocumentActions['revertChangeSet']
  }
  promptAnnotations?: {
    create(request: PromptAnnotationCreateRequest): Promise<PromptAnnotation>
    update(annotationId: string, body: string): Promise<PromptAnnotation | null>
    discard(annotationId: string): Promise<boolean>
    beginOverlayEdit?(annotationId: string, sessionKey: string): void
    completeOverlayEdit?(annotationId: string): void
    releaseOverlayEdit?(annotationId: string): void
    setActiveDocument(sessionKey: string, documentId: string): void
  }
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
  publishDocument?(request: {
    sessionKey: string
    documentId: string
    revisionId: string
    name: string
  }): Promise<void>
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

function headRevisionIdPayload(event: WorkbenchComponentEvent): string {
  if (!event.payload || typeof event.payload !== 'object') return ''
  const revisionId = (event.payload as { revisionId?: unknown }).revisionId
  return typeof revisionId === 'string' ? revisionId : ''
}

function surfaceError(operation: string, message?: string): Error {
  const error = new Error(message ? `${operation}: ${message}` : operation) as Error & {
    code?: string
  }
  error.code = 'PREVIEW_RENDERER_FAILED'
  return error
}

function productErrorMessage(
  error: unknown,
  options: ArtifactWorkbenchProviderOptions,
): string {
  const classified = classifyArtifactProductError(error)
  const translated = options.t(classified.messageKey)
  return translated === classified.messageKey ? classified.fallbackMessage : translated
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

function previewLeaseEnabledForItem(
  item: WorkbenchItem,
  options: ArtifactWorkbenchProviderOptions,
): boolean {
  return options.previewLeasesEnabled === true
    && item.payload.previewLeaseEligible !== false
}

function isPreparedImmutableResourcePreview(item: WorkbenchItem): boolean {
  const prepared = preparedPreviewFromWorkbenchItem(item)
  return prepared !== null && prepared.resource.type !== 'document'
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
      if (opened.message) console.warn('[artifact] Native open failed:', opened.message)
      options.pushToast(options.t('chat.toast.artifactOpenFailed'), { tone: 'danger' })
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

function runtimeContextStateValue<T>(
  state: Readonly<Record<string, unknown>>,
  key: string,
  fallback: T,
): T {
  const value = state[key]
  return value === undefined ? fallback : value as T
}

function promptAnnotationRpcErrorCode(error: unknown): string {
  if (!error || typeof error !== 'object') return ''
  const code = (error as { code?: unknown }).code
  return typeof code === 'string' ? code : ''
}

class ArtifactPreviewRuntime implements WorkbenchPanelRuntime {
  private annotationMode = false
  private annotationModeRestorePending = false
  private annotationOverlayCopyVersion: 0 | 1 = 0
  private annotationPickerArmed = false
  private annotationModeOperation = 0
  private annotationSelectionAttempt = 0
  private annotationSelectionPending = false
  private annotationOverlayAttempt = 0
  private annotationOverlayId = ''
  private annotationOverlayBody = ''
  private annotationOverlayOperation: symbol | null = null
  private annotationOverlayDiscarded: {
    attempt: number
    annotationId: string
  } | null = null
  private annotationScreenshotUrl = ''
  private annotationReplacement: {
    annotationId: string
    body: string
    discardOriginal: boolean
  } | null = null
  private annotationUpdateTimer: ReturnType<typeof setTimeout> | null = null
  private component: ArtifactPreviewPanelHandle | null = null
  private blockedHeadRevisionId = ''
  private createdSurface = false
  private generation = 0
  private item: WorkbenchItem
  private lease: ArtifactPreviewLease | null = null
  private leaseArtifactId = ''
  private leaseRenewTimer: ReturnType<typeof setInterval> | null = null
  private readonly nativeRecoveryAttemptedKeys = new Set<string>()
  private nativeRecoveryInFlight: Promise<void> | null = null
  private defaultMode: WorkbenchPreviewMode
  private mode: WorkbenchPreviewMode
  private nativeProtocolVersion: 1 | 2 | 3 = 1
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
    const preparedPreview = preparedPreviewFromWorkbenchItem(item)
    this.defaultMode = preparedPreview ? 'offline' : preferences.mode
    this.mode = preparedPreview ? 'offline' : preferences.mode
    this.noticeShown = preferences.noticeShown
    const artifact = artifactFromWorkbenchItem(item)
    const leasePending = Boolean(
      previewLeaseEnabledForItem(this.item, this.options)
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
      previewLaunchUrl: preparedPreview?.launchUrl || '',
      previewMode: this.mode,
      ...(preparedPreview
        ? {
            fullModeAvailable: false,
            previewNetworkAllowed: false,
            previewSandboxProfile: preparedPreview.sandboxProfile,
          }
        : {}),
      previewReadiness: 'loading',
      previewState: leasePending ? 'loading' : 'idle',
      remoteResourcesEnabled: false,
      annotationAvailable: false,
      annotationMode: false,
      annotationModeStopping: false,
    })
  }

  async initialize() {
    const artifact = artifactFromWorkbenchItem(this.item)
    const documentLoad = artifact && !isPreparedImmutableResourcePreview(this.item)
      ? this.options.artifactDocuments?.load(
          artifact,
          artifactSessionKey(this.item, this.options),
        ).catch(() => undefined)
      : undefined
    await documentLoad
    if (
      !previewLeaseEnabledForItem(this.item, this.options)
      || !artifact
      || !isActiveDocumentArtifactCandidate(artifact)
    ) {
      return
    }
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
    const preparedPreview = preparedPreviewFromWorkbenchItem(item)
    if (!preparedPreview) return
    this.defaultMode = 'offline'
    this.mode = 'offline'
    this.context.updateRenderState({
      effectiveMode: 'offline',
      fullModeAvailable: false,
      previewDefaultMode: 'offline',
      previewLaunchUrl: preparedPreview.launchUrl || '',
      previewMode: 'offline',
      previewNetworkAllowed: false,
      previewSandboxProfile: preparedPreview.sandboxProfile,
      remoteResourcesEnabled: false,
    })
  }

  async handleComponentEvent(event: WorkbenchComponentEvent, item: WorkbenchItem) {
    this.item = item
    if (event.type === 'artifact-document-publish') {
      if (isPreparedImmutableResourcePreview(item)) return
      if (this.context.getRenderState().documentPublishing === true) return
      const artifact = artifactFromWorkbenchItem(item)
      const sessionKey = artifactSessionKey(item, this.options)
      const payload = event.payload && typeof event.payload === 'object'
        ? event.payload as Record<string, unknown>
        : {}
      const workspace = artifact
        ? this.options.artifactDocuments?.snapshot(artifact, sessionKey).workspace
        : null
      const document = workspace?.document
      if (
        !artifact
        || !sessionKey
        || !document
        || workspace?.source !== 'document-api'
        || payload.documentId !== document.documentId
        || payload.revisionId !== document.headRevisionId
        || !this.options.publishDocument
      ) return
      this.context.updateRenderState({ documentPublishing: true })
      try {
        await this.options.publishDocument({
          sessionKey,
          documentId: document.documentId,
          revisionId: document.headRevisionId,
          name: document.name || String(artifact.name || ''),
        })
      } catch (error) {
        this.options.pushToast(
          productErrorMessage(error, this.options),
          { tone: 'danger', duration: 9000 },
        )
      } finally {
        this.context.updateRenderState({ documentPublishing: false })
      }
      return
    }
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
      return
    }
    if (event.type === 'artifact-annotation-fallback-update') {
      const payload = event.payload && typeof event.payload === 'object'
        ? event.payload as Record<string, unknown>
        : {}
      this.handleAnnotationDraftChange(
        typeof payload.annotationId === 'string' ? payload.annotationId : '',
        typeof payload.body === 'string' ? payload.body : '',
      )
      return
    }
    if (event.type === 'artifact-annotation-fallback-submit') {
      const payload = event.payload && typeof event.payload === 'object'
        ? event.payload as Record<string, unknown>
        : {}
      await this.handleAnnotationSubmit(
        typeof payload.annotationId === 'string' ? payload.annotationId : '',
        typeof payload.body === 'string' ? payload.body : '',
      )
      return
    }
    if (event.type === 'artifact-annotation-fallback-cancel') {
      const payload = event.payload && typeof event.payload === 'object'
        ? event.payload as Record<string, unknown>
        : {}
      await this.handleAnnotationCancel(
        typeof payload.annotationId === 'string' ? payload.annotationId : '',
      )
      return
    }
    if (event.type === 'artifact-prompt-annotation-reselect') {
      const payload = event.payload && typeof event.payload === 'object'
        ? event.payload as Record<string, unknown>
        : {}
      const annotationId = typeof payload.annotationId === 'string' ? payload.annotationId : ''
      const body = typeof payload.body === 'string' ? payload.body : ''
      if (
        !annotationId
        || !body.trim()
        || this.context.getRenderState().annotationAvailable !== true
      ) return
      this.annotationReplacement = { annotationId, body, discardOriginal: true }
      await this.setAnnotationMode(true)
      this.options.pushToast(
        this.options.t('workbench.artifactAnnotation.reselectHint'),
        { tone: 'info' },
      )
      return
    }
    if (event.type === 'artifact-prompt-annotation-reuse') {
      const payload = event.payload && typeof event.payload === 'object'
        ? event.payload as Record<string, unknown>
        : {}
      const body = typeof payload.body === 'string' ? payload.body : ''
      if (
        !body.trim()
        || !promptAnnotationBodyWithinLimit(body)
        || this.context.getRenderState().annotationAvailable !== true
      ) return
      this.annotationReplacement = {
        annotationId: '',
        body,
        discardOriginal: false,
      }
      await this.setAnnotationMode(true)
      this.options.pushToast(
        this.options.t('workbench.artifactAnnotation.reuseHint'),
        { tone: 'info' },
      )
      return
    }
    if (event.type === 'artifact-prompt-annotations-accepted') {
      this.annotationModeRestorePending = false
      const visibleMode = runtimeContextStateValue(
        this.context.getRenderState(),
        'annotationMode',
        this.annotationMode,
      )
      if (this.annotationMode || visibleMode) await this.setAnnotationMode(false)
      else this.invalidateAnnotationSelectionAttempt()
      return
    }
    if (event.type === 'artifact-head-changed') {
      if (isPreparedImmutableResourcePreview(this.item)) return
      const renderedMode = runtimeContextStateValue(
        this.context.getRenderState(),
        'annotationMode',
        this.annotationMode,
      )
      const stopping = runtimeContextStateValue(
        this.context.getRenderState(),
        'annotationModeStopping',
        false,
      )
      const preserveAnnotationMode = this.annotationMode || (renderedMode && !stopping)
      this.annotationModeRestorePending = preserveAnnotationMode
      this.invalidateAnnotationSelectionAttempt()
      const expectedRevisionId = headRevisionIdPayload(event)
      if (expectedRevisionId && !await this.ensureCanonicalDocumentHead(expectedRevisionId)) {
        this.annotationModeRestorePending = false
        const message = this.options.t('workbench.artifactDocument.sourceUnavailable')
        this.blockedHeadRevisionId = expectedRevisionId
        await this.releaseNativeSurface(true)
        await this.releaseLease()
        this.context.updateRenderState({
          nativeSurfaceState: 'error',
          previewBlocked: true,
          previewLeaseError: message,
          previewReadiness: 'error',
          previewState: 'error',
        })
        this.options.pushToast(
          message,
          { tone: 'danger' },
        )
        return
      }
      this.blockedHeadRevisionId = ''
      if (previewLeaseEnabledForItem(this.item, this.options)) {
        const artifact = artifactFromWorkbenchItem(this.item)
        const headArtifact = artifact && this.options.artifactDocuments?.headArtifact(
          artifact,
          artifactSessionKey(this.item, this.options),
        )
        const headArtifactId = String(headArtifact?.id || '')
        if (
          expectedRevisionId
          && this.lease
          && headArtifactId
          && headArtifactId === this.leaseArtifactId
        ) {
          this.annotationModeRestorePending = false
          return
        }
        await this.replaceLeasePreview()
        await this.restoreAnnotationModeAfterSurfaceRefresh()
      } else {
        await this.component?.reload()
        await this.restoreAnnotationModeAfterSurfaceRefresh()
      }
    }
  }

  async performAction(actionId: string, item: WorkbenchItem) {
    this.item = item
    const artifact = artifactFromWorkbenchItem(item)
    const preparedPreview = preparedPreviewFromWorkbenchItem(item)
    if (
      preparedPreview
      && (
        actionId === 'toggle-preview-mode'
        || actionId === 'set-preview-mode-full'
        || actionId === 'set-preview-mode-offline'
        || actionId === 'set-default-preview-mode'
        || actionId === 'restore-default-preview-mode'
        || actionId === 'toggle-remote-resources'
      )
    ) return
    if (actionId === 'toggle-annotation-mode') {
      if (runtimeContextStateValue(
        this.context.getRenderState(),
        'annotationModeStopping',
        false,
      )) return
      const visibleMode = runtimeContextStateValue(
        this.context.getRenderState(),
        'annotationMode',
        this.annotationMode,
      )
      await this.setAnnotationMode(!visibleMode)
    } else if (actionId === 'refresh') {
      if (this.annotationMode) await this.setAnnotationMode(false)
      else this.invalidateAnnotationSelectionAttempt()
      let refreshedDocument = false
      if (artifact && !isPreparedImmutableResourcePreview(this.item)) {
        await this.options.artifactDocuments?.load(
          artifact,
          artifactSessionKey(item, this.options),
          { force: true },
        ).catch(() => undefined)
        const snapshot = this.options.artifactDocuments?.snapshot(
          artifact,
          artifactSessionKey(item, this.options),
        )
        refreshedDocument = Boolean(
          snapshot?.loaded
          && !snapshot.loading
          && !snapshot.stale
          && snapshot.workspace?.source === 'document-api',
        )
      }
      if (this.context.getRenderState().previewBlocked === true) {
        await this.retryLeasePreview()
        return
      }
      // A document refresh must resolve a new lease against the canonical
      // current head. Reloading the existing native surface would only reload
      // the immutable URL for the previous Revision.
      if (refreshedDocument && previewLeaseEnabledForItem(this.item, this.options)) {
        await this.replaceLeasePreview()
        return
      }
      if (
        this.nativeProtocolVersion !== 1
        && this.createdSurface
        && this.context.nativeWorkbenchApi?.navigateSurface
      ) {
        await this.context.nativeWorkbenchApi.navigateSurface({
          version: this.nativeProtocolVersion,
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
      const immutableResource = isPreparedImmutableResourcePreview(this.item)
      await downloadArtifact(
        item,
        immutableResource
          ? artifact
          : this.options.artifactDocuments?.headArtifact(
              artifact,
              artifactSessionKey(item, this.options),
            ) || artifact,
        this.options,
      )
    }
  }

  private invalidateAnnotationSelectionAttempt() {
    this.annotationSelectionAttempt += 1
    this.annotationSelectionPending = false
  }

  private updateAnnotationModeIntent(enabled: boolean, clearReplacement = !enabled) {
    if (!enabled) this.invalidateAnnotationSelectionAttempt()
    this.annotationMode = enabled
    this.annotationPickerArmed = enabled
    if (clearReplacement) this.annotationReplacement = null
  }

  private updateAnnotationModeState(enabled: boolean, clearReplacement = !enabled) {
    this.updateAnnotationModeIntent(enabled, clearReplacement)
    this.context.updateRenderState({
      annotationMode: enabled,
      annotationModeStopping: false,
    })
  }

  private async setAnnotationMode(enabled: boolean): Promise<boolean> {
    const nativeApi = this.context.nativeWorkbenchApi
    let operation = ++this.annotationModeOperation
    const visibleMode = runtimeContextStateValue(
      this.context.getRenderState(),
      'annotationMode',
      this.annotationMode,
    )
    // User intent fences asynchronous selection work synchronously, before the
    // Desktop IPC round-trip can yield back to a late create continuation. The
    // visible pressed state is retained until Desktop confirms that its native
    // picker has actually stopped.
    if (!enabled) {
      this.annotationModeRestorePending = false
      this.updateAnnotationModeIntent(false)
      this.context.updateRenderState({
        annotationMode: visibleMode,
        annotationModeStopping: visibleMode,
      })
    }
    if (
      !nativeApi?.setArtifactAnnotationMode
      || this.nativeProtocolVersion !== 3
      || !this.createdSurface
      || (enabled && this.context.getRenderState().annotationAvailable !== true)
    ) {
      if (!enabled) {
        this.context.updateRenderState({
          annotationMode: visibleMode,
          annotationModeStopping: false,
        })
      }
      return false
    }
    const request = {
      version: 3 as const,
      surfaceId: this.item.id,
      enabled,
    }
    const invoke = async () => {
      try {
        return await nativeApi.setArtifactAnnotationMode!(request)
      } catch {
        return {
          ok: false,
          code: 'ANNOTATION_UNAVAILABLE',
          retryable: true,
        }
      }
    }
    let result = await invoke()
    if (operation !== this.annotationModeOperation) return false
    if (
      !result.ok
      && this.reserveNativeCapabilityRecovery(result, [
        'PREVIEW_CAPABILITY_EXPIRED',
        'PREVIEW_RENDERER_FAILED',
        // Older Desktop builds used this broad code when the scoped surface
        // disappeared. Preserve upgrade recovery without exposing diagnostics.
        'ANNOTATION_UNAVAILABLE',
      ])
    ) {
      const recovered = await this.recoverNativeCapabilitySurface(this.generation)
      if (recovered) {
        // Surface replacement deliberately fences the original operation.
        // This is the one allowed replay, now bound to the replacement surface.
        operation = ++this.annotationModeOperation
        result = await invoke()
      }
    }
    if (operation !== this.annotationModeOperation) return false
    if (!result.ok) {
      // Fail closed in the renderer so a late native selection cannot create
      // a draft. A rejected stop remains visibly pressed because Desktop has
      // not confirmed that the native picker is inactive; the next click can
      // retry the stop operation instead of accidentally enabling it again.
      if (enabled) this.updateAnnotationModeState(false, false)
      else {
        this.context.updateRenderState({
          annotationMode: visibleMode,
          annotationModeStopping: false,
        })
      }
      this.options.pushToast(
        this.options.t('workbench.artifactAnnotation.unavailable'),
        { tone: 'danger' },
      )
      return false
    }
    if (enabled) this.updateAnnotationModeState(true)
    else {
      this.context.updateRenderState({
        annotationMode: false,
        annotationModeStopping: false,
      })
    }
    return true
  }

  /**
   * Mark the native one-shot picker consumed without sending another Desktop
   * command. The `annotation-selected` event is emitted only after Desktop has
   * already stopped inspect mode; sending `enabled: false` here would clear the
   * just-issued opaque selection before the Gateway can resolve it.
   */
  private suspendAnnotationPicker() {
    this.annotationPickerArmed = false
  }

  private annotationSelectionFenceCurrent(fence: {
    attempt: number
    generation: number
    surfaceId: string
    sessionKey: string
    documentId: string
    revisionId: string
    modeIntent: boolean
  }): boolean {
    if (
      !fence.modeIntent
      || !this.annotationMode
      || fence.attempt !== this.annotationSelectionAttempt
      || fence.generation !== this.generation
      || fence.surfaceId !== this.item.id
      || !this.createdSurface
      || !this.context.isItemOpen()
    ) return false
    const current = this.currentDocument()
    return Boolean(
      current
      && current.sessionKey === fence.sessionKey
      && current.document.documentId === fence.documentId
      && current.document.headRevisionId === fence.revisionId,
    )
  }

  private async rearmAnnotationPickerIfNeeded(): Promise<boolean> {
    if (!this.annotationMode || this.annotationOverlayId || this.annotationSelectionPending) {
      return false
    }
    // Use the same generation-fenced capability recovery as the toolbar
    // action. A one-shot picker can disappear between two annotations when
    // Desktop replaces its scoped surface; that should be invisible to the
    // user and must not release the pressed annotation session.
    return await this.setAnnotationMode(true)
  }

  private async restoreAnnotationModeAfterSurfaceRefresh(): Promise<boolean> {
    if (!this.annotationModeRestorePending) return false
    // While an unsaved native editor is represented by the Web fallback, keep
    // the picker intent pending. Submitting or cancelling that editor owns the
    // next rearm, so a replacement Preview never opens a picker behind it.
    if (this.annotationOverlayId) return false
    if (
      !this.createdSurface
      || this.context.getRenderState().annotationAvailable !== true
    ) return false
    this.annotationModeRestorePending = false
    return await this.setAnnotationMode(true)
  }

  async handleSurfaceRect(rect: NativeSurfaceRect, item: WorkbenchItem) {
    this.item = item
    this.rect = rect
    await this.syncSurfaceRect()
    if (rect.visible && this.nativeProtocolVersion === 3) {
      await this.refreshAnnotationCapability()
      await this.restoreAnnotationModeAfterSurfaceRefresh()
    }
  }

  async handleNativeSurfaceEvent(
    event: NativeWorkbenchSurfaceEvent,
    item: WorkbenchItem,
  ) {
    this.item = item
    if (!this.createdSurface) return
    if (event.type === 'annotation-selected') {
      await this.handleAnnotationSelected(event.detail?.selection)
    } else if (event.type === 'annotation-draft-change') {
      this.handleAnnotationDraftChange(
        event.detail?.annotationId || '',
        event.detail?.body || '',
      )
    } else if (event.type === 'annotation-submit') {
      await this.handleAnnotationSubmit(
        event.detail?.annotationId || '',
        event.detail?.body || '',
      )
    } else if (event.type === 'annotation-cancel') {
      await this.handleAnnotationCancel(event.detail?.annotationId || '')
    } else if (event.type === 'annotation-overlay-fallback') {
      const annotationId = event.detail?.annotationId || this.annotationOverlayId
      if (!annotationId || annotationId !== this.annotationOverlayId) return
      await this.flushAnnotationBody(annotationId, this.annotationOverlayBody)
      this.context.updateRenderState({
        annotationFallback: {
          annotationId,
          body: this.annotationOverlayBody,
          reason: event.detail?.reason || '',
          screenshotUrl: this.annotationScreenshotUrl,
        },
      })
      this.options.pushToast(
        this.options.t('workbench.artifactAnnotation.overlayFallback'),
        { tone: 'warn', duration: 7000 },
      )
    } else if (event.type === 'escape') {
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
        version: this.nativeProtocolVersion === 1 ? 2 : this.nativeProtocolVersion,
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

  private annotationId(): string {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
    return `annotation-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  }

  private async handleAnnotationSelected(
    selection: NativeArtifactAnnotationSelection | null | undefined,
  ) {
    const candidate = selection
    const current = this.currentDocument()
    const promptAnnotations = this.options.promptAnnotations
    const nativeApi = this.context.nativeWorkbenchApi
    if (
      !candidate
      || !candidate.selectionId
      || !candidate.tagName
      || !candidate.elementPath
      || !candidate.elementProofSha256
      || !this.annotationMode
      || !this.annotationPickerArmed
      || this.annotationSelectionPending
      || Boolean(this.annotationOverlayId)
      || !current
      || !promptAnnotations
      || !nativeApi?.showArtifactAnnotationOverlay
    ) return

    const attempt = ++this.annotationSelectionAttempt
    const fence = {
      attempt,
      generation: this.generation,
      surfaceId: this.item.id,
      sessionKey: current.sessionKey,
      documentId: current.document.documentId,
      revisionId: current.document.headRevisionId,
      modeIntent: this.annotationMode,
    }
    this.annotationSelectionPending = true
    this.suspendAnnotationPicker()
    const annotationId = this.annotationId()
    const replacement = this.annotationReplacement
    let created: PromptAnnotation | null = null
    let screenshotUrl = ''
    let overlayRequested = false
    let takeoverCommitted = false
    let createErrorCode = ''

    const abandonLateContinuation = async () => {
      if (overlayRequested && created) {
        await nativeApi.closeArtifactAnnotationOverlay?.({
          version: 3,
          surfaceId: fence.surfaceId,
          annotationId: created.annotationId,
        }).catch(() => undefined)
      }
      await promptAnnotations.discard(created?.annotationId || annotationId).catch(() => false)
      if (created && this.annotationOverlayId === created.annotationId) {
        this.clearAnnotationOverlayState()
      } else {
        this.releaseAnnotationScreenshot(screenshotUrl)
        promptAnnotations.releaseOverlayEdit?.(created?.annotationId || annotationId)
      }
    }

    const commitReplacement = async () => {
      if (!replacement) return
      this.annotationReplacement = null
      if (!replacement.discardOriginal) return
      try {
        await promptAnnotations.discard(replacement.annotationId)
      } catch {
        // The new trusted editor/draft has already taken over. Never delete it
        // merely because cleanup of the stale source draft failed.
        this.options.pushToast(
          this.options.t('workbench.artifactAnnotation.replacementCleanupFailed'),
          { tone: 'warn', duration: 9000 },
        )
      }
    }

    this.releaseAnnotationScreenshot()
    promptAnnotations.beginOverlayEdit?.(annotationId, current.sessionKey)
    try {
      created = await promptAnnotations.create({
        annotationId,
        sessionKey: current.sessionKey,
        documentId: current.document.documentId,
        revisionId: current.document.headRevisionId,
        selection: {
          selectionId: candidate.selectionId,
          tagName: candidate.tagName,
          elementPath: candidate.elementPath,
          elementProofSha256: candidate.elementProofSha256,
          ...(candidate.domSha256 ? { domSha256: candidate.domSha256 } : {}),
        },
        ...(replacement ? { body: replacement.body } : {}),
      })
      if (!created || !this.annotationSelectionFenceCurrent(fence)) {
        await abandonLateContinuation()
        return
      }
      screenshotUrl = await this.captureAnnotationScreenshot()
      if (!this.annotationSelectionFenceCurrent(fence)) {
        await abandonLateContinuation()
        return
      }
      promptAnnotations.setActiveDocument(current.sessionKey, current.document.documentId)
      this.annotationOverlayAttempt += 1
      this.annotationOverlayId = created.annotationId
      this.annotationOverlayBody = created.body
      this.annotationScreenshotUrl = screenshotUrl
      const supportsLocalizedOverlay = this.annotationOverlayCopyVersion === 1
      if (!supportsLocalizedOverlay) {
        await this.hideNativeSurfaceForAnnotationFallback()
        this.context.updateRenderState({
          annotationFallback: {
            annotationId: created.annotationId,
            body: created.body,
            reason: 'localized-overlay-unavailable',
            screenshotUrl: this.annotationScreenshotUrl,
          },
        })
        takeoverCommitted = true
        await commitReplacement()
        return
      }
      overlayRequested = true
      const overlayRequest = {
        version: 3,
        surfaceId: fence.surfaceId,
        selectionId: candidate.selectionId,
        annotationId: created.annotationId,
        ...(created.body ? { initialBody: created.body } : {}),
        overlayCopyVersion: 1,
        copy: this.annotationOverlayCopy(created),
      } as const
      const invokeOverlay = async () => {
        try {
          return await nativeApi.showArtifactAnnotationOverlay!(overlayRequest)
        } catch {
          return {
            ok: false,
            code: 'PREVIEW_RENDERER_FAILED',
            retryable: true,
          }
        }
      }
      let shown = await invokeOverlay()
      if (!this.annotationSelectionFenceCurrent(fence)) {
        await abandonLateContinuation()
        return
      }
      if (
        !shown.ok
        && this.reserveNativeCapabilityRecovery(shown, ['PREVIEW_RENDERER_FAILED'])
      ) {
        // Desktop disposes the failed trusted overlay while preserving the
        // opaque selection. Replay this exact, bounded request once; a second
        // failure takes the existing localized Web fallback path.
        shown = await invokeOverlay()
        if (!this.annotationSelectionFenceCurrent(fence)) {
          await abandonLateContinuation()
          return
        }
      }
      if (!shown.ok) {
        this.context.updateRenderState({
          annotationFallback: {
            annotationId: created.annotationId,
            body: created.body,
            reason: shown.code || 'overlay-unavailable',
            screenshotUrl: this.annotationScreenshotUrl,
          },
        })
        this.options.pushToast(
          this.options.t('workbench.artifactAnnotation.overlayFallback'),
          { tone: 'warn', duration: 7000 },
        )
      } else {
        this.releaseAnnotationScreenshot()
      }
      // Only retire the old stale draft after a native editor or an explicit
      // trusted fallback has successfully taken ownership of the new draft.
      takeoverCommitted = true
      await commitReplacement()
    } catch (error) {
      createErrorCode = promptAnnotationRpcErrorCode(error)
      if (!takeoverCommitted && !this.annotationSelectionFenceCurrent(fence)) {
        await abandonLateContinuation()
        return
      }
      if (!created) {
        if (createErrorCode !== 'ARTIFACT_ANNOTATION_CREATE_AMBIGUOUS'
          && createErrorCode !== 'ARTIFACT_ANNOTATION_CREATE_CONFLICT') {
          await promptAnnotations.discard(annotationId).catch(() => false)
        }
        promptAnnotations.releaseOverlayEdit?.(annotationId)
        this.clearAnnotationOverlayState()
      } else {
        this.context.updateRenderState({
          annotationFallback: {
            annotationId: created.annotationId,
            body: created.body,
            reason: 'overlay-unavailable',
            screenshotUrl: this.annotationScreenshotUrl,
          },
        })
        await commitReplacement()
      }
      if (created) {
        this.options.pushToast(
          this.options.t('workbench.artifactAnnotation.overlayFallback'),
          { tone: 'warn' },
        )
      } else if ([
        'ARTIFACT_ELEMENT_CHANGED',
        // Older Gateways used the whole-DOM name for the same recoverable
        // selection rejection. Keep the UX actionable during upgrades.
        'ARTIFACT_DOM_CHANGED',
      ].includes(createErrorCode)) {
        this.options.pushToast(
          this.options.t('workbench.artifactAnnotation.elementChanged'),
          { tone: 'warn', duration: 12_000 },
        )
      } else {
        this.options.pushToast(
          this.options.t('workbench.artifactAnnotation.createFailed'),
          { tone: 'danger', duration: 9000 },
        )
      }
    } finally {
      if (attempt === this.annotationSelectionAttempt) {
        this.annotationSelectionPending = false
      }
    }
    if (!created && createErrorCode !== 'ARTIFACT_ANNOTATION_CREATE_AMBIGUOUS') {
      await this.rearmAnnotationPickerIfNeeded()
    }
  }

  private handleAnnotationDraftChange(annotationId: string, body: string) {
    if (!annotationId || annotationId !== this.annotationOverlayId) return
    this.annotationOverlayBody = body
    if (this.annotationUpdateTimer) clearTimeout(this.annotationUpdateTimer)
    this.annotationUpdateTimer = setTimeout(() => {
      this.annotationUpdateTimer = null
      void this.flushAnnotationBody(annotationId, body)
    }, 250)
  }

  private async flushAnnotationBody(annotationId: string, body: string): Promise<boolean> {
    if (!annotationId || !this.options.promptAnnotations) return false
    if (this.annotationUpdateTimer) {
      clearTimeout(this.annotationUpdateTimer)
      this.annotationUpdateTimer = null
    }
    try {
      await this.options.promptAnnotations.update(annotationId, body)
      return true
    } catch {
      this.options.pushToast(
        this.options.t('workbench.artifactAnnotation.updateFailed'),
        { tone: 'danger' },
      )
      return false
    }
  }

  private annotationOverlayFence(annotationId: string) {
    if (!annotationId || annotationId !== this.annotationOverlayId) return null
    return {
      attempt: this.annotationOverlayAttempt,
      generation: this.generation,
      surfaceId: this.item.id,
      annotationId,
    }
  }

  private annotationOverlayFenceCurrent(fence: {
    attempt: number
    generation: number
    surfaceId: string
    annotationId: string
  }): boolean {
    return fence.attempt === this.annotationOverlayAttempt
      && fence.generation === this.generation
      && fence.surfaceId === this.item.id
      && fence.annotationId === this.annotationOverlayId
      && this.createdSurface
      && this.context.isItemOpen()
  }

  private async closeAnnotationOverlay(fence: {
    attempt: number
    generation: number
    surfaceId: string
    annotationId: string
  }): Promise<{ ok: boolean; code?: string; retryable?: boolean }> {
    const close = this.context.nativeWorkbenchApi?.closeArtifactAnnotationOverlay
    if (!close) return { ok: false }
    try {
      const result = await close({
        version: 3,
        surfaceId: fence.surfaceId,
        annotationId: fence.annotationId,
      })
      if (
        !result.ok
        && result.retryable === true
        && (result.code === 'PREVIEW_CAPABILITY_EXPIRED'
          || result.code === 'ANNOTATION_UNAVAILABLE')
      ) {
        // The scoped surface is already gone, so its trusted overlay is gone
        // too. Treat close as acknowledged; after local cleanup the normal
        // rearm path rebuilds the current surface once and retries the picker.
        return { ok: true }
      }
      return result
    } catch {
      return {
        ok: false,
        code: 'ANNOTATION_UNAVAILABLE',
        retryable: true,
      }
    }
  }

  private showAnnotationCloseFailure() {
    this.options.pushToast(
      this.options.t('workbench.artifactAnnotation.closeFailed'),
      { tone: 'danger', duration: 9000 },
    )
  }

  private async handleAnnotationSubmit(annotationId: string, body: string) {
    const fence = this.annotationOverlayFence(annotationId)
    if (!fence || !body.trim() || this.annotationOverlayOperation) return
    const operation = Symbol('annotation-submit')
    this.annotationOverlayOperation = operation
    try {
      // A prior cancel may already have durably discarded this draft while a
      // native close failed. It cannot be resurrected; only retry that close.
      const alreadyDiscarded = this.annotationOverlayDiscarded?.attempt === fence.attempt
        && this.annotationOverlayDiscarded.annotationId === fence.annotationId
      if (!alreadyDiscarded) {
        this.annotationOverlayBody = body
        if (!await this.flushAnnotationBody(annotationId, body)) return
        if (!this.annotationOverlayFenceCurrent(fence)) return
      }
      const closed = await this.closeAnnotationOverlay(fence)
      if (!this.annotationOverlayFenceCurrent(fence)) return
      if (!closed.ok) {
        this.showAnnotationCloseFailure()
        return
      }
      this.options.promptAnnotations?.completeOverlayEdit?.(annotationId)
      if (!this.clearAnnotationOverlayState(fence)) return
      await this.syncSurfaceRect()
      // Adding one draft only completes that element's editor. Keep the
      // explicit annotation session active so the user can select more page
      // elements; the main chat send acceptance is the terminal boundary that
      // releases the picker and pressed toolbar state.
      await this.rearmAnnotationPickerIfNeeded()
    } finally {
      if (this.annotationOverlayOperation === operation) {
        this.annotationOverlayOperation = null
      }
    }
  }

  private async handleAnnotationCancel(annotationId: string) {
    const fence = this.annotationOverlayFence(annotationId)
    if (!fence || this.annotationOverlayOperation) return
    const operation = Symbol('annotation-cancel')
    this.annotationOverlayOperation = operation
    try {
      if (this.annotationUpdateTimer) {
        clearTimeout(this.annotationUpdateTimer)
        this.annotationUpdateTimer = null
      }
      const alreadyDiscarded = this.annotationOverlayDiscarded?.attempt === fence.attempt
        && this.annotationOverlayDiscarded.annotationId === fence.annotationId
      if (!alreadyDiscarded) {
        try {
          await this.options.promptAnnotations?.discard(annotationId)
        } catch {
          this.options.pushToast(
            this.options.t('workbench.artifactAnnotation.discardFailed'),
            { tone: 'danger', duration: 9000 },
          )
          return
        }
        if (!this.annotationOverlayFenceCurrent(fence)) return
        this.annotationOverlayDiscarded = {
          attempt: fence.attempt,
          annotationId: fence.annotationId,
        }
      }
      // Explicit close is the native acknowledgement. Until it succeeds the
      // trusted editor remains visible and owns the opaque selection.
      const closed = await this.closeAnnotationOverlay(fence)
      if (!this.annotationOverlayFenceCurrent(fence)) return
      if (!closed.ok) {
        this.showAnnotationCloseFailure()
        return
      }
      if (!this.clearAnnotationOverlayState(fence)) return
      await this.syncSurfaceRect()
      await this.rearmAnnotationPickerIfNeeded()
    } finally {
      if (this.annotationOverlayOperation === operation) {
        this.annotationOverlayOperation = null
      }
    }
  }

  private clearAnnotationOverlayState(expected?: {
    attempt: number
    annotationId: string
  }): boolean {
    if (
      expected
      && (
        expected.attempt !== this.annotationOverlayAttempt
        || expected.annotationId !== this.annotationOverlayId
      )
    ) return false
    if (this.annotationUpdateTimer) clearTimeout(this.annotationUpdateTimer)
    const annotationId = this.annotationOverlayId
    this.annotationUpdateTimer = null
    this.annotationOverlayOperation = null
    this.annotationOverlayDiscarded = null
    this.annotationOverlayAttempt += 1
    this.annotationOverlayId = ''
    this.annotationOverlayBody = ''
    this.options.promptAnnotations?.releaseOverlayEdit?.(annotationId)
    this.releaseAnnotationScreenshot()
    this.context.updateRenderState({ annotationFallback: null })
    return true
  }

  private annotationOverlayCopy(annotation: PromptAnnotation) {
    const newlineShortcut = isMacPlatform() ? '⇧ Return' : 'Shift + Enter'
    return {
      targetLabel: promptAnnotationTargetLabel(annotation, this.options.t),
      contextLabel: this.options.t('workbench.artifactAnnotation.contextLabel'),
      bodyLabel: this.options.t('workbench.artifactAnnotation.bodyLabel'),
      placeholder: this.options.t('workbench.artifactAnnotation.placeholder'),
      newlineHint: this.options.t('workbench.artifactAnnotation.newlineHint', {
        shortcut: newlineShortcut,
      }),
      cancelLabel: this.options.t('common.cancel'),
      submitLabel: this.options.t('workbench.artifactAnnotation.submit'),
      emptyBodyMessage: this.options.t('workbench.artifactAnnotation.emptyBody'),
    }
  }

  private async hideNativeSurfaceForAnnotationFallback() {
    const nativeApi = this.context.nativeWorkbenchApi
    if (!nativeApi || !this.rect || !this.createdSurface) return
    try {
      await nativeApi.setSurfaceRect({
        surfaceId: this.item.id,
        x: this.rect.x,
        y: this.rect.y,
        width: this.rect.width,
        height: this.rect.height,
        visible: false,
      })
    } catch {
      // The DOM fallback still owns the saved draft; surface recovery is
      // retried when the fallback closes.
    }
  }

  private async captureAnnotationScreenshot(): Promise<string> {
    const screenshot = this.context.nativeWorkbenchApi?.screenshot
    if (
      !screenshot
      || typeof URL === 'undefined'
      || typeof URL.createObjectURL !== 'function'
    ) return ''
    try {
      const result = await screenshot({ version: 3 })
      if (!result.ok) return ''
      const copied = new Uint8Array(result.value.data.byteLength)
      copied.set(result.value.data)
      return URL.createObjectURL(new Blob(
        [copied.buffer],
        { type: 'image/png' },
      ))
    } catch {
      // The trusted text editor remains usable without a frozen preview.
      return ''
    }
  }

  private releaseAnnotationScreenshot(url = this.annotationScreenshotUrl) {
    if (url === this.annotationScreenshotUrl) this.annotationScreenshotUrl = ''
    if (
      !url
      || typeof URL === 'undefined'
      || typeof URL.revokeObjectURL !== 'function'
    ) return
    try {
      URL.revokeObjectURL(url)
    } catch {}
  }

  private preserveAnnotationFallback(reason = 'update-pending') {
    if (!this.annotationOverlayId) return
    this.context.updateRenderState({
      annotationFallback: {
        annotationId: this.annotationOverlayId,
        body: this.annotationOverlayBody,
        reason,
        screenshotUrl: this.annotationScreenshotUrl,
      },
    })
  }

  async suspend() {
    if (!this.rect) return
    await this.setSurfaceRect({ ...this.rect, visible: false })
  }

  async resume() {
    await this.syncSurfaceRect()
    if (this.nativeProtocolVersion === 3) {
      await this.refreshAnnotationCapability()
      await this.restoreAnnotationModeAfterSurfaceRefresh()
    }
  }

  async beforeClose(options?: WorkbenchBeforeCloseOptions): Promise<boolean> {
    if (this.annotationOverlayId) {
      if (!await this.flushAnnotationBody(
        this.annotationOverlayId,
        this.annotationOverlayBody,
      )) {
        this.preserveAnnotationFallback()
        return false
      }
    }
    return await this.component?.beforeClose?.(options) ?? true
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
    // Desktop only advertises the picker for the active, visible v3 surface.
    // Query after positioning/activation so a valid editor is not cached off.
    await this.refreshAnnotationCapability()
    await this.restoreAnnotationModeAfterSurfaceRefresh()
  }

  private currentDocument() {
    if (isPreparedImmutableResourcePreview(this.item)) return null
    const artifact = artifactFromWorkbenchItem(this.item)
    if (!artifact) return null
    const sessionKey = artifactSessionKey(this.item, this.options)
    const workspace = this.options.artifactDocuments?.snapshot(artifact, sessionKey).workspace
    if (
      !workspace
      || workspace.source !== 'document-api'
      || workspace.document.kind !== 'html'
      || !workspace.document.capabilities.source
      || !workspace.document.capabilities.manualEdit
      || !workspace.document.capabilities.agentEdit
      || !workspace.document.capabilities.selectionContext
      || workspace.document.capabilities.promptAnnotations !== true
    ) return null
    return { artifact, document: workspace.document, sessionKey }
  }

  private async refreshAnnotationCapability() {
    const nativeApi = this.context.nativeWorkbenchApi
    const document = this.currentDocument()
    if (
      this.nativeProtocolVersion !== 3
      || this.options.platform.id !== 'desktop'
      || !this.createdSurface
      || !document
      || !this.options.promptAnnotations
      || !nativeApi?.getArtifactAnnotationCapabilities
      || !nativeApi.setArtifactAnnotationMode
      || !nativeApi.showArtifactAnnotationOverlay
      || !nativeApi.closeArtifactAnnotationOverlay
    ) {
      this.annotationOverlayCopyVersion = 0
      const preserveRestoreIntent = this.annotationModeRestorePending
      this.annotationModeOperation += 1
      this.annotationSelectionAttempt += 1
      this.annotationSelectionPending = false
      this.annotationMode = false
      this.annotationPickerArmed = false
      if (!preserveRestoreIntent) this.annotationReplacement = null
      this.context.updateRenderState({
        annotationAvailable: false,
        annotationMode: false,
        annotationModeStopping: false,
      })
      return
    }
    try {
      const capability = await nativeApi.getArtifactAnnotationCapabilities()
      this.annotationOverlayCopyVersion = capability.overlayCopyVersion === 1 ? 1 : 0
      const available = capability.version === 3
        && capability.available
        && capability.picker !== false
        && capability.trustedOverlay !== false
      if (!available) {
        const preserveRestoreIntent = this.annotationModeRestorePending
        this.annotationModeOperation += 1
        this.annotationSelectionAttempt += 1
        this.annotationSelectionPending = false
        this.annotationMode = false
        this.annotationPickerArmed = false
        if (!preserveRestoreIntent) this.annotationReplacement = null
      }
      const renderedMode = runtimeContextStateValue(
        this.context.getRenderState(),
        'annotationMode',
        this.annotationMode,
      )
      const stopping = available && runtimeContextStateValue(
        this.context.getRenderState(),
        'annotationModeStopping',
        false,
      )
      // A pressed renderer state with an already-fenced intent means Desktop
      // has not acknowledged the stop. A capability refresh must not turn
      // that uncertainty into a false success signal.
      const preserveUnconfirmedStop = available
        && renderedMode
        && !this.annotationMode
      this.context.updateRenderState({
        annotationAvailable: available,
        annotationMode: available
          ? preserveUnconfirmedStop ? renderedMode : this.annotationMode
          : false,
        annotationModeStopping: stopping,
        annotationUnavailableReason: capability.reason || '',
      })
    } catch {
      this.annotationOverlayCopyVersion = 0
      const preserveRestoreIntent = this.annotationModeRestorePending
      this.annotationModeOperation += 1
      this.annotationSelectionAttempt += 1
      this.annotationSelectionPending = false
      this.annotationMode = false
      this.annotationPickerArmed = false
      if (!preserveRestoreIntent) this.annotationReplacement = null
      this.context.updateRenderState({
        annotationAvailable: false,
        annotationMode: false,
        annotationModeStopping: false,
      })
    }
  }

  private async prepareLeasePreview(): Promise<boolean> {
    const preparedPreview = preparedPreviewFromWorkbenchItem(this.item)
    if (preparedPreview) {
      this.context.updateRenderState({
        effectiveMode: 'offline',
        fullModeAvailable: false,
        previewBlocked: false,
        previewLaunchUrl: preparedPreview.launchUrl || '',
        previewMode: 'offline',
        previewNetworkAllowed: false,
        previewSandboxProfile: 'opaque-offline',
        remoteResourcesEnabled: false,
      })
      return false
    }
    const originalArtifact = artifactFromWorkbenchItem(this.item)
    if (!originalArtifact) return false
    // The stable download URL and immutable preview identity serve different
    // purposes. The lease broker receives the current revision's real
    // artifact-store ID; downloads continue to use the document-head URL.
    const artifact = this.options.artifactDocuments?.headArtifact(
      originalArtifact,
      artifactSessionKey(this.item, this.options),
    ) || originalArtifact
    const nativeApi = this.context.nativeWorkbenchApi
    const capabilities = nativeApi?.getCapabilities
      ? await nativeApi.getCapabilities()
      : { protocolVersions: [1] as Array<1 | 2 | 3>, modes: ['offline'] as WorkbenchPreviewMode[] }
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
      && (
        (!capabilities.protocolVersions.includes(2)
          && !capabilities.protocolVersions.includes(3))
        || !hasNativeLeaseBroker
      )
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
    this.leaseArtifactId = String(artifact.id || '')
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
    this.nativeProtocolVersion = capabilities.protocolVersions.includes(3) ? 3 : 2
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
      version: this.nativeProtocolVersion === 3 ? 3 : 2,
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
    // The native capability is scoped to the active, visible v3 surface. A
    // replacement keeps the previous rect, so activate it before querying;
    // otherwise a reload can cache "unavailable" until an unrelated resize.
    await this.refreshAnnotationCapability()
    await this.restoreAnnotationModeAfterSurfaceRefresh()
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

  private async ensureCanonicalDocumentHead(expectedRevisionId: string): Promise<boolean> {
    const artifact = artifactFromWorkbenchItem(this.item)
    const documents = this.options.artifactDocuments
    if (!artifact || !documents) return false
    const sessionKey = artifactSessionKey(this.item, this.options)
    const snapshot = documents.snapshot(artifact, sessionKey)
    if (this.documentHeadReached(snapshot, expectedRevisionId)) return true
    try {
      await documents.load(artifact, sessionKey, { force: true })
      return this.documentHeadReached(
        documents.snapshot(artifact, sessionKey),
        expectedRevisionId,
      )
    } catch {
      return false
    }
  }

  private documentHeadReached(
    snapshot: ArtifactDocumentWorkspaceSnapshot,
    expectedRevisionId: string,
  ): boolean {
    const workspace = snapshot.workspace
    if (
      snapshot.loading
      || !snapshot.loaded
      || snapshot.stale
      || workspace?.source !== 'document-api'
    ) return false
    const headRevisionId = workspace.document.headRevisionId
    if (headRevisionId === expectedRevisionId) return true
    const expected = workspace.revisions.find(
      revision => revision.revisionId === expectedRevisionId,
    )
    const head = workspace.revisions.find(
      revision => revision.revisionId === headRevisionId,
    )
    return Boolean(
      expected
      && head
      && Number.isSafeInteger(expected.generation)
      && Number.isSafeInteger(head.generation)
      && head.generation >= expected.generation,
    )
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
      if (this.lease !== lease) return
      if (renewal.lease_id !== lease.lease_id) {
        throw new ArtifactPreviewLeaseError('Preview lease identity changed.', 502)
      }
      this.lease = {
        ...lease,
        expires_at: renewal.expires_at,
      }
    } catch (error) {
      if (this.lease !== lease) return
      if (
        error instanceof ArtifactPreviewLeaseError
        && (error.status === 404 || error.status === 410)
      ) {
        try {
          await this.replaceLeasePreview()
        } catch (replacementError) {
          // Renewal runs without an awaiting caller. Always settle a failed
          // replacement into the localized Preview error state so no rejected
          // promise escapes the interval callback.
          await this.handleLeaseFailure(replacementError)
        }
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
    this.leaseArtifactId = ''
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
    if (!previewLeaseEnabledForItem(this.item, this.options)) {
      await this.component?.reload()
      return
    }
    try {
      if (
        this.blockedHeadRevisionId
        && !await this.ensureCanonicalDocumentHead(this.blockedHeadRevisionId)
      ) {
        throw new Error(this.options.t('workbench.artifactDocument.sourceUnavailable'))
      }
      this.blockedHeadRevisionId = ''
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
    const message = productErrorMessage(error, this.options)
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
      // A retained Web annotation editor owns the user's unpersisted body and
      // frozen screenshot while the Preview is rebuilt. Keep the new native
      // surface behind it until submit/cancel closes that fallback.
      visible: rect.visible && !this.context.getRenderState().annotationFallback,
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
    if (this.nativeProtocolVersion !== 1) return
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
    const overlayId = this.annotationOverlayId
    const preserveModeIntent = this.annotationModeRestorePending || this.annotationMode
    let preserveAnnotationFallback = false
    if (overlayId) {
      preserveAnnotationFallback = !await this.flushAnnotationBody(
        overlayId,
        this.annotationOverlayBody,
      )
      if (preserveAnnotationFallback) {
        this.annotationModeRestorePending = preserveModeIntent
        this.preserveAnnotationFallback()
      }
    }
    this.annotationReplacement = null
    this.annotationModeOperation += 1
    this.annotationSelectionAttempt += 1
    this.annotationSelectionPending = false
    if (!preserveAnnotationFallback) this.releaseAnnotationScreenshot()
    if (!nativeApi || !this.createdSurface) {
      if (this.annotationOverlayId && !preserveAnnotationFallback) {
        this.clearAnnotationOverlayState()
      }
      this.createdSurface = false
      this.annotationMode = preserveAnnotationFallback && preserveModeIntent
      this.annotationPickerArmed = false
      this.context.updateRenderState({
        annotationMode: this.annotationMode,
        annotationModeStopping: false,
        annotationAvailable: false,
      })
      return true
    }

    if (this.annotationOverlayId) {
      try {
        await nativeApi.closeArtifactAnnotationOverlay?.({
          version: 3,
          surfaceId: this.item.id,
          annotationId: this.annotationOverlayId,
        })
      } catch {}
      if (!preserveAnnotationFallback) this.clearAnnotationOverlayState()
    }
    if (this.annotationMode) {
      try {
        await nativeApi.setArtifactAnnotationMode?.({
          version: 3,
          surfaceId: this.item.id,
          enabled: false,
        })
      } catch {}
    }
    this.annotationMode = preserveAnnotationFallback && preserveModeIntent
    this.annotationPickerArmed = false
    this.context.updateRenderState({
      annotationMode: this.annotationMode,
      annotationModeStopping: false,
      annotationAvailable: false,
    })

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
    if (this.nativeRecoveryInFlight) {
      await this.nativeRecoveryInFlight
      return
    }

    const failedGeneration = this.generation
    const recoveryKey = this.nativeRecoveryKey()
    if (!this.nativeRecoveryAttemptedKeys.has(recoveryKey)) {
      this.nativeRecoveryAttemptedKeys.add(recoveryKey)
      const recovery = this.recoverNativeSurface(failedGeneration)
      this.nativeRecoveryInFlight = recovery
      try {
        await recovery
      } finally {
        if (this.nativeRecoveryInFlight === recovery) this.nativeRecoveryInFlight = null
      }
      return
    }

    await this.releaseNativeSurface(false)
    if (!this.context.isItemOpen()) return
    const error = surfaceError('Preview renderer failed')
    this.context.updateRenderState({ nativeSurfaceState: state })
    this.context.reportError(error)
    this.options.pushToast(
      this.options.t('workbench.artifactPreview.failedDetail'),
      { tone: 'danger' },
    )
  }

  private nativeRecoveryKey(): string {
    const artifact = artifactFromWorkbenchItem(this.item)
    const sessionKey = artifactSessionKey(this.item, this.options)
    const workspace = artifact
      ? this.options.artifactDocuments?.snapshot(artifact, sessionKey).workspace
      : null
    const headRevisionId = workspace?.source === 'document-api'
      ? workspace.document.headRevisionId
      : ''
    const resourceArtifact = this.resource?.artifact
    const resourceIdentity = String(
      this.item.payload.resourceIdentity
      || resourceArtifact?.id
      || resourceArtifact?.sha256
      || artifact?.id
      || artifact?.sha256
      || this.leaseArtifactId
      || this.item.id,
    )
    return `${this.item.id}:${resourceIdentity}:${headRevisionId || this.leaseArtifactId}`
  }

  private reserveNativeCapabilityRecovery(
    result: { retryable?: boolean; code?: string },
    codes: readonly string[],
  ): boolean {
    if (result.retryable !== true || !result.code || !codes.includes(result.code)) {
      return false
    }
    const recoveryKey = this.nativeRecoveryKey()
    if (this.nativeRecoveryAttemptedKeys.has(recoveryKey)) return false
    this.nativeRecoveryAttemptedKeys.add(recoveryKey)
    return true
  }

  private async recoverNativeCapabilitySurface(failedGeneration: number): Promise<boolean> {
    if (this.nativeRecoveryInFlight) {
      await this.nativeRecoveryInFlight
      return this.context.isItemOpen() && this.createdSurface
    }
    const recovery = this.recoverNativeSurface(failedGeneration)
    this.nativeRecoveryInFlight = recovery
    try {
      await recovery
    } finally {
      if (this.nativeRecoveryInFlight === recovery) this.nativeRecoveryInFlight = null
    }
    return (
      this.context.isItemOpen()
      && this.createdSurface
      && this.generation !== failedGeneration
    )
  }

  private async recoverNativeSurface(failedGeneration: number) {
    if (
      failedGeneration !== this.generation
      || !this.createdSurface
      || !this.context.isItemOpen()
    ) return

    const renderedMode = runtimeContextStateValue(
      this.context.getRenderState(),
      'annotationMode',
      this.annotationMode,
    )
    const stopping = runtimeContextStateValue(
      this.context.getRenderState(),
      'annotationModeStopping',
      false,
    )
    this.annotationModeRestorePending = this.annotationMode || (renderedMode && !stopping)

    if (previewLeaseEnabledForItem(this.item, this.options)) {
      try {
        await this.replaceLeasePreview()
      } catch (error) {
        await this.handleLeaseFailure(error)
      }
      return
    }

    const resource = this.resource
    if (!resource) {
      await this.failNativeSurface(surfaceError('Preview renderer failed'))
      return
    }
    if (!await this.releaseNativeSurface(false)) {
      await this.failNativeSurface(surfaceError('Failed to reset the native Workbench surface'))
      return
    }
    if (!this.context.isItemOpen()) return
    await this.createNativeSurface(resource)
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
  if (runtimeStateValue(state, 'annotationAvailable', false)) {
    const enabled = runtimeStateValue(state, 'annotationMode', false)
    const stopping = runtimeStateValue(state, 'annotationModeStopping', false)
    items.push({
      kind: 'action',
      id: 'toggle-annotation-mode',
      icon: 'pencil',
      label: options.t(enabled
        ? 'workbench.artifactAnnotation.stop'
        : 'workbench.artifactAnnotation.start'),
      disabled: stopping,
      pressed: enabled,
    })
  }
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
  const hasPreparedPreview = preparedPreviewFromWorkbenchItem(item) !== null
  const hasLease = Boolean(runtimeStateValue(state, 'previewLaunchUrl', ''))
  if (hasLease && !hasPreparedPreview) {
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
    !hasPreparedPreview
    && item.hostKind === 'native-webcontents'
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
      component: ArtifactDocumentPanel,
      supports: item => artifactFromWorkbenchItem(item) !== null,
      getHeader: artifactHeader,
      getToolbarItems: (item, state) => artifactToolbarItems(item, state, options),
      getProps: (item, state) => ({
        artifact: artifactFromWorkbenchItem(item),
        documentActions: (() => {
          if (isPreparedImmutableResourcePreview(item)) return undefined
          const documents = options.artifactDocuments
          if (
            !documents?.restoreRevision
            || !documents.revertChangeSet
          ) return undefined
          return {
            restoreRevision: documents.restoreRevision,
            revertChangeSet: documents.revertChangeSet,
          } satisfies ArtifactDocumentActions
        })(),
        documentSnapshot: (() => {
          if (isPreparedImmutableResourcePreview(item)) return undefined
          const artifact = artifactFromWorkbenchItem(item)
          return artifact
            ? options.artifactDocuments?.snapshot(
                artifact,
                artifactSessionKey(item, options),
              )
            : undefined
        })(),
        documentFeatures: (() => {
          if (isPreparedImmutableResourcePreview(item)) return false
          const artifact = artifactFromWorkbenchItem(item)
          if (!artifact) return false
          return options.artifactDocuments?.snapshot(
            artifact,
            artifactSessionKey(item, options),
          ).workspace?.source === 'document-api'
        })(),
        initialSection: initialSectionFromWorkbenchItem(item),
        initialSectionRequestId: initialSectionRequestIdFromWorkbenchItem(item),
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
        previewNetworkAllowed: runtimeStateValue(
          state,
          'previewNetworkAllowed',
          true,
        ),
        previewSandboxProfile: runtimeStateValue(
          state,
          'previewSandboxProfile',
          'default',
        ),
        publishing: runtimeStateValue(state, 'documentPublishing', false),
        annotationFallback: runtimeStateValue(state, 'annotationFallback', null),
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
