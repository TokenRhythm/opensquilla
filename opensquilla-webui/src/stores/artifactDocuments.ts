import { defineStore } from 'pinia'
import { markRaw, ref, shallowRef } from 'vue'

import type {
  ArtifactDocumentActions,
  ArtifactDocumentWorkspace,
  ArtifactDocumentWorkspaceSnapshot,
} from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/rpc'
import { PendingMutationRequestIds } from '@/utils/mutationRequestIdentity'
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
  return error instanceof Error && error.message
    ? error.message
    : 'Artifact document metadata is unavailable.'
}

function unavailableAction(message: string): Error {
  return new Error(message)
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
    mutationRequestIds.clear()
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
            error: 'Artifact document metadata is temporarily unavailable.',
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

  async function refreshAfterMutation<T>(
    artifact: ArtifactPayload,
    sessionKey: string,
    mutation: () => Promise<T | null>,
  ): Promise<ArtifactDocumentWorkspace> {
    let mutationError: unknown = null
    let result: T | null = null
    try {
      result = await mutation()
      if (result === null) {
        mutationError = unavailableAction('Artifact document action was not accepted.')
      }
    } catch (error) {
      mutationError = error
    }

    // A request can commit on the server even if its response is interrupted.
    // Always force a canonical refetch before reporting the outcome.
    const workspace = await refresh(artifact, sessionKey)
    if (mutationError) throw mutationError
    return workspace
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
    const clientRequestId = mutationRequestIds.idFor(
      logicalRequestKey,
      'document-restore',
    )
    const result = await refreshAfterMutation(
      artifact,
      sessionKey,
      () => current.provider.restoreRevision({
        sessionKey,
        documentId: document.documentId,
        revisionId: revision.revisionId,
        expectedHeadRevisionId: document.headRevisionId,
        expectedStateRevision: document.stateRevision,
        clientRequestId,
      }),
    )
    mutationRequestIds.release(logicalRequestKey, clientRequestId)
    return result
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
    const clientRequestId = mutationRequestIds.idFor(
      logicalRequestKey,
      'document-revert',
    )
    const result = await refreshAfterMutation(
      artifact,
      sessionKey,
      () => current.provider.revertChangeSet({
        sessionKey,
        documentId: document.documentId,
        changeSetId: changeSet.changeSetId,
        expectedHeadRevisionId: document.headRevisionId,
        expectedStateRevision: document.stateRevision,
        clientRequestId,
      }),
    )
    mutationRequestIds.release(logicalRequestKey, clientRequestId)
    return result
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
