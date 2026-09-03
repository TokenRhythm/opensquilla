import type { InjectionKey } from 'vue'
import type { Ref, ShallowRef } from 'vue'
import type { NativeWorkbenchApi, PlatformId } from '@/platform/types'
import type {
  ArtifactChangeSet,
  ArtifactDocument,
  ArtifactDocumentWorkspace,
  ArtifactEditCapabilities,
  ArtifactEditSession,
  ArtifactEditSessionCloseRequest,
  ArtifactEditSessionHeartbeatRequest,
  ArtifactEditSessionStartRequest,
  ArtifactMutationResolution,
  ArtifactMutationResolutionRequest,
  ArtifactRevision,
  ArtifactSourcePatchResult,
  ArtifactSourceSnapshot,
} from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/artifacts'
import type { DisplayAttachment } from '@/types/chat'
import type { ArtifactWorkbenchPreviewKind } from '@/utils/workbench/artifactPreview'
import type {
  DocumentImportResponse,
  DocumentPublishResponse,
  WorkbenchPreviewResponse,
  WorkbenchResource,
  WorkbenchResourceOpenResponse,
  WorkbenchResourceRef,
  WorkbenchResourcesListResponse,
  WorkbenchResourceType,
} from '@/types/workbenchResources'
import type {
  PromptAnnotation,
  PromptAnnotationCreateRequest,
  PromptAnnotationDiscardRequest,
  PromptAnnotationFocusRequest,
  PromptAnnotationFocusResult,
  PromptAnnotationUpdateRequest,
} from '@/types/promptAnnotations'

export interface OpenArtifactDocument {
  readonly sessionKey: string
  readonly artifactId: string
}

export interface CloseArtifactDocument {
  readonly sessionKey: string
  readonly documentId: string
}

export interface RenameArtifactDocument extends CloseArtifactDocument {
  readonly expectedStateRevision: number
  readonly name: string
}

export interface RestoreArtifactRevision extends CloseArtifactDocument {
  readonly revisionId: string
  readonly expectedHeadRevisionId: string
  readonly expectedStateRevision: number
  readonly clientRequestId?: string
  readonly idempotencyKey?: string
}

export interface RevertArtifactChangeSet extends CloseArtifactDocument {
  readonly changeSetId: string
  readonly expectedHeadRevisionId: string
  readonly expectedStateRevision: number
  readonly clientRequestId?: string
  readonly idempotencyKey?: string
}

export interface ReadArtifactSource extends CloseArtifactDocument {
  readonly revisionId?: string
}

export interface ArtifactSourceChange {
  readonly startOffset: number
  readonly endOffset: number
  readonly replacement: string
}

export interface PatchArtifactSource extends CloseArtifactDocument {
  readonly expectedHeadRevisionId: string
  readonly expectedSourceSha256: string
  readonly expectedStateRevision: number
  readonly patches: readonly [ArtifactSourceChange, ...ArtifactSourceChange[]]
  readonly offsetEncoding?: 'unicode-code-point'
  readonly editSessionId?: string
  readonly expectedEditSessionStateRevision?: number
  readonly expectedLastSavedRevisionId?: string
  readonly clientRequestId?: string
  readonly idempotencyKey?: string
}

