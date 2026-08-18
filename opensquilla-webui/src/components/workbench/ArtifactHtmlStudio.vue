<template>
  <section class="artifact-html-studio" :aria-label="t('workbench.artifactDocument.source')">
    <header class="artifact-html-studio__toolbar">
      <span class="artifact-html-studio__selection" :title="selectedElement?.label || ''">
        <Icon name="fileCode" :size="14" aria-hidden="true" />
        <span>{{ selectedElement?.label || t('workbench.artifactDocument.noElementSelected') }}</span>
      </span>
      <span class="artifact-html-studio__status" :data-state="status">
        {{ statusLabel }}
      </span>
      <button
        type="button"
        class="btn btn--primary artifact-html-studio__action"
        :disabled="!dirty || !editingReady || loading || saving"
        @click="flush"
      >
        <Icon name="save" :size="14" aria-hidden="true" />
        <span>{{ saving
          ? t('workbench.artifactDocument.saving')
          : t('workbench.artifactDocument.saveSource') }}</span>
      </button>
    </header>
    <div v-if="error" class="artifact-html-studio__error" role="alert">
      <Icon name="info" :size="14" aria-hidden="true" />
      <span>{{ error }}</span>
      <span class="artifact-html-studio__error-actions">
        <button
          v-if="headConflict && dirty"
          type="button"
          class="btn btn--ghost"
          data-testid="copy-unsaved-source"
          @click="copyUnsavedSource"
        >
          {{ copyState === 'copied'
            ? t('workbench.artifactDocument.unsavedSourceCopied')
            : t('workbench.artifactDocument.copyUnsavedSource') }}
        </button>
        <button
          v-if="headConflict"
          type="button"
          class="btn btn--ghost"
          data-testid="discard-and-load-latest"
          :disabled="loading || saving"
          @click="discardAndLoadLatest"
        >
          {{ t('workbench.artifactDocument.discardAndLoadLatest') }}
        </button>
        <button v-else type="button" class="btn btn--ghost" @click="retry">
          {{ t('workbench.resources.retry') }}
        </button>
      </span>
    </div>
    <div ref="editorElement" class="artifact-html-studio__editor" />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type * as Monaco from 'monaco-editor'
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import HtmlWorker from 'monaco-editor/language/html/html.worker.js?worker'

