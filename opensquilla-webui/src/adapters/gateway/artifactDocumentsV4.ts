import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import type {
  ArtifactDocumentProvider,
  CloseArtifactDocument,
  ReadArtifactSource,
  RenameArtifactDocument,
  RestoreArtifactRevision,
  RevertArtifactChangeSet,
} from '@/modules/artifactWorkbench'

interface V4RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  supports(method: string): boolean
  markUnsupported(method: string): void
}
import type {
  ArtifactChangeSet,
  ArtifactChangeSetResponse,
  ArtifactChangeSetsListResponse,
  ArtifactDocument,
  ArtifactDocumentResponse,
  ArtifactDocumentsListResponse,
  ArtifactDocumentWorkspace,
  ArtifactEditCapabilities,
  ArtifactEditCapabilitiesResponse,
  ArtifactEditSession,
  ArtifactRevision,
  ArtifactRevisionsListResponse,
  ArtifactSourceSnapshot,
} from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/artifacts'
import {
  acceptsWorkbenchResult,
  artifactDocumentContracts,
} from './artifactWorkbenchContracts'
import { mapArtifactProductFailure } from './artifactErrorMapping'

export { isOfficeArtifact } from '@/utils/chat/artifacts'

export const ARTIFACT_DOCUMENT_RPC_METHODS = {
  capabilities: artifactDocumentContracts.capabilities.method,
  documentsList: artifactDocumentContracts.documentsList.method,
  documentsGet: artifactDocumentContracts.documentsGet.method,
  documentsOpen: artifactDocumentContracts.documentsOpen.method,
  documentsClose: artifactDocumentContracts.documentsClose.method,
  documentsRename: artifactDocumentContracts.documentsRename.method,
  revisionsList: artifactDocumentContracts.revisionsList.method,
  revisionsRestore: artifactDocumentContracts.revisionsRestore.method,
  changesList: artifactDocumentContracts.changesList.method,
  changesGet: artifactDocumentContracts.changesGet.method,
  changesRevert: artifactDocumentContracts.changesRevert.method,
  sourceRead: artifactDocumentContracts.sourceRead.method,
  sourcePatch: artifactDocumentContracts.sourcePatch.method,
  mutationResolve: artifactDocumentContracts.mutationResolve.method,
  editSessionStart: artifactDocumentContracts.editSessionStart.method,
  editSessionHeartbeat: artifactDocumentContracts.editSessionHeartbeat.method,
  editSessionClose: artifactDocumentContracts.editSessionClose.method,
  legacyGet: artifactDocumentContracts.legacyGet.method,
} as const

const ARTIFACT_DOCUMENT_CONTRACTS_BY_METHOD = new Map(
  Object.values(artifactDocumentContracts).map(contract => [contract.method, contract]),
)

type ArtifactDocumentRpc = {
  hasRpcMethod?: (method: string) => boolean
  rememberUnsupportedMethod?: (method: string) => void
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ) => Promise<T>
}

interface ArtifactsGetResponse {
  artifact?: ArtifactPayload | null
}

import {
  artifactPayloadFromRevision,
  createLegacyArtifactWorkspace,
  normalizeArtifactChangeSet,
  normalizeArtifactDocument,
  normalizeArtifactEditCapabilities,
  normalizeArtifactEditSession,
  normalizeArtifactRevision,
  numberAt,
  objectValue,
  stringAt,
  unavailableArtifactEditCapabilities,
  valueAt,
} from '@/workbench/artifactDocumentProvider'
function methodNotFound(error: unknown): boolean {
  const code = objectValue(error)?.code
  const message = error instanceof Error ? error.message : String(error)
  return code === 'METHOD_NOT_FOUND' || /method not found/i.test(message)
}

function signalOptions(signal?: AbortSignal): RpcCallOptions | undefined {
  return {
    timeoutMs: 10_000,
    timeoutAction: 'reject',
    abortAction: 'reject',
    ...(signal ? { signal } : {}),
  }
}

