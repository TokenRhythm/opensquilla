import { defineStore } from 'pinia'
import { markRaw, ref, shallowRef } from 'vue'

import type {
  ArtifactDocumentActions,
  ArtifactDocumentWorkspace,
  ArtifactDocumentWorkspaceSnapshot,
} from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/rpc'
import {
  artifactMutationOutcomeMayBePending,
  artifactProductClientError,
  classifyArtifactProductError,
} from '@/utils/artifactProductErrors'
import { PendingMutationRequestIds } from '@/utils/mutationRequestIdentity'
import { resolveArtifactMutationBounded } from '@/workbench/artifactMutationRecovery'
import {
  createLegacyArtifactWorkspace,
  type ArtifactDocumentProvider,
} from '@/workbench/artifactDocumentProvider'

function artifactIdentity(artifact: ArtifactPayload): string {
  return String(
    artifact.documentId
      || artifact.document_id
      || artifact.id
      || artifact.key
      || artifact.download_url
      || artifact.name
      || 'artifact',
  )
}

export function artifactDocumentWorkspaceKey(
  artifact: ArtifactPayload,
  sessionKey: string,
): string {
  return `${sessionKey}\u0000${artifactIdentity(artifact)}`
}

function emptySnapshot(key: string): ArtifactDocumentWorkspaceSnapshot {
  return {
    key,
    loading: false,
    loaded: false,
    stale: false,
    error: null,
    workspace: null,
  }
}

function errorMessage(error: unknown): string {
  return classifyArtifactProductError(error).fallbackMessage
}

function unavailableAction(message: string): Error {
  void message
  return artifactProductClientError('DOCUMENT_UNAVAILABLE')
}