import Icon from '@/components/Icon.vue'
import { useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import type {
  ArtifactDocument,
  ArtifactEditSession,
  ArtifactMutationResolution,
  ArtifactSourceSnapshot,
  ArtifactSourcePatchResult,
} from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/rpc'
import type { WorkbenchBeforeCloseOptions } from '@/workbench/types'
import { copyTextWithFallback } from '@/utils/browser'
import {
  artifactMutationOutcomeMayBePending,
  artifactProductClientError,
  classifyArtifactProductError,
} from '@/utils/artifactProductErrors'
import {
  createMutationClientRequestId,
  PendingMutationRequestIds,
} from '@/utils/mutationRequestIdentity'
import { resolveArtifactMutationBounded } from '@/workbench/artifactMutationRecovery'
import {
  htmlElementAtOffsets,
  htmlSourceElements,
  minimalSourcePatch,
  SOURCE_OFFSET_ENCODING,
  type HtmlSourceElement,
} from '@/workbench/htmlSourceModel'

type MonacoEnvironmentShape = {
  MonacoEnvironment?: {
    getWorker(moduleId: string, label: string): Worker
  }
}

const monacoGlobal = globalThis as unknown as MonacoEnvironmentShape
monacoGlobal.MonacoEnvironment ||= {
  getWorker(_moduleId: string, label: string) {
    return label === 'html' || label === 'handlebars' || label === 'razor'
      ? new HtmlWorker()
      : new EditorWorker()
  },
}

const props = defineProps<{
  artifact: ArtifactPayload
  document: ArtifactDocument
  sessionKey: string
}>()

const emit = defineEmits<{
  'source-saved': [revisionId: string]
}>()

const { t } = useI18n()
const artifactDocuments = useArtifactDocumentsStore()
const editorElement = ref<HTMLElement | null>(null)
const snapshot = ref<ArtifactSourceSnapshot | null>(null)
const elements = ref<HtmlSourceElement[]>([])
const selectedElement = ref<HtmlSourceElement | null>(null)
const loading = ref(false)
const saving = ref(false)
const dirty = ref(false)
const error = ref('')
const savedAt = ref(0)
const editSession = ref<ArtifactEditSession | null>(null)
const canonicalHeadRevisionId = ref(props.document.headRevisionId)
const headConflict = ref(false)
const copyState = ref<'idle' | 'copied' | 'failed'>('idle')
const editSessionMode = ref<
  'starting' | 'healthy' | 'degraded' | 'reacquiring' | 'legacy' | 'closed'
>(
  'starting',
)
let editor: Monaco.editor.IStandaloneCodeEditor | null = null
let modelSubscription: Monaco.IDisposable | null = null
let cursorSubscription: Monaco.IDisposable | null = null
let autosaveTimer: ReturnType<typeof setTimeout> | null = null
let parseTimer: ReturnType<typeof setTimeout> | null = null
let suppressChanges = false
let unmounted = false
let editVersion = 0
let loadGeneration = 0
let flushPromise: Promise<boolean> | null = null
let closePromise: Promise<boolean> | null = null
let startPromise: Promise<boolean> | null = null
let heartbeatTimer: ReturnType<typeof setTimeout> | null = null
let reacquireTimer: ReturnType<typeof setTimeout> | null = null
let sessionMutationQueue: Promise<void> = Promise.resolve()
let deferredHeadRevisionId = ''
let cleanHeadReloadPromise: Promise<boolean> | null = null
let reacquirePromise: Promise<boolean> | null = null
let autosaveBlocked = false

type PendingSourceMutation = {
  logicalKey: string
  requestId: string
  request: Readonly<Record<string, unknown>>
  baseline: ArtifactSourceSnapshot
  content: string
}

let pendingSourceMutation: PendingSourceMutation | null = null

const EDIT_SESSION_HEARTBEAT_MS = 20_000
const pendingSourceRequestIds = new PendingMutationRequestIds(4)
let editSessionClientRequestId = createMutationClientRequestId('edit-session')

const editingReady = computed(() => (
  !headConflict.value
  && editSessionMode.value !== 'starting'
  && editSessionMode.value !== 'closed'
))

const status = computed(() => error.value
  ? 'error'
  : saving.value
    ? 'saving'
    : dirty.value
      ? 'dirty'
      : savedAt.value
        ? 'saved'
        : 'ready')
const statusLabel = computed(() => t(`workbench.artifactDocument.sourceStatus.${status.value}`))

function currentSource(): string {
  return editor?.getValue() || ''
}

function updateElementIndex() {
  if (!editor) return
  try {
    elements.value = htmlSourceElements(editor.getValue())
    updateSelectedElement()
  } catch {
    elements.value = []
    selectedElement.value = null
  }
}

function updateSelectedElement() {
  const selection = editor?.getSelection()
  const model = editor?.getModel()
  if (!selection || !model) {
    selectedElement.value = null
    return
  }
  const start = model.getOffsetAt(selection.getStartPosition())
  const end = model.getOffsetAt(selection.getEndPosition())
  selectedElement.value = htmlElementAtOffsets(elements.value, start, end)
}

function scheduleParse() {
  if (parseTimer) clearTimeout(parseTimer)
  parseTimer = setTimeout(updateElementIndex, 180)
}

function scheduleAutosave() {
  clearAutosave()
  if (
    headConflict.value
    || !editingReady.value
    || pendingSourceMutation !== null
    || autosaveBlocked
    || unmounted
  ) return
  autosaveTimer = setTimeout(() => void flush(), 1_200)
}

function clearAutosave() {
  if (autosaveTimer) clearTimeout(autosaveTimer)
  autosaveTimer = null
}

function rpcError(errorValue: unknown): string {
  const classified = classifyArtifactProductError(errorValue)
  const translated = t(classified.messageKey)
  return translated === classified.messageKey ? classified.fallbackMessage : translated
}

function runSessionMutation<T>(operation: () => Promise<T>): Promise<T> {
  const pending = sessionMutationQueue.then(operation, operation)
  sessionMutationQueue = pending.then(() => undefined, () => undefined)
  return pending
}

function stopHeartbeat() {
  if (heartbeatTimer) clearTimeout(heartbeatTimer)
  heartbeatTimer = null
}

function stopReacquire() {
  if (reacquireTimer) clearTimeout(reacquireTimer)
  reacquireTimer = null
}

function blockEditing(reason: unknown) {
  stopHeartbeat()
  clearAutosave()
  stopReacquire()
  editSessionMode.value = 'degraded'
  error.value = rpcError(reason)
  editor?.updateOptions({ readOnly: true })
}

function degradeEditSession(reason: unknown) {
  stopHeartbeat()
  editSessionMode.value = 'degraded'
  error.value = rpcError(reason)
  editor?.updateOptions({ readOnly: snapshot.value === null || headConflict.value })
}

function enterHeadConflict(reason: unknown) {
  headConflict.value = true
  copyState.value = 'idle'
  blockEditing(reason)
}

function reconcileDeferredHeadRevision() {
  if (!deferredHeadRevisionId || saving.value || flushPromise) return
  const observedHeadRevisionId = deferredHeadRevisionId
  deferredHeadRevisionId = ''
  if (snapshot.value?.revisionId !== observedHeadRevisionId) {
    if (dirty.value) {
      enterHeadConflict(artifactProductClientError('DOCUMENT_CHANGED'))
    } else {
      void reloadCleanHead()
    }
  }
}

function editorTracksHead(headRevisionId: string): boolean {
  return snapshot.value?.revisionId === headRevisionId
    && (
      editSessionMode.value !== 'healthy'
      || editSession.value?.lastSavedRevisionId === headRevisionId
    )
}

async function runCleanHeadReload(): Promise<boolean> {
  while (
    !unmounted
    && !dirty.value
    && !saving.value
    && !flushPromise
    && !headConflict.value
  ) {
    const requestedHeadRevisionId = canonicalHeadRevisionId.value
    if (editorTracksHead(requestedHeadRevisionId)) return true

    clearAutosave()
    loadGeneration += 1
    editor?.updateOptions({ readOnly: true })
    await closeEditSessionBestEffort()
    if (
      unmounted
      || dirty.value
      || saving.value
      || flushPromise
      || headConflict.value
    ) return false

    editSession.value = null
    editSessionMode.value = 'starting'
    editSessionClientRequestId = createMutationClientRequestId('edit-session')
    error.value = ''
    const loaded = await initializeEditor()
    if (unmounted || dirty.value || headConflict.value) return false
    if (loaded && editorTracksHead(canonicalHeadRevisionId.value)) return true
    if (error.value || canonicalHeadRevisionId.value === requestedHeadRevisionId) return false
    // A second head arrived while the source RPC was in flight. Repeat against
    // that newest immutable head instead of publishing an intermediate buffer.
  }
  return false
}

function reloadCleanHead(): Promise<boolean> {
  if (cleanHeadReloadPromise) return cleanHeadReloadPromise
  const pending = runCleanHeadReload()
  cleanHeadReloadPromise = pending
  const clear = () => {
    if (cleanHeadReloadPromise === pending) cleanHeadReloadPromise = null
  }
  void pending.then(clear, clear)
  return pending
}

function assertActiveEditSession(
  candidate: ArtifactEditSession | null,
  expectedId?: string,
): ArtifactEditSession {
  if (
    !candidate
    || candidate.documentId !== props.document.documentId
    || candidate.mode !== 'edit'
    || candidate.status !== 'active'
    || (Boolean(expectedId) && candidate.editSessionId !== expectedId)
    || !candidate.lastSavedRevisionId
  ) {
    throw artifactProductClientError('EDIT_SESSION_RENEWAL_REQUIRED')
  }
  return candidate
}

function scheduleHeartbeat() {
  stopHeartbeat()
  if (editSessionMode.value !== 'healthy' || unmounted) return
  heartbeatTimer = setTimeout(onHeartbeatTimer, EDIT_SESSION_HEARTBEAT_MS)
}

function scheduleReacquire(delayMs = 0) {
  stopReacquire()
  if (unmounted || headConflict.value || editSessionMode.value === 'legacy') return
  reacquireTimer = setTimeout(() => {
    reacquireTimer = null
    void reacquireEditing()
  }, delayMs)
}

function onHeartbeatTimer() {
  void heartbeatEditSession()
}

async function heartbeatEditSession() {
  if (editSessionMode.value !== 'healthy' || unmounted) return
  const provider = artifactDocuments.provider
  if (!provider?.heartbeatEditSession) {
    degradeEditSession(artifactProductClientError('EDIT_SESSION_RENEWAL_REQUIRED'))
    scheduleReacquire()
    return
  }
  try {
    await runSessionMutation(async () => {
      const current = assertActiveEditSession(editSession.value)
      const refreshed = await provider.heartbeatEditSession!({
        sessionKey: props.sessionKey,
        editSessionId: current.editSessionId,
        expectedStateRevision: current.stateRevision,
      })
      const updated = assertActiveEditSession(refreshed, current.editSessionId)
      if (
        updated.stateRevision <= current.stateRevision
        || updated.lastSavedRevisionId !== current.lastSavedRevisionId
      ) {
        throw artifactProductClientError('DOCUMENT_CHANGED')
      }
      editSession.value = updated
    })
    scheduleHeartbeat()
  } catch {
    degradeEditSession(artifactProductClientError('EDIT_SESSION_RENEWAL_REQUIRED'))
    scheduleReacquire()
  }
}

async function runReacquireEditing(): Promise<boolean> {
  const provider = artifactDocuments.provider
  if (!provider || unmounted || headConflict.value) return false
  if (!provider.startEditSession) {
    editSession.value = null
    editSessionMode.value = 'legacy'
    error.value = ''
    editor?.updateOptions({ readOnly: snapshot.value === null })
    return true
  }
  const expectedRevisionId = snapshot.value?.revisionId || canonicalHeadRevisionId.value
  editSessionMode.value = 'reacquiring'
  editor?.updateOptions({ readOnly: snapshot.value === null })
  try {
    const requestId = createMutationClientRequestId('edit-session')
    const started = await runSessionMutation(() => provider.startEditSession!({
      sessionKey: props.sessionKey,
      documentId: props.document.documentId,
      mode: 'edit',
      clientRequestId: requestId,
    }))
    if (!started) {
      editSession.value = null
      editSessionMode.value = 'legacy'
      error.value = ''
      editor?.updateOptions({ readOnly: snapshot.value === null })
      return true
    }
    const active = assertActiveEditSession(started)
    if (active.lastSavedRevisionId !== expectedRevisionId) {
      if (dirty.value) {
        enterHeadConflict(artifactProductClientError('DOCUMENT_CHANGED'))
      } else {
        canonicalHeadRevisionId.value = active.lastSavedRevisionId
        deferredHeadRevisionId = ''
        editSession.value = active
        editSessionMode.value = 'healthy'
        error.value = ''
        editor?.updateOptions({ readOnly: true })
        scheduleHeartbeat()
        void artifactDocuments.refresh(props.artifact, props.sessionKey)
        return loadSource(active.lastSavedRevisionId)
      }
      return false
    }
    editSession.value = active
    editSessionMode.value = 'healthy'
    error.value = ''
    editor?.updateOptions({ readOnly: snapshot.value === null })
    scheduleHeartbeat()
    if (snapshot.value === null) return loadSource()
    if (dirty.value) scheduleAutosave()
    return true
  } catch {
    degradeEditSession(artifactProductClientError('EDIT_SESSION_RENEWAL_REQUIRED'))
    scheduleReacquire(1_000)
    return false
  }
}

function reacquireEditing(): Promise<boolean> {
  if (editSessionMode.value === 'legacy') return Promise.resolve(true)
  if (editSessionMode.value === 'healthy') return Promise.resolve(true)
  if (reacquirePromise) return reacquirePromise
  const pending = runReacquireEditing()
  reacquirePromise = pending
  const clear = () => {
    if (reacquirePromise === pending) reacquirePromise = null
  }
  void pending.then(clear, clear)
  return pending
}

async function startEditing(): Promise<boolean> {
  const provider = artifactDocuments.provider
  if (!provider || !props.document.capabilities.source || unmounted) return false
  editSessionMode.value = 'starting'
  editSession.value = null
  deferredHeadRevisionId = ''
  headConflict.value = false
  error.value = ''
  editor?.updateOptions({ readOnly: true })
  try {
    if (!provider.startEditSession) {
      editSessionMode.value = 'legacy'
      return true
    }
    const started = await runSessionMutation(() => provider.startEditSession!({
      sessionKey: props.sessionKey,
      documentId: props.document.documentId,
      mode: 'edit',
      clientRequestId: editSessionClientRequestId,
    }))
    // METHOD_NOT_FOUND is the only provider path that returns null. It is an
    // explicit compatibility mode and must never be represented as a session.
    if (!started) {
      editSessionMode.value = 'legacy'
      return true
    }
    const active = assertActiveEditSession(started)
    if (active.lastSavedRevisionId !== canonicalHeadRevisionId.value) {
      throw artifactProductClientError('DOCUMENT_CHANGED')
    }
    editSession.value = active
    editSessionMode.value = 'healthy'
    scheduleHeartbeat()
    return true
  } catch (caught) {
    degradeEditSession(caught)
    scheduleReacquire()
    return false
  }
}

async function initializeEditor(): Promise<boolean> {
  const starting = startEditing()
  startPromise = starting
  let started = false
  try {
    started = await starting
  } finally {
    if (startPromise === starting) startPromise = null
  }
  if (!started) return false
  return loadSource()
}

async function retry() {
  if (editSessionMode.value === 'starting' || editSessionMode.value === 'reacquiring') return
  if (editSessionMode.value === 'degraded' && !snapshot.value && !dirty.value) {
    await initializeEditor()
    return
  }
  if (editSessionMode.value === 'degraded') {
    if (!await reacquireEditing()) return
    if (snapshot.value) return
  }
  await loadSource()
}

async function loadSource(
  revisionId: string = canonicalHeadRevisionId.value,
): Promise<boolean> {
  const provider = artifactDocuments.provider
  if (
    !provider
    || !props.document.capabilities.source
    || !editingReady.value
    || headConflict.value
    || unmounted
  ) return false
  if (dirty.value || saving.value || flushPromise) {
    error.value = rpcError(artifactProductClientError('DOCUMENT_CHANGED'))
    return false
  }
  const generation = ++loadGeneration
  const requestedDocumentId = props.document.documentId
  const requestedRevisionId = revisionId
  const requestedSessionKey = props.sessionKey
  const startingEditVersion = editVersion
  loading.value = true
  error.value = ''
  editor?.updateOptions({ readOnly: true })
  try {
    const loaded = await provider.readSource({
      sessionKey: requestedSessionKey,
      documentId: requestedDocumentId,
      revisionId: requestedRevisionId,
    })
    if (!loaded) throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
    if (
      generation !== loadGeneration
      || unmounted
      || props.sessionKey !== requestedSessionKey
      || props.document.documentId !== requestedDocumentId
      || canonicalHeadRevisionId.value !== requestedRevisionId
    ) {
      return false
    }
    if (
      loaded.documentId !== requestedDocumentId
      || loaded.revisionId !== requestedRevisionId
    ) {
      throw artifactProductClientError('DOCUMENT_CHANGED')
    }
    // Monaco is read-only while loading, but the edit generation is still a
    // final guard against programmatic edits and late responses.
    if (dirty.value || editVersion !== startingEditVersion) {
      error.value = rpcError(artifactProductClientError('DOCUMENT_CHANGED'))
      return false
    }
    snapshot.value = loaded
    suppressChanges = true
    editor?.setValue(loaded.content)
    suppressChanges = false
    editVersion += 1
    dirty.value = false
    updateElementIndex()
    return true
  } catch (caught) {
    suppressChanges = false
    if (generation === loadGeneration && !unmounted) {
      error.value = rpcError(caught)
    }
    return false
  } finally {
    if (generation === loadGeneration && !unmounted) {
      loading.value = false
      editor?.updateOptions({ readOnly: snapshot.value === null || !editingReady.value })
    }
  }
}

function sourceFromResolution(
  pending: PendingSourceMutation,
  resolution: ArtifactMutationResolution,
): ArtifactSourcePatchResult | null {
  const result = resolution.result
  if (
    resolution.status !== 'applied'
    || !result
    || result.documentId !== props.document.documentId
  ) return null
  return {
    documentId: result.documentId,
    revisionId: result.revisionId,
    language: pending.baseline.language,
    content: '',
    sha256: result.sha256,
    offsetEncoding: SOURCE_OFFSET_ENCODING,
    patchCount: 1,
    stateRevision: result.stateRevision,
    editSession: null,
  }
}

function beginSilentEditSessionRecovery() {
  if (editSessionMode.value === 'legacy') return
  stopHeartbeat()
  editSessionMode.value = 'degraded'
  error.value = ''
  editor?.updateOptions({ readOnly: snapshot.value === null || headConflict.value })
  scheduleReacquire()
}

function acceptSavedSource(
  pending: PendingSourceMutation,
  saved: ArtifactSourcePatchResult,
  options: { responseConfirmed: boolean },
): boolean {
  const { responseConfirmed } = options
  if (
    saved.documentId !== props.document.documentId
    || !saved.revisionId
    || !saved.sha256
  ) {
    error.value = rpcError(artifactProductClientError('DOCUMENT_UNAVAILABLE'))
    autosaveBlocked = true
    return false
  }
  if (responseConfirmed && editSessionMode.value === 'healthy' && editSession.value) {
    const current = editSession.value
    const updated = assertActiveEditSession(saved.editSession, current.editSessionId)
    if (
      updated.lastSavedRevisionId !== saved.revisionId
      || updated.stateRevision <= current.stateRevision
    ) {
      enterHeadConflict(artifactProductClientError('DOCUMENT_CHANGED'))
      return false
    }
    editSession.value = updated
  } else if (!responseConfirmed) {
    beginSilentEditSessionRecovery()
  }

  pendingSourceRequestIds.release(pending.logicalKey, pending.requestId)
  pendingSourceMutation = null
  snapshot.value = { ...saved, content: pending.content }
  dirty.value = currentSource() !== pending.content
  autosaveBlocked = false
  savedAt.value = Date.now()
  updateElementIndex()
  const observedHeadRevisionId = deferredHeadRevisionId || canonicalHeadRevisionId.value
  deferredHeadRevisionId = ''
  canonicalHeadRevisionId.value = saved.revisionId
  if (
    observedHeadRevisionId !== pending.baseline.revisionId
    && observedHeadRevisionId !== saved.revisionId
  ) {
    if (dirty.value) {
      enterHeadConflict(artifactProductClientError('DOCUMENT_CHANGED'))
      return false
    }
    deferredHeadRevisionId = observedHeadRevisionId
  }
  emit('source-saved', saved.revisionId)
  return true
}

async function resolvePendingSourceMutation(
  options: { allowLegacyReplay: boolean },
): Promise<'applied' | 'not_applied' | 'pending'> {
  const provider = artifactDocuments.provider
  const pending = pendingSourceMutation
  if (!provider || !pending) return 'not_applied'

  let resolution: ArtifactMutationResolution | null = null
  if (provider.resolveMutation) {
    try {
      resolution = await resolveArtifactMutationBounded(
        async request => {
          try {
            return await provider.resolveMutation!(request)
          } catch (caught) {
            if (!artifactMutationOutcomeMayBePending(caught)) throw caught
            return { status: 'pending', retryAfterMs: null, result: null }
          }
        },
        {
          sessionKey: props.sessionKey,
          operation: 'source.patch',
          requestId: pending.requestId,
          documentId: props.document.documentId,
        },
      )
    } catch {
      resolution = { status: 'pending', retryAfterMs: null, result: null }
    }
  }

  if (resolution?.status === 'applied') {
    const saved = sourceFromResolution(pending, resolution)
    if (!saved) {
      error.value = rpcError(artifactProductClientError('MUTATION_OUTCOME_PENDING'))
      autosaveBlocked = true
      return 'pending'
    }
    return acceptSavedSource(pending, saved, { responseConfirmed: false })
      ? 'applied'
      : 'pending'
  }
  if (resolution?.status === 'not_applied') {
    pendingSourceRequestIds.markNotApplied(pending.logicalKey, pending.requestId)
    pendingSourceMutation = null
    error.value = rpcError(artifactProductClientError('MUTATION_NOT_APPLIED'))
    autosaveBlocked = true
    return 'not_applied'
  }
  if (resolution?.status === 'pending') {
    error.value = rpcError(artifactProductClientError('MUTATION_OUTCOME_PENDING'))
    autosaveBlocked = true
    return 'pending'
  }

  if (!options.allowLegacyReplay) {
    error.value = rpcError(artifactProductClientError('MUTATION_OUTCOME_PENDING'))
    autosaveBlocked = true
    return 'pending'
  }

  // On an explicit retry against an older Gateway, replay the exact frozen
  // request. No field or request identity may be recomputed here.
  try {
    const replay = () => provider.patchSource(pending.request)
    const saved = pending.request.editSessionId
      ? await runSessionMutation(replay)
      : await replay()
    if (!saved) throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
    return acceptSavedSource(pending, saved, { responseConfirmed: true })
      ? 'applied'
      : 'pending'
  } catch {
    error.value = rpcError(artifactProductClientError('MUTATION_OUTCOME_PENDING'))
    autosaveBlocked = true
    return 'pending'
  }
}

async function commitCurrentSnapshot(): Promise<boolean> {
  clearAutosave()
  const provider = artifactDocuments.provider
  if (!provider) return false
  saving.value = true
  try {
    if (headConflict.value) {
      error.value = rpcError(artifactProductClientError('DOCUMENT_CHANGED'))
      editor?.updateOptions({ readOnly: true })
      return false
    }
    error.value = ''
    if (pendingSourceMutation) {
      const outcome = await resolvePendingSourceMutation({ allowLegacyReplay: true })
      if (outcome === 'pending') return false
      if (outcome === 'applied') return !dirty.value
    }

    if (
      (editSessionMode.value === 'degraded' || editSessionMode.value === 'reacquiring')
      && !await reacquireEditing()
    ) return false

    const baseline = snapshot.value
    const content = currentSource()
    if (!baseline || !dirty.value) return !dirty.value
    if (!editingReady.value || headConflict.value) {
      error.value = rpcError(artifactProductClientError('DOCUMENT_CHANGED'))
      editor?.updateOptions({ readOnly: true })
      return false
    }
    const patch = minimalSourcePatch(baseline.content, content)
    if (!patch) {
      dirty.value = false
      autosaveBlocked = false
      return true
    }
    const logicalSaveKey = JSON.stringify([
      props.sessionKey,
      props.document.documentId,
      baseline.revisionId,
      baseline.stateRevision,
      baseline.sha256,
      SOURCE_OFFSET_ENCODING,
      patch.startOffset,
      patch.endOffset,
      patch.replacement,
    ])
    const requestId = pendingSourceRequestIds.idFor(logicalSaveKey, 'document-save')
    const request: Record<string, unknown> = {
      sessionKey: props.sessionKey,
      documentId: props.document.documentId,
      expectedHeadRevisionId: baseline.revisionId,
      expectedStateRevision: baseline.stateRevision,
      expectedSourceSha256: baseline.sha256,
      offsetEncoding: SOURCE_OFFSET_ENCODING,
      patches: [patch],
      clientRequestId: requestId,
    }
    const requiresEditSession = editSessionMode.value !== 'legacy'
    const currentSession = requiresEditSession
      ? assertActiveEditSession(editSession.value)
      : null
    if (currentSession) {
      if (currentSession.lastSavedRevisionId !== baseline.revisionId) {
        enterHeadConflict(artifactProductClientError('DOCUMENT_CHANGED'))
        return false
      }
      request.editSessionId = currentSession.editSessionId
      request.expectedEditSessionStateRevision = currentSession.stateRevision
      request.expectedLastSavedRevisionId = currentSession.lastSavedRevisionId
    }
    const frozenRequest = pendingSourceRequestIds.freeze(
      logicalSaveKey,
      requestId,
      request,
    )
    const pending: PendingSourceMutation = {
      logicalKey: logicalSaveKey,
      requestId,
      request: frozenRequest,
      baseline,
      content,
    }
    pendingSourceMutation = pending

    try {
      const save = () => provider.patchSource(frozenRequest)
      const saved = requiresEditSession ? await runSessionMutation(save) : await save()
      if (!saved) throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
      return acceptSavedSource(pending, saved, { responseConfirmed: true })
    } catch (caught) {
      if (artifactMutationOutcomeMayBePending(caught)) {
        pendingSourceRequestIds.markPending(logicalSaveKey, requestId)
        const outcome = await resolvePendingSourceMutation({ allowLegacyReplay: false })
        return outcome === 'applied' && !dirty.value
      }
      pendingSourceRequestIds.markNotApplied(logicalSaveKey, requestId)
      pendingSourceMutation = null
      autosaveBlocked = true
      const classified = classifyArtifactProductError(caught)
      if (classified.code === 'DOCUMENT_CHANGED') {
        enterHeadConflict(caught)
      } else if (classified.code === 'EDIT_SESSION_RENEWAL_REQUIRED') {
        degradeEditSession(caught)
        scheduleReacquire()
      } else {
        error.value = rpcError(caught)
      }
      return false
    }
  } finally {
    saving.value = false
    reconcileDeferredHeadRevision()
  }
}

async function flush(): Promise<boolean> {
  if (flushPromise) return flushPromise
  const pending = commitCurrentSnapshot()
  flushPromise = pending
  try {
    return await pending
  } finally {
    if (flushPromise === pending) flushPromise = null
    reconcileDeferredHeadRevision()
    if (dirty.value && editingReady.value && !headConflict.value && !unmounted) {
      scheduleAutosave()
    }
  }
}

async function copyUnsavedSource() {
  copyState.value = 'idle'
  try {
    await copyTextWithFallback(currentSource())
    copyState.value = 'copied'
  } catch {
    copyState.value = 'failed'
    error.value = t('workbench.artifactDocument.copyUnsavedSourceFailed')
  }
}

async function discardAndLoadLatest(): Promise<boolean> {
  if (!headConflict.value || loading.value || saving.value || flushPromise) return false
  clearAutosave()
  if (parseTimer) clearTimeout(parseTimer)
  parseTimer = null
  loadGeneration += 1
  editor?.updateOptions({ readOnly: true })

  // Discard is explicit: only this action may replace the local buffer after
  // a head conflict. Never rebase or autosave the stale source implicitly.
  dirty.value = false
  snapshot.value = null
  selectedElement.value = null
  elements.value = []
  await closeEditSessionBestEffort()

  editSession.value = null
  editSessionMode.value = 'starting'
  editSessionClientRequestId = createMutationClientRequestId('edit-session')
  autosaveBlocked = false
  copyState.value = 'idle'
  headConflict.value = false
  error.value = ''
  const loaded = await initializeEditor()
  if (!loaded && !error.value) {
    blockEditing(artifactProductClientError('DOCUMENT_UNAVAILABLE'))
  }
  return loaded
}

async function drainPendingEdits(): Promise<boolean> {
  // A flush already in flight may leave a newer generation dirty. Drain until
  // the editor and immutable head converge, or stop on the first failed save.
  while (dirty.value || flushPromise) {
    if (!await flush()) return false
  }
  return true
}

async function closeEditSessionBestEffort() {
  stopHeartbeat()
  stopReacquire()
  if (startPromise) await startPromise
  if (editSession.value?.status !== 'active') return
  const provider = artifactDocuments.provider
  if (!provider?.closeEditSession) {
    editSessionMode.value = 'closed'
    return
  }
  try {
    await runSessionMutation(async () => {
      const current = assertActiveEditSession(editSession.value)
      const closed = await provider.closeEditSession!({
        sessionKey: props.sessionKey,
        editSessionId: current.editSessionId,
        expectedStateRevision: current.stateRevision,
      })
      if (
        !closed
        || closed.editSessionId !== current.editSessionId
        || closed.documentId !== props.document.documentId
        || closed.status !== 'closed'
      ) {
        throw artifactProductClientError('EDIT_SESSION_RENEWAL_REQUIRED')
      }
      editSession.value = closed
      editSessionMode.value = 'closed'
    })
  } catch {
    // Closing is best effort after the source is durable. The server TTL still
    // releases an unreachable session, while stale/expired sessions never get
    // another write from this editor instance.
    editSessionMode.value = 'closed'
    if (!unmounted && dirty.value) {
      error.value = rpcError(artifactProductClientError('EDIT_SESSION_RENEWAL_REQUIRED'))
    }
  }
}

async function beforeClose(options: WorkbenchBeforeCloseOptions = {}): Promise<boolean> {
  if (options.preserveRuntime) return drainPendingEdits()
  if (closePromise) return closePromise
  const closing = (async () => {
    if (!await drainPendingEdits()) return false
    await closeEditSessionBestEffort()
    return true
  })()
  closePromise = closing
  const accepted = await closing
  if (!accepted && closePromise === closing) closePromise = null
  return accepted
}

onMounted(async () => {
  if (!editorElement.value) return
  let monaco: typeof Monaco
  try {
    monaco = await import('monaco-editor')
  } catch (caught) {
    error.value = rpcError(caught)
    return
  }
  if (unmounted || !editorElement.value) return
  editor = monaco.editor.create(editorElement.value, {
    value: '',
    language: 'html',
    theme: 'vs-dark',
    automaticLayout: true,
    minimap: { enabled: false },
    fontSize: 13,
    lineNumbersMinChars: 3,
    padding: { top: 10, bottom: 10 },
    scrollBeyondLastLine: false,
    tabSize: 2,
    readOnly: true,
  })
  modelSubscription = editor.onDidChangeModelContent(() => {
    if (suppressChanges) return
    editVersion += 1
    autosaveBlocked = false
    dirty.value = snapshot.value?.content !== editor?.getValue()
    scheduleParse()
    if (dirty.value) scheduleAutosave()
  })
  cursorSubscription = editor.onDidChangeCursorSelection(updateSelectedElement)
  void initializeEditor()
})

watch(
  () => props.document.headRevisionId,
  headRevisionId => {
    canonicalHeadRevisionId.value = headRevisionId
    if (snapshot.value?.revisionId === headRevisionId) return
    // The Gateway may publish the state invalidation immediately before the
    // source.patch response. Defer judgment until that response identifies
    // the revision accepted for this exact in-flight save.
    if (saving.value || flushPromise) {
      deferredHeadRevisionId = headRevisionId
      return
    }
    if (dirty.value) {
      enterHeadConflict(artifactProductClientError('DOCUMENT_CHANGED'))
      return
    }
    void reloadCleanHead()
  },
)

onBeforeUnmount(() => {
  stopHeartbeat()
  stopReacquire()
  clearAutosave()
  if (parseTimer) clearTimeout(parseTimer)
  parseTimer = null
  // The normal workbench close path awaits beforeClose. This also covers host
  // teardown/crashes where Vue cannot await an unmount hook: capture the
  // editor buffer synchronously, then flush and release in the background.
  void beforeClose()
  unmounted = true
  loadGeneration += 1
  modelSubscription?.dispose()
  cursorSubscription?.dispose()
  editor?.dispose()
  editor = null
})

defineExpose({ beforeClose, discardAndLoadLatest, flush, reload: loadSource })
</script>

<style scoped>
.artifact-html-studio {
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  background: var(--bg);
}

.artifact-html-studio__toolbar {
  display: flex;
  min-height: 42px;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}

.artifact-html-studio__selection {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}

.artifact-html-studio__selection span {
  overflow: hidden;
  max-width: 220px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-html-studio__status {
  margin-inline-start: auto;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
}

.artifact-html-studio__status[data-state='dirty'],
.artifact-html-studio__status[data-state='saving'] {
  color: var(--warn);
}

.artifact-html-studio__status[data-state='error'] {
  color: var(--danger);
}

.artifact-html-studio__action {
  display: inline-flex;
  min-height: 29px;
  align-items: center;
  gap: 5px;
  padding: 0 9px;
  font-size: 11px;
}

.artifact-html-studio__error {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  background: color-mix(in srgb, var(--danger) 10%, var(--bg));
  color: var(--danger);
  font-size: 11px;
}

.artifact-html-studio__error span {
  flex: 1;
}

.artifact-html-studio__error-actions {
  display: inline-flex;
  flex: 0 0 auto !important;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
}

.artifact-html-studio__editor {
  flex: 1;
  min-width: 0;
  min-height: 180px;
}

@media (max-width: 680px) {
  .artifact-html-studio__action span,
  .artifact-html-studio__status {
    display: none;
  }
}
</style>
