import type { InjectionKey } from 'vue'
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

export interface ArtifactDocumentProvider {
  getCapabilities(signal?: AbortSignal): Promise<ArtifactEditCapabilities>
  listDocuments(sessionKey: string, signal?: AbortSignal): Promise<ArtifactDocument[]>
  getDocument(documentId: string, sessionKey: string, signal?: AbortSignal): Promise<ArtifactDocument | null>
  loadWorkspace(artifact: ArtifactPayload, sessionKey: string, signal?: AbortSignal): Promise<ArtifactDocumentWorkspace>
  listRevisions(documentId: string, sessionKey: string, signal?: AbortSignal): Promise<ArtifactRevision[]>
  listChangeSets(documentId: string, sessionKey: string, signal?: AbortSignal): Promise<ArtifactChangeSet[]>
  getChangeSet(documentId: string, changeSetId: string, sessionKey: string, signal?: AbortSignal): Promise<ArtifactChangeSet | null>
  openDocument(request: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<{ document: ArtifactDocument | null; editSession: ArtifactEditSession | null }>
  closeDocument(request: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<ArtifactDocument | null>
  renameDocument(request: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<ArtifactDocument | null>
  restoreRevision(request: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<ArtifactRevision | null>
  revertChangeSet(request: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<ArtifactChangeSet | null>
  readSource(request: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<ArtifactSourceSnapshot | null>
  patchSource(request: Readonly<Record<string, unknown>>, signal?: AbortSignal): Promise<ArtifactSourcePatchResult | null>
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

/** One semantic Workbench boundary; wire methods stay in its v4 Adapter. */
export interface ArtifactWorkbench {
  readonly artifacts: ArtifactCatalog
  readonly documents: ArtifactDocumentProvider
  readonly resources: WorkbenchResourceProvider
  readonly promptAnnotations: ArtifactPromptAnnotationProvider
  readonly content: ArtifactContentAccess
  ready(): Promise<void>
  subscribeDocumentChanges(
    listener: (change: ArtifactDocumentChange) => void,
  ): ArtifactWorkbenchSubscription
}

export const ARTIFACT_WORKBENCH_KEY: InjectionKey<ArtifactWorkbench> = Symbol('ArtifactWorkbench')