export interface ArtifactDocumentProvider {
  getCapabilities(signal?: AbortSignal): Promise<ArtifactEditCapabilities>
  listDocuments(sessionKey: string, signal?: AbortSignal): Promise<ArtifactDocument[]>
  getDocument(documentId: string, sessionKey: string, signal?: AbortSignal): Promise<ArtifactDocument | null>
  loadWorkspace(artifact: ArtifactPayload, sessionKey: string, signal?: AbortSignal): Promise<ArtifactDocumentWorkspace>
  listRevisions(documentId: string, sessionKey: string, signal?: AbortSignal): Promise<ArtifactRevision[]>
  listChangeSets(documentId: string, sessionKey: string, signal?: AbortSignal): Promise<ArtifactChangeSet[]>
  getChangeSet(documentId: string, changeSetId: string, sessionKey: string, signal?: AbortSignal): Promise<ArtifactChangeSet | null>
  openDocument(request: OpenArtifactDocument, signal?: AbortSignal): Promise<{ document: ArtifactDocument | null; editSession: ArtifactEditSession | null }>
  closeDocument(request: CloseArtifactDocument, signal?: AbortSignal): Promise<ArtifactDocument | null>
  renameDocument(request: RenameArtifactDocument, signal?: AbortSignal): Promise<ArtifactDocument | null>
  restoreRevision(request: RestoreArtifactRevision, signal?: AbortSignal): Promise<ArtifactRevision | null>
  revertChangeSet(request: RevertArtifactChangeSet, signal?: AbortSignal): Promise<ArtifactChangeSet | null>
  readSource(request: ReadArtifactSource, signal?: AbortSignal): Promise<ArtifactSourceSnapshot | null>
  patchSource(request: PatchArtifactSource, signal?: AbortSignal): Promise<ArtifactSourcePatchResult | null>
  resolveMutation?(request: ArtifactMutationResolutionRequest, signal?: AbortSignal): Promise<ArtifactMutationResolution | null>
  startEditSession?(request: ArtifactEditSessionStartRequest, signal?: AbortSignal): Promise<ArtifactEditSession | null>
  heartbeatEditSession?(request: ArtifactEditSessionHeartbeatRequest, signal?: AbortSignal): Promise<ArtifactEditSession | null>
  closeEditSession?(request: ArtifactEditSessionCloseRequest, signal?: AbortSignal): Promise<ArtifactEditSession | null>
}

export interface WorkbenchResourceProvider {
  available(): boolean
  canImportDocuments(): boolean
  list(sessionKey: string, options?: { types?: WorkbenchResourceType[]; limit?: number; signal?: AbortSignal }): Promise<WorkbenchResourcesListResponse>
  get(sessionKey: string, resource: WorkbenchResourceRef, signal?: AbortSignal): Promise<WorkbenchResource | null>
  open?(sessionKey: string, resource: WorkbenchResourceRef, request: { intent: 'edit-current'; expectedSha256?: string; idempotencyKey: string }, signal?: AbortSignal): Promise<WorkbenchResourceOpenResponse | null>
  createPreview?(sessionKey: string, resource: WorkbenchResourceRef, signal?: AbortSignal): Promise<WorkbenchPreviewResponse | null>
  importDocument(request: { sessionKey: string; source: WorkbenchResourceRef; expectedSha256: string; idempotencyKey: string; name?: string }, signal?: AbortSignal): Promise<DocumentImportResponse>
  publishDocument(request: { sessionKey: string; documentId: string; revisionId: string; idempotencyKey: string; name?: string }, signal?: AbortSignal): Promise<DocumentPublishResponse>
  resolveMutation?(request: ArtifactMutationResolutionRequest, signal?: AbortSignal): Promise<ArtifactMutationResolution | null>
}

export interface ArtifactPromptAnnotationProvider {
  list(sessionKey: string, signal?: AbortSignal): Promise<PromptAnnotation[]>
  create(request: PromptAnnotationCreateRequest): Promise<PromptAnnotation | null>
  update(request: PromptAnnotationUpdateRequest): Promise<PromptAnnotation | null>
  discard(request: PromptAnnotationDiscardRequest): Promise<PromptAnnotation | null>
  focus(request: PromptAnnotationFocusRequest): Promise<PromptAnnotationFocusResult | null>
}

export interface ArtifactDocumentChange {
  readonly documentId: string
}

export interface ArtifactCatalog {
  listSession(
    sessionKey: string,
    options?: { readonly limit?: number; readonly signal?: AbortSignal },
  ): Promise<ArtifactPayload[] | null>
}

export interface ArtifactWorkbenchSubscription {
  close(): void
}