export function createRpcArtifactDocumentProvider(
  rpc: ArtifactDocumentRpc,
): ArtifactDocumentProvider {
  let cachedCapabilities: ArtifactEditCapabilities | null = null

  const supports = (method: string) => rpc.hasRpcMethod?.(method) !== false

  async function optionalCall<T>(
    method: string,
    params: Record<string, unknown> = {},
    signal?: AbortSignal,
  ): Promise<T | null> {
    if (!supports(method)) return null
    try {
      const result = await rpc.call<T>(method, params, signalOptions(signal))
      const contract = ARTIFACT_DOCUMENT_CONTRACTS_BY_METHOD.get(method)
      if (!contract || !acceptsWorkbenchResult(contract, result)) {
        throw new Error(`${method} returned an invalid response`)
      }
      return result
    } catch (error) {
      if (!methodNotFound(error)) throw mapArtifactProductFailure(error)
      rpc.rememberUnsupportedMethod?.(method)
      return null
    }
  }

  async function capabilities(signal?: AbortSignal): Promise<ArtifactEditCapabilities> {
    if (cachedCapabilities) return cachedCapabilities
    if (!supports(ARTIFACT_DOCUMENT_RPC_METHODS.capabilities)) {
      return unavailableArtifactEditCapabilities()
    }
    const response = await optionalCall<ArtifactEditCapabilitiesResponse>(
      ARTIFACT_DOCUMENT_RPC_METHODS.capabilities,
      {},
      signal,
    )
    const normalized = normalizeArtifactEditCapabilities(
      response?.capabilities ?? response,
    )
    if (response) cachedCapabilities = normalized
    return normalized
  }

  async function documents(sessionKey: string, signal?: AbortSignal) {
    const [response, editorCapabilities] = await Promise.all([
      optionalCall<ArtifactDocumentsListResponse>(
        ARTIFACT_DOCUMENT_RPC_METHODS.documentsList,
        { sessionKey },
        signal,
      ),
      capabilities(signal),
    ])
    return Array.isArray(response?.documents)
      ? response.documents
          .map(value => normalizeArtifactDocument(value, editorCapabilities, sessionKey))
          .filter((value): value is ArtifactDocument => value !== null)
      : []
  }

  async function document(
    documentId: string,
    sessionKey: string,
    signal?: AbortSignal,
  ) {
    const [response, editorCapabilities] = await Promise.all([
      optionalCall<ArtifactDocumentResponse>(
        ARTIFACT_DOCUMENT_RPC_METHODS.documentsGet,
        { documentId, sessionKey },
        signal,
      ),
      capabilities(signal),
    ])
    return normalizeArtifactDocument(response?.document, editorCapabilities, sessionKey)
  }

  async function revisions(
    documentId: string,
    sessionKey: string,
    signal?: AbortSignal,
  ) {
    const response = await optionalCall<ArtifactRevisionsListResponse>(
      ARTIFACT_DOCUMENT_RPC_METHODS.revisionsList,
      { documentId, sessionKey },
      signal,
    )
    return Array.isArray(response?.revisions)
      ? response.revisions
          .map(normalizeArtifactRevision)
          .filter((value): value is ArtifactRevision => value !== null)
      : []
  }

  async function changeSets(
    documentId: string,
    sessionKey: string,
    signal?: AbortSignal,
  ) {
    const response = await optionalCall<ArtifactChangeSetsListResponse>(
      ARTIFACT_DOCUMENT_RPC_METHODS.changesList,
      { documentId, sessionKey },
      signal,
    )
    return Array.isArray(response?.changeSets)
      ? response.changeSets
          .map(normalizeArtifactChangeSet)
          .filter((value): value is ArtifactChangeSet => value !== null)
      : []
  }

  async function refreshedLegacyArtifact(
    artifact: ArtifactPayload,
    sessionKey: string,
    signal?: AbortSignal,
  ): Promise<ArtifactPayload> {
    const artifactId = String(artifact.id || '').trim()
    if (!artifactId) return artifact
    try {
      const response = await optionalCall<ArtifactsGetResponse>(
        ARTIFACT_DOCUMENT_RPC_METHODS.legacyGet,
        { artifactId, sessionKey },
        signal,
      )
      return response?.artifact ? { ...artifact, ...response.artifact } : artifact
    } catch {
      return artifact
    }
  }

  async function resolveDocument(
    artifact: ArtifactPayload,
    sessionKey: string,
    signal?: AbortSignal,
  ): Promise<ArtifactDocument | null> {
    const explicitId = String(
      artifact.documentId || artifact.document_id || '',
    ).trim()
    if (explicitId) {
      const match = await document(explicitId, sessionKey, signal)
      if (match) return match
    }
    // Preview is deliberately read-only. An immutable delivery becomes an
    // editable Document only through the explicit documents.import action;
    // loading a preview must never adopt it or guess a Document by filename.
    return null
  }

  async function workspace(
    artifact: ArtifactPayload,
    sessionKey: string,
    signal?: AbortSignal,
  ): Promise<ArtifactDocumentWorkspace> {
    // Transient RPC failures must reach the store so it can retain a
    // last-known-good adopted workspace. Treat only an authoritative null as
    // absence; converting an exception to null would silently point downloads
    // back at the original immutable ArtifactRef.
    const resolved = await resolveDocument(artifact, sessionKey, signal)
    if (!resolved) {
      return createLegacyArtifactWorkspace(
        await refreshedLegacyArtifact(artifact, sessionKey, signal),
        sessionKey,
      )
    }

    const [revisionList, changeSetList] = await Promise.all([
      revisions(resolved.documentId, sessionKey, signal),
      changeSets(resolved.documentId, sessionKey, signal),
    ])
    const headRevision = revisionList.find(
      revision => revision.revisionId === resolved?.headRevisionId,
    )
    const headArtifact = {
      ...(headRevision
        ? artifactPayloadFromRevision(headRevision)
        : {
            ...artifact,
            id: resolved.headRevisionId,
            name: resolved.name || artifact.name,
          }),
      // Downloads must dereference the mutable document head at request time,
      // even between a successful save and the asynchronous metadata refresh.
      download_url: resolved.latestDownloadUrl,
      // Keep the stable mutable-document identity on the derived head payload.
      // The immutable artifact id changes on every commit and must not become
      // the workspace/cache key during the release-triggered preview rebuild.
      documentId: resolved.documentId,
      document_id: resolved.documentId,
    }
    return {
      document: resolved,
      revisions: revisionList,
      changeSets: changeSetList,
      headArtifact,
      source: 'document-api',
    }
  }

  async function responseDocument(
    method: string,
    request: CloseArtifactDocument | RenameArtifactDocument,
    signal?: AbortSignal,
  ) {
    const response = await optionalCall<Record<string, unknown>>(
      method,
      { ...request },
      signal,
    )
    return normalizeArtifactDocument(
      response?.document,
      await capabilities(signal),
      typeof request.sessionKey === 'string' ? request.sessionKey : '',
    )
  }

  async function responseRevision(
    method: string,
    request: RestoreArtifactRevision,
    signal?: AbortSignal,
  ) {
    const response = await optionalCall<Record<string, unknown>>(
      method,
      { ...request },
      signal,
    )
    return normalizeArtifactRevision(response?.revision)
  }

  async function responseChangeSet(
    method: string,
    request: RevertArtifactChangeSet,
    signal?: AbortSignal,
  ) {
    const response = await optionalCall<ArtifactChangeSetResponse>(
      method,
      { ...request },
      signal,
    )
    return normalizeArtifactChangeSet(response?.changeSet)
  }

  return {
    getCapabilities: capabilities,
    listDocuments: documents,
    getDocument: document,
    loadWorkspace: workspace,
    listRevisions: revisions,
    listChangeSets: changeSets,
    async getChangeSet(documentId, changeSetId, sessionKey, signal) {
      const response = await optionalCall<ArtifactChangeSetResponse>(
        ARTIFACT_DOCUMENT_RPC_METHODS.changesGet,
        { documentId, changeSetId, sessionKey },
        signal,
      )
      return normalizeArtifactChangeSet(response?.changeSet)
    },
    async openDocument(request, signal) {
      const response = await optionalCall<Record<string, unknown>>(
        ARTIFACT_DOCUMENT_RPC_METHODS.documentsOpen,
        { ...request },
        signal,
      )
      return {
        document: normalizeArtifactDocument(
          response?.document,
          await capabilities(signal),
          typeof request.sessionKey === 'string' ? request.sessionKey : '',
        ),
        editSession: normalizeArtifactEditSession(response?.editSession),
      }
    },
    closeDocument: (request, signal) => responseDocument(
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsClose,
      request,
      signal,
    ),
    renameDocument: (request, signal) => responseDocument(
      ARTIFACT_DOCUMENT_RPC_METHODS.documentsRename,
      request,
      signal,
    ),
    restoreRevision: (request, signal) => responseRevision(
      ARTIFACT_DOCUMENT_RPC_METHODS.revisionsRestore,
      request,
      signal,
    ),
    revertChangeSet: (request, signal) => responseChangeSet(
      ARTIFACT_DOCUMENT_RPC_METHODS.changesRevert,
      request,
      signal,
    ),
    async readSource(request, signal) {
      return sourceResponse(ARTIFACT_DOCUMENT_RPC_METHODS.sourceRead, request, signal)
    },
    async patchSource(request, signal) {
      const response = await optionalCall<Record<string, unknown>>(
        ARTIFACT_DOCUMENT_RPC_METHODS.sourcePatch,
        { ...request },
        signal,
      )
      const source = normalizeSourceSnapshot(response?.source)
      if (!source) return null
      return {
        ...source,
        editSession: normalizeArtifactEditSession(response?.editSession),
      }
    },
    async resolveMutation(request, signal) {
      const response = await optionalCall<Record<string, unknown>>(
        ARTIFACT_DOCUMENT_RPC_METHODS.mutationResolve,
        { ...request },
        signal,
      )
      if (!response) return null
      const status = response.status
      if (status !== 'applied' && status !== 'not_applied' && status !== 'pending') {
        throw new Error('Invalid page update resolution response')
      }
      const rawResult = objectValue(response.result)
      const document = normalizeArtifactDocument(response.document, undefined, request.sessionKey)
      const rawDocument = objectValue(response.document)
      const revision = normalizeArtifactRevision(rawDocument?.head)
      const documentId = rawResult ? stringAt(rawResult, 'documentId') : ''
      const revisionId = rawResult ? stringAt(rawResult, 'revisionId') : ''
      const sha256 = rawResult ? stringAt(rawResult, 'sha256') : ''
      const stateRevision = rawResult
        ? Math.max(1, numberAt(rawResult, 1, 'stateRevision'))
        : 1
      const rawRetryAfterMs = response.retryAfterMs
      return {
        status,
        retryAfterMs: rawRetryAfterMs !== null
          && rawRetryAfterMs !== undefined
          && Number.isFinite(Number(rawRetryAfterMs))
          ? Math.max(0, Number(rawRetryAfterMs))
          : null,
        result: documentId && revisionId && /^[0-9a-f]{64}$/.test(sha256)
          ? { documentId, revisionId, sha256, stateRevision }
          : null,
        ...(document ? { document } : {}),
        ...(revision ? { revision } : {}),
      }
    },
    async startEditSession(request, signal) {
      return editSessionResponse(
        ARTIFACT_DOCUMENT_RPC_METHODS.editSessionStart,
        { ...request },
        signal,
      )
    },
    async heartbeatEditSession(request, signal) {
      return editSessionResponse(
        ARTIFACT_DOCUMENT_RPC_METHODS.editSessionHeartbeat,
        { ...request },
        signal,
      )
    },
    async closeEditSession(request, signal) {
      return editSessionResponse(
        ARTIFACT_DOCUMENT_RPC_METHODS.editSessionClose,
        { ...request },
        signal,
      )
    },
  }

  async function editSessionResponse(
    method: string,
    request: Readonly<Record<string, unknown>>,
    signal?: AbortSignal,
  ): Promise<ArtifactEditSession | null> {
    const response = await optionalCall<Record<string, unknown>>(
      method,
      { ...request },
      signal,
    )
    if (!response) return null
    const editSession = normalizeArtifactEditSession(response.editSession)
    if (!editSession) throw new Error('Invalid artifact EditSession response')
    return editSession
  }

  async function sourceResponse(
    method: string,
    request: ReadArtifactSource,
    signal?: AbortSignal,
  ): Promise<ArtifactSourceSnapshot | null> {
    const response = await optionalCall<Record<string, unknown>>(
      method,
      { ...request },
      signal,
    )
    return normalizeSourceSnapshot(response?.source)
  }

  function normalizeSourceSnapshot(value: unknown): ArtifactSourceSnapshot | null {
    const raw = objectValue(value)
    if (!raw) return null
    const documentId = stringAt(raw, 'documentId', 'document_id')
    const revisionId = stringAt(raw, 'revisionId', 'revision_id')
    if (!documentId || !revisionId) return null
    return {
      documentId,
      revisionId,
      language: stringAt(raw, 'language'),
      content: stringAt(raw, 'content', 'source', 'text'),
      sha256: stringAt(raw, 'sha256'),
      // Older gateways predate the advertised field but already used Python
      // code-point indexes. Unknown future encodings fail closed.
      offsetEncoding: stringAt(raw, 'offsetEncoding', 'offset_encoding') === ''
        || stringAt(raw, 'offsetEncoding', 'offset_encoding') === 'unicode-code-point'
        ? 'unicode-code-point'
        : (() => { throw new Error('Unsupported artifact source offset encoding') })(),
      patchCount: valueAt(raw, 'patchCount', 'patch_count') == null
        ? null
        : Math.max(0, numberAt(raw, 0, 'patchCount', 'patch_count')),
      stateRevision: Math.max(1, numberAt(raw, 1, 'stateRevision', 'state_revision')),
    }
  }
}

export function createV4ArtifactDocuments(
  transport: V4RpcTransport,
): ArtifactDocumentProvider {
  return createRpcArtifactDocumentProvider({
    call: <T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions) => (
      transport.request<T>(method, params, options)
    ),
    hasRpcMethod: method => transport.supports(method),
    rememberUnsupportedMethod: method => transport.markUnsupported(method),
  })
}