export const useArtifactDocumentsStore = defineStore('artifactDocuments', () => {
  const snapshots = ref<Record<string, ArtifactDocumentWorkspaceSnapshot>>({})
  const provider = shallowRef<ArtifactDocumentProvider | null>(null)
  const requests = new Map<string, AbortController>()
  const generations = new Map<string, number>()
  const mutationRequestIds = new PendingMutationRequestIds(64)

  function setProvider(next: ArtifactDocumentProvider | null) {
    if (provider.value === next) return
    abortAll()
    provider.value = next ? markRaw(next) : null
  }

  function snapshot(
    artifact: ArtifactPayload,
    sessionKey: string,
  ): ArtifactDocumentWorkspaceSnapshot {
    const key = artifactDocumentWorkspaceKey(artifact, sessionKey)
    return snapshots.value[key] || emptySnapshot(key)
  }

  function setSnapshot(key: string, value: ArtifactDocumentWorkspaceSnapshot) {
    snapshots.value = { ...snapshots.value, [key]: value }
  }

  function retireRequest(key: string): number {
    requests.get(key)?.abort()
    requests.delete(key)
    const generation = (generations.get(key) || 0) + 1
    generations.set(key, generation)
    return generation
  }

  async function load(
    artifact: ArtifactPayload,
    sessionKey: string,
    options: { force?: boolean } = {},
  ): Promise<ArtifactDocumentWorkspace> {
    const key = artifactDocumentWorkspaceKey(artifact, sessionKey)
    const current = snapshots.value[key]
    if (!options.force && current?.loaded && current.workspace) return current.workspace

    const generation = retireRequest(key)
    const controller = new AbortController()
    requests.set(key, controller)
    setSnapshot(key, {
      key,
      loading: true,
      loaded: current?.loaded ?? false,
      stale: current?.stale ?? false,
      error: null,
      workspace: current?.workspace ?? null,
    })

    try {
      const workspace = provider.value
        ? await provider.value.loadWorkspace(artifact, sessionKey, controller.signal)
        : createLegacyArtifactWorkspace(artifact, sessionKey)
      if (generations.get(key) === generation) {
        // Adoption is monotonic for the lifetime of a workbench snapshot. A
        // temporarily unavailable document RPC must never replace the stable
        // document head with the original immutable ArtifactRef.
        if (
          current?.workspace?.source === 'document-api'
          && workspace.source === 'legacy-artifact'
        ) {
          setSnapshot(key, {
            key,
            loading: false,
            loaded: true,
            stale: true,
            error: errorMessage(artifactProductClientError('DOCUMENT_UNAVAILABLE')),
            workspace: current.workspace,
          })
          return current.workspace
        }
        setSnapshot(key, {
          key,
          loading: false,
          loaded: true,
          stale: false,
          error: null,
          workspace,
        })
      }
      return workspace
    } catch (error) {
      if (controller.signal.aborted) throw error
      // Preserve the last-known-good head on refresh failures. Constructing a
      // legacy workspace is safe only before this artifact has ever loaded.
      const workspace = current?.workspace
        ?? createLegacyArtifactWorkspace(artifact, sessionKey)
      if (generations.get(key) === generation) {
        setSnapshot(key, {
          key,
          loading: false,
          loaded: true,
          stale: true,
          error: errorMessage(error),
          workspace,
        })
      }
      return workspace
    } finally {
      if (requests.get(key) === controller) requests.delete(key)
    }
  }

  function refresh(
    artifact: ArtifactPayload,
    sessionKey: string,
  ): Promise<ArtifactDocumentWorkspace> {
    return load(artifact, sessionKey, { force: true })
  }

  function headArtifact(
    artifact: ArtifactPayload,
    sessionKey: string,
  ): ArtifactPayload {
    return snapshot(artifact, sessionKey).workspace?.headArtifact || artifact
  }

  function mutableWorkspace(
    artifact: ArtifactPayload,
    sessionKey: string,
  ): { provider: ArtifactDocumentProvider; workspace: ArtifactDocumentWorkspace } {
    const currentProvider = provider.value
    const workspace = snapshot(artifact, sessionKey).workspace
    if (!currentProvider || !workspace || workspace.source !== 'document-api') {
      throw unavailableAction('Artifact document actions are unavailable.')
    }
    return { provider: currentProvider, workspace }
  }

  async function runDocumentMutation<T>(
    artifact: ArtifactPayload,
    sessionKey: string,
    options: {
      currentProvider: ArtifactDocumentProvider
      documentId: string
      operation: 'revision.restore' | 'change.revert'
      logicalKey: string
      requestPrefix: string
      buildPayload: (requestId: string) => Readonly<Record<string, unknown>>
      execute: (payload: Readonly<Record<string, unknown>>) => Promise<T | null>
    },
  ): Promise<ArtifactDocumentWorkspace> {
    const clientRequestId = mutationRequestIds.idFor(
      options.logicalKey,
      options.requestPrefix,
    )
    const wasPending = mutationRequestIds.isPending(options.logicalKey, clientRequestId)
    const payload = mutationRequestIds.pendingPayload(options.logicalKey, clientRequestId)
      || mutationRequestIds.freeze(
        options.logicalKey,
        clientRequestId,
        options.buildPayload(clientRequestId),
      )
    const release = () => mutationRequestIds.release(options.logicalKey, clientRequestId)
    const pendingError = () => artifactProductClientError('MUTATION_OUTCOME_PENDING')
    const notApplied = async (): Promise<never> => {
      release()
      await refresh(artifact, sessionKey)
      throw artifactProductClientError('MUTATION_NOT_APPLIED')
    }
    const resolve = async () => {
      if (!options.currentProvider.resolveMutation) return null
      try {
        return await resolveArtifactMutationBounded(
          request => options.currentProvider.resolveMutation!(request),
          {
            sessionKey,
            operation: options.operation,
            requestId: clientRequestId,
            documentId: options.documentId,
          },
        )
      } catch {
        throw pendingError()
      }
    }

    if (wasPending && options.currentProvider.resolveMutation) {
      const resolution = await resolve()
      if (resolution?.status === 'not_applied') return notApplied()
      if (resolution?.status === 'pending') throw pendingError()
      if (resolution?.status === 'applied') {
        release()
        return refresh(artifact, sessionKey)
      }
      // Null means an old Gateway. Continue with an explicit exact replay.
    }

    try {
      const result = await options.execute(payload)
      if (result === null) {
        release()
        await refresh(artifact, sessionKey)
        throw unavailableAction('Artifact document action was not accepted.')
      }
      release()
      return refresh(artifact, sessionKey)
    } catch (error) {
      if (!artifactMutationOutcomeMayBePending(error)) {
        release()
        await refresh(artifact, sessionKey)
        throw error
      }
      mutationRequestIds.markPending(options.logicalKey, clientRequestId)
      if (!wasPending && options.currentProvider.resolveMutation) {
        const resolution = await resolve()
        if (resolution?.status === 'not_applied') return notApplied()
        if (resolution?.status === 'applied') {
          release()
          return refresh(artifact, sessionKey)
        }
      }
      await refresh(artifact, sessionKey)
      throw pendingError()
    }
  }

  const restoreRevision: ArtifactDocumentActions['restoreRevision'] = async (
    artifact,
    sessionKey,
    revisionId,
  ) => {
    const current = mutableWorkspace(artifact, sessionKey)
    const document = current.workspace.document
    const revision = current.workspace.revisions.find(item => item.revisionId === revisionId)
    if (!document.capabilities.revisions || !revision || revision.documentId !== document.documentId) {
      throw unavailableAction('Artifact revision restore is unavailable.')
    }
    if (revision.revisionId === document.headRevisionId) return current.workspace
    const logicalRequestKey = JSON.stringify([
      'restore',
      sessionKey,
      document.documentId,
      revision.revisionId,
      document.headRevisionId,
      document.stateRevision,
    ])
    return runDocumentMutation(
      artifact,
      sessionKey,
      {
        currentProvider: current.provider,
        documentId: document.documentId,
        operation: 'revision.restore',
        logicalKey: logicalRequestKey,
        requestPrefix: 'document-restore',
        buildPayload: clientRequestId => ({
          sessionKey,
          documentId: document.documentId,
          revisionId: revision.revisionId,
          expectedHeadRevisionId: document.headRevisionId,
          expectedStateRevision: document.stateRevision,
          clientRequestId,
        }),
        execute: payload => current.provider.restoreRevision(payload),
      },
    )
  }

  const revertChangeSet: ArtifactDocumentActions['revertChangeSet'] = async (
    artifact,
    sessionKey,
    changeSetId,
  ) => {
    const current = mutableWorkspace(artifact, sessionKey)
    const document = current.workspace.document
    const changeSet = current.workspace.changeSets.find(
      item => item.changeSetId === changeSetId,
    )
    if (
      !document.capabilities.changeSets
      || !changeSet
      || changeSet.documentId !== document.documentId
      || changeSet.status !== 'applied'
      || changeSet.appliedRevisionId !== document.headRevisionId
    ) {
      throw unavailableAction('Artifact change-set revert is unavailable.')
    }
    const logicalRequestKey = JSON.stringify([
      'revert',
      sessionKey,
      document.documentId,
      changeSet.changeSetId,
      document.headRevisionId,
      document.stateRevision,
    ])
    return runDocumentMutation(
      artifact,
      sessionKey,
      {
        currentProvider: current.provider,
        documentId: document.documentId,
        operation: 'change.revert',
        logicalKey: logicalRequestKey,
        requestPrefix: 'document-revert',
        buildPayload: clientRequestId => ({
          sessionKey,
          documentId: document.documentId,
          changeSetId: changeSet.changeSetId,
          expectedHeadRevisionId: document.headRevisionId,
          expectedStateRevision: document.stateRevision,
          clientRequestId,
        }),
        execute: payload => current.provider.revertChangeSet(payload),
      },
    )
  }

  function clearSession(sessionKey: string) {
    const prefix = `${sessionKey}\u0000`
    const next: Record<string, ArtifactDocumentWorkspaceSnapshot> = {}
    for (const [key, value] of Object.entries(snapshots.value)) {
      if (key.startsWith(prefix)) {
        retireRequest(key)
      } else {
        next[key] = value
      }
    }
    snapshots.value = next
  }

  function abortAll() {
    for (const key of [...requests.keys()]) retireRequest(key)
  }

  function reset() {
    abortAll()
    mutationRequestIds.clear()
    snapshots.value = {}
    generations.clear()
  }

  return {
    snapshots,
    provider,
    setProvider,
    snapshot,
    load,
    refresh,
    headArtifact,
    restoreRevision,
    revertChangeSet,
    clearSession,
    reset,
  }
})