export interface ArtifactAccessRequest {
  readonly sessionKey?: string
  readonly signal?: AbortSignal
  readonly requireSameOrigin?: boolean
}

export type ArtifactFetchResult =
  | { readonly ok: true; readonly status: number; readonly url: string; readonly blob: Blob }
  | { readonly ok: false; readonly status: number; readonly url: string; readonly message: string }

export type ArtifactOpenResult =
  | { readonly ok: true; readonly status: number; readonly url: string; readonly objectUrl?: string }
  | { readonly ok: false; readonly status: number; readonly url: string; readonly message: string }

export type AttachmentFetchResult =
  | {
      readonly ok: true
      readonly status: number
      readonly source: 'local-file' | 'inline' | 'staged'
      readonly url: string
      readonly blob: Blob
      readonly filename: string
    }
  | {
      readonly ok: false
      readonly status: number
      readonly source: 'none' | 'inline' | 'staged'
      readonly url: string
      readonly message: string
    }

export interface AttachmentUploadReceipt {
  readonly fileUuid: string
  readonly expiresAt?: number
  readonly ttlSeconds?: number
}

/** Authenticated artifact and attachment bytes without endpoint or header leakage. */
export interface ArtifactContentAccess {
  fetchArtifact(
    artifact: ArtifactPayload,
    request?: ArtifactAccessRequest,
  ): Promise<ArtifactFetchResult>
  openArtifact(
    artifact: ArtifactPayload,
    request?: ArtifactAccessRequest,
  ): Promise<ArtifactOpenResult>
  openArtifactBlob(
    artifact: ArtifactPayload,
    request?: ArtifactAccessRequest,
  ): Promise<ArtifactOpenResult>
  clearPreviewStorage(previewOrigin: string): Promise<void>
  fetchAttachment(
    attachment: DisplayAttachment,
    request?: ArtifactAccessRequest,
  ): Promise<AttachmentFetchResult>
  uploadAttachment(file: File, mime: string): Promise<AttachmentUploadReceipt>
}

export type ArtifactPreviewState = 'idle' | 'loading' | 'loaded' | 'timeout' | 'error'
export type ArtifactPreviewErrorCode = 'network' | 'too_large' | 'unsupported' | null

export interface ArtifactPreviewOptions {
  artifact: () => ArtifactPayload
  sessionKey?: () => string | undefined
  variant?: 'content' | 'thumbnail'
  fullSize?: boolean
  timeoutMs?: number
  maxRetries?: number
  maxBytes?: number
  requireSameOrigin?: boolean
  acceptBlob?: (blob: Blob) => boolean
}

export interface ArtifactPreviewController {
  state: Ref<ArtifactPreviewState>
  errorCode: Ref<ArtifactPreviewErrorCode>
  progress: Ref<number | null>
  objectUrl: ShallowRef<string>
  load(): void
  retry(): void
  observe(el: Element | null): void
  release(): void
  dispose(): void
}

export type ArtifactPreviewResourceState =
  | 'crashed'
  | 'error'
  | 'idle'
  | 'loading'
  | 'missing-resource'
  | 'offline'
  | 'ready'
  | 'ready-with-warnings'
  | 'suspended'
  | 'unsupported'

export type ArtifactPreviewResourceErrorCode =
  | 'download-failed'
  | 'integrity-error'
  | 'invalid-content'
  | 'missing-url'
  | 'native-error'
  | 'native-crashed'
  | 'offline'
  | 'preview-blocked'
  | 'too-large'
  | 'unsupported'

export interface NativeHtmlArtifactResource {
  artifact: ArtifactPayload
  data: ArrayBuffer
  hasRelativeResources: boolean
  mime: string
  relativeResourceCount: number
  sessionKey: string
}

