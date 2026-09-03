import type { TransportCallOptions as RpcCallOptions } from './transportTypes'
import type { ArtifactPromptAnnotationProvider } from '@/modules/artifactWorkbench'
import {
  acceptsWorkbenchResult,
  promptAnnotationContracts,
} from './artifactWorkbenchContracts'
import { mapArtifactProductFailure } from './artifactErrorMapping'

interface V4RpcTransport {
  request<T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions): Promise<T>
  supports(method: string): boolean
  markUnsupported(method: string): void
}
import type {
  PromptAnnotation,
  PromptAnnotationFocusResponse,
  PromptAnnotationResponse,
  PromptAnnotationsListResponse,
} from '@/types/promptAnnotations'

export const PROMPT_ANNOTATION_RPC_METHODS = {
  create: promptAnnotationContracts.create.method,
  list: promptAnnotationContracts.list.method,
  update: promptAnnotationContracts.update.method,
  discard: promptAnnotationContracts.discard.method,
  focus: promptAnnotationContracts.focus.method,
} as const

const PROMPT_ANNOTATION_CONTRACTS_BY_METHOD = new Map(
  Object.values(promptAnnotationContracts).map(contract => [contract.method, contract]),
)

type PromptAnnotationRpc = {
  hasRpcMethod?: (method: string) => boolean
  rememberUnsupportedMethod?: (method: string) => void
  call: <T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options?: RpcCallOptions,
  ) => Promise<T>
}

import {
  normalizePromptAnnotation,
  objectValue,
} from '@/workbench/artifactPromptAnnotationProvider'
function methodNotFound(error: unknown): boolean {
  const raw = objectValue(error)
  const message = error instanceof Error ? error.message : String(error)
  return raw?.code === 'METHOD_NOT_FOUND' || /method not found/i.test(message)
}

function signalOptions(signal?: AbortSignal): RpcCallOptions {
  return {
    timeoutMs: 10_000,
    timeoutAction: 'reject',
    abortAction: 'reject',
    ...(signal ? { signal } : {}),
  }
}

export function createRpcArtifactPromptAnnotationProvider(
  rpc: PromptAnnotationRpc,
): ArtifactPromptAnnotationProvider {
  async function call<T>(
    method: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<T | null> {
    if (rpc.hasRpcMethod?.(method) === false) return null
    try {
      const result = await rpc.call<T>(method, params, signalOptions(signal))
      const contract = PROMPT_ANNOTATION_CONTRACTS_BY_METHOD.get(method)
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

  return {
    async list(sessionKey, signal) {
      const response = await call<PromptAnnotationsListResponse>(
        PROMPT_ANNOTATION_RPC_METHODS.list,
        { sessionKey, status: 'draft' },
        signal,
      )
      return Array.isArray(response?.annotations)
        ? response.annotations
            .map(value => normalizePromptAnnotation(value, { sessionKey }))
            .filter((item): item is PromptAnnotation => item !== null)
        : []
    },
    async create(request) {
      const response = await call<PromptAnnotationResponse>(PROMPT_ANNOTATION_RPC_METHODS.create, {
        annotationId: request.annotationId,
        sessionKey: request.sessionKey,
        documentId: request.documentId,
        revisionId: request.revisionId,
        selection: {
          selectionId: request.selection.selectionId,
          tagName: request.selection.tagName,
          elementPath: request.selection.elementPath,
          elementProofSha256: request.selection.elementProofSha256,
          ...(request.selection.domSha256
            ? { domSha256: request.selection.domSha256 }
            : {}),
        },
        ...(request.body !== undefined ? { body: request.body } : {}),
      })
      return normalizePromptAnnotation(response?.annotation, { sessionKey: request.sessionKey })
    },
    async update(request) {
      const response = await call<PromptAnnotationResponse>(PROMPT_ANNOTATION_RPC_METHODS.update, {
        annotationId: request.annotationId,
        sessionKey: request.sessionKey,
        body: request.body,
        expectedStateRevision: request.expectedStateRevision,
      })
      return normalizePromptAnnotation(response?.annotation, { sessionKey: request.sessionKey })
    },
    async discard(request) {
      const response = await call<PromptAnnotationResponse>(PROMPT_ANNOTATION_RPC_METHODS.discard, {
        annotationId: request.annotationId,
        sessionKey: request.sessionKey,
        expectedStateRevision: request.expectedStateRevision,
      })
      return normalizePromptAnnotation(response?.annotation, { sessionKey: request.sessionKey })
    },
    async focus(request) {
      const response = await call<PromptAnnotationFocusResponse>(
        PROMPT_ANNOTATION_RPC_METHODS.focus,
        {
          sessionKey: request.sessionKey,
          annotationId: request.annotationId,
        },
      )
      const annotationId = typeof response?.annotationId === 'string'
        ? response.annotationId.trim()
        : ''
      const documentId = typeof response?.documentId === 'string'
        ? response.documentId.trim()
        : ''
      return response?.focused === true
        && annotationId === request.annotationId
        && Boolean(documentId)
        ? { focused: true, annotationId, documentId }
        : null
    },
  }
}

export function createV4ArtifactPromptAnnotations(
  transport: V4RpcTransport,
): ArtifactPromptAnnotationProvider {
  return createRpcArtifactPromptAnnotationProvider({
    call: <T = unknown>(method: string, params?: Record<string, unknown>, options?: RpcCallOptions) => (
      transport.request<T>(method, params, options)
    ),
    hasRpcMethod: method => transport.supports(method),
    rememberUnsupportedMethod: method => transport.markUnsupported(method),
  })
}