export interface ArtifactPreviewResourceOptions {
  artifact: () => ArtifactPayload
  createObjectUrl?: (blob: Blob) => string
  htmlCollectionStatus?: () => 'complete' | 'partial' | 'not_applicable'
  htmlLaunchUrl?: () => string
  htmlLeaseState?: () => 'ready' | 'pending' | 'blocked'
  nativeHtml?: () => boolean
  onNativeHtmlReady?: (resource: NativeHtmlArtifactResource) => void
  revokeObjectUrl?: (url: string) => void
  sessionKey?: () => string
}

export interface ArtifactPreviewResourceController {
  errorCode: Ref<ArtifactPreviewResourceErrorCode | null>
  kind: Ref<ArtifactWorkbenchPreviewKind>
  markdownHtml: ShallowRef<string>
  objectUrl: ShallowRef<string>
  progress: Ref<number | null>
  relativeResources: ShallowRef<string[]>
  state: Ref<ArtifactPreviewResourceState>
  text: ShallowRef<string>
  dispose(): void
  load(): Promise<void>
  markNativeCrashed(): void
  markNativeError(): void
  reload(): Promise<void>
  resume(): Promise<void>
  suspend(): void
}

export type ArtifactPreviewMode = 'full' | 'offline'
export type ArtifactPreviewCollectionStatus = 'complete' | 'partial' | 'not_applicable'

export interface ArtifactPreviewLeaseSource {
  kind: 'bundle' | 'single_file'
  collection_status: ArtifactPreviewCollectionStatus
  file_count: number
  total_bytes: number
  warning_codes: string[]
}

export interface ArtifactPreviewLease {
  version: 1
  lease_id: string
  effective_mode: ArtifactPreviewMode
  launch_url: string
  entrypoint: string
  expires_at: string
  preview_origin: string | null
  idle_timeout_seconds: number
  source: ArtifactPreviewLeaseSource
}

export interface ArtifactPreviewLeaseRenewal {
  version: 1
  lease_id: string
  expires_at: string
}

export type ArtifactPreviewNativeBroker = Pick<
  NativeWorkbenchApi,
  | 'createArtifactPreviewLease'
  | 'renewArtifactPreviewLease'
  | 'revokeArtifactPreviewLease'
>

export interface ArtifactPreviewLeaseRequest {
  nativeBroker?: ArtifactPreviewNativeBroker
  sessionKey?: string
}

export class ArtifactPreviewLeaseError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code = '',
  ) {
    super(message)
    this.name = 'ArtifactPreviewLeaseError'
  }
}

/** Named preview capability; generic HTTP remains private to its v4 Adapter. */
export interface ArtifactPreviewAccess {
  create(options: ArtifactPreviewOptions): ArtifactPreviewController
  createResource(options: ArtifactPreviewResourceOptions): ArtifactPreviewResourceController
  createLease(
    artifact: ArtifactPayload,
    mode: ArtifactPreviewMode,
    client: PlatformId,
    request?: ArtifactPreviewLeaseRequest,
  ): Promise<ArtifactPreviewLease>
  renewLease(
    leaseId: string,
    request?: ArtifactPreviewLeaseRequest,
  ): Promise<ArtifactPreviewLeaseRenewal>
  revokeLease(leaseId: string, request?: ArtifactPreviewLeaseRequest): Promise<void>
}

/** One semantic Workbench boundary; wire methods stay in its v4 Adapter. */
export interface ArtifactWorkbench {
  readonly artifacts: ArtifactCatalog
  readonly documents: ArtifactDocumentProvider
  readonly resources: WorkbenchResourceProvider
  readonly promptAnnotations: ArtifactPromptAnnotationProvider
  readonly content: ArtifactContentAccess
  readonly previews: ArtifactPreviewAccess
  ready(): Promise<void>
  subscribeDocumentChanges(
    listener: (change: ArtifactDocumentChange) => void,
  ): ArtifactWorkbenchSubscription
}

export const ARTIFACT_WORKBENCH_KEY: InjectionKey<ArtifactWorkbench> = Symbol('ArtifactWorkbench')
