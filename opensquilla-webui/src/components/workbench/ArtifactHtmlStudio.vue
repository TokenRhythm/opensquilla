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
          {{ t('common.retry') }}
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
  ArtifactSourceSnapshot,
} from '@/types/artifactDocuments'
import type { ArtifactPayload } from '@/types/rpc'
import { copyTextWithFallback } from '@/utils/browser'
import {
  createMutationClientRequestId,
  PendingMutationRequestIds,
} from '@/utils/mutationRequestIdentity'
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
const headConflict = ref(false)
const copyState = ref<'idle' | 'copied' | 'failed'>('idle')
const editSessionMode = ref<'initializing' | 'active' | 'legacy' | 'failed' | 'closed'>(
  'initializing',
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
let sessionMutationQueue: Promise<void> = Promise.resolve()

const EDIT_SESSION_HEARTBEAT_MS = 20_000
const pendingSourceRequestIds = new PendingMutationRequestIds(4)
let editSessionClientRequestId = createMutationClientRequestId('edit-session')

const editingReady = computed(() => (
  editSessionMode.value === 'active' || editSessionMode.value === 'legacy'
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
  if (headConflict.value || !editingReady.value || unmounted) return
  autosaveTimer = setTimeout(() => void flush(), 1_200)
}

function clearAutosave() {
  if (autosaveTimer) clearTimeout(autosaveTimer)
  autosaveTimer = null
}

function rpcError(errorValue: unknown): string {
  return errorValue instanceof Error && errorValue.message
    ? errorValue.message
    : t('workbench.artifactDocument.sourceUnavailable')
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

function failEditSession(reason: unknown) {
  stopHeartbeat()
  clearAutosave()
  editSessionMode.value = 'failed'
  error.value = rpcError(reason)
  editor?.updateOptions({ readOnly: true })
}

function enterHeadConflict(reason: unknown) {
  headConflict.value = true
  copyState.value = 'idle'
  failEditSession(reason)
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
    throw new Error(t('workbench.artifactDocument.sourceConflict'))
  }
  return candidate
}

function scheduleHeartbeat() {
  stopHeartbeat()
  if (editSessionMode.value !== 'active' || unmounted) return
  heartbeatTimer = setTimeout(onHeartbeatTimer, EDIT_SESSION_HEARTBEAT_MS)
}

function onHeartbeatTimer() {
  void heartbeatEditSession()
}

async function heartbeatEditSession() {
  if (editSessionMode.value !== 'active' || unmounted) return
  const provider = artifactDocuments.provider
  if (!provider?.heartbeatEditSession) {
    failEditSession(new Error(t('workbench.artifactDocument.sourceConflict')))
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
        throw new Error(t('workbench.artifactDocument.sourceConflict'))
      }
      editSession.value = updated
    })
    scheduleHeartbeat()
  } catch (caught) {
    failEditSession(caught)
  }
}

async function startEditing(): Promise<boolean> {
  const provider = artifactDocuments.provider
  if (!provider || !props.document.capabilities.source || unmounted) return false
  editSessionMode.value = 'initializing'
  editSession.value = null
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
    if (active.lastSavedRevisionId !== props.document.headRevisionId) {
      throw new Error(t('workbench.artifactDocument.sourceConflict'))
    }
    editSession.value = active
    editSessionMode.value = 'active'
    scheduleHeartbeat()
    return true
  } catch (caught) {
    failEditSession(caught)
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
  if (editSessionMode.value === 'initializing') return
  if (editSessionMode.value === 'failed' && !snapshot.value && !dirty.value) {
    await initializeEditor()
    return
  }
  await loadSource()
}

async function loadSource(): Promise<boolean> {
  const provider = artifactDocuments.provider
  if (
    !provider
    || !props.document.capabilities.source
    || !editingReady.value
    || headConflict.value
    || unmounted
  ) return false
  if (dirty.value || saving.value || flushPromise) {
    error.value = t('workbench.artifactDocument.sourceConflict')
    return false
  }
  const generation = ++loadGeneration
  const requestedDocumentId = props.document.documentId
  const requestedRevisionId = props.document.headRevisionId
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
    if (!loaded) throw new Error(t('workbench.artifactDocument.sourceUnavailable'))
    if (
      generation !== loadGeneration
      || unmounted
      || props.sessionKey !== requestedSessionKey
      || props.document.documentId !== requestedDocumentId
      || props.document.headRevisionId !== requestedRevisionId
    ) {
      return false
    }
    if (
      loaded.documentId !== requestedDocumentId
      || loaded.revisionId !== requestedRevisionId
    ) {
      throw new Error(t('workbench.artifactDocument.sourceConflict'))
    }
    // Monaco is read-only while loading, but the edit generation is still a
    // final guard against programmatic edits and late responses.
    if (dirty.value || editVersion !== startingEditVersion) {
      error.value = t('workbench.artifactDocument.sourceConflict')
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

async function commitCurrentSnapshot(): Promise<boolean> {
  clearAutosave()
  const provider = artifactDocuments.provider
  const baseline = snapshot.value
  const content = currentSource()
  const saveVersion = editVersion
  if (!provider || !baseline || !dirty.value) return !dirty.value
  if (!editingReady.value || headConflict.value) {
    error.value = t('workbench.artifactDocument.sourceConflict')
    editor?.updateOptions({ readOnly: true })
    return false
  }
  const patch = minimalSourcePatch(baseline.content, content)
  if (!patch) {
    dirty.value = false
    return true
  }
  // The replacement is used only in this component's bounded in-memory key.
  // The RPC receives a random opaque ID, so source text and paths cannot leak
  // through clientRequestId while an exact response-loss retry stays stable.
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
  const saveClientRequestId = pendingSourceRequestIds.idFor(
    logicalSaveKey,
    'document-save',
  )
  saving.value = true
  error.value = ''
  try {
    const requiresEditSession = editSessionMode.value === 'active'
    const save = async () => {
      const request: Record<string, unknown> = {
        sessionKey: props.sessionKey,
        documentId: props.document.documentId,
        expectedHeadRevisionId: baseline.revisionId,
        expectedStateRevision: baseline.stateRevision,
        expectedSourceSha256: baseline.sha256,
        offsetEncoding: SOURCE_OFFSET_ENCODING,
        patches: [patch],
        clientRequestId: saveClientRequestId,
      }
      if (requiresEditSession && editSessionMode.value !== 'active') {
        throw new Error(t('workbench.artifactDocument.sourceConflict'))
      }
      const currentSession = requiresEditSession
        ? assertActiveEditSession(editSession.value)
        : null
      if (currentSession) {
        if (currentSession.lastSavedRevisionId !== baseline.revisionId) {
          throw new Error(t('workbench.artifactDocument.sourceConflict'))
        }
        request.editSessionId = currentSession.editSessionId
        request.expectedEditSessionStateRevision = currentSession.stateRevision
        request.expectedLastSavedRevisionId = currentSession.lastSavedRevisionId
      }
      const result = await provider.patchSource(request)
      if (!result) throw new Error(t('workbench.artifactDocument.sourceUnavailable'))
      if (currentSession) {
        const updated = assertActiveEditSession(
          result.editSession,
          currentSession.editSessionId,
        )
        if (
          updated.lastSavedRevisionId !== result.revisionId
          || updated.stateRevision <= currentSession.stateRevision
        ) {
          throw new Error(t('workbench.artifactDocument.sourceConflict'))
        }
        editSession.value = updated
      }
      pendingSourceRequestIds.release(logicalSaveKey, saveClientRequestId)
      return result
    }
    const saved = requiresEditSession
      ? await runSessionMutation(save)
      : await save()
    if (!saved) throw new Error(t('workbench.artifactDocument.sourceUnavailable'))
    snapshot.value = { ...saved, content }
    // The response advances only the immutable buffer captured above. An edit
    // made while the request was in flight remains a new dirty generation.
    dirty.value = editVersion !== saveVersion
    savedAt.value = Date.now()
    updateElementIndex()
    emit('source-saved', saved.revisionId)
    void artifactDocuments.refresh(props.artifact, props.sessionKey)
    return true
  } catch (caught) {
    if (editSessionMode.value === 'active') failEditSession(caught)
    else if (!error.value) error.value = rpcError(caught)
    return false
  } finally {
    saving.value = false
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
  editSessionMode.value = 'initializing'
  editSessionClientRequestId = createMutationClientRequestId('edit-session')
  copyState.value = 'idle'
  headConflict.value = false
  error.value = ''
  const loaded = await initializeEditor()
  if (!loaded && !error.value) {
    failEditSession(new Error(t('workbench.artifactDocument.sourceUnavailable')))
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
  if (startPromise) await startPromise
  if (editSessionMode.value !== 'active' && editSession.value?.status !== 'active') return
  const provider = artifactDocuments.provider
  if (!provider?.closeEditSession) {
    editSessionMode.value = 'failed'
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
        throw new Error(t('workbench.artifactDocument.sourceConflict'))
      }
      editSession.value = closed
      editSessionMode.value = 'closed'
    })
  } catch (caught) {
    // Closing is best effort after the source is durable. The server TTL still
    // releases an unreachable session, while stale/expired sessions never get
    // another write from this editor instance.
    editSessionMode.value = 'failed'
    if (!unmounted) error.value = rpcError(caught)
  }
}

async function beforeClose(): Promise<boolean> {
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
    if (snapshot.value?.revisionId === headRevisionId) return
    if (
      editSessionMode.value === 'active'
      && editSession.value?.lastSavedRevisionId !== headRevisionId
    ) {
      enterHeadConflict(new Error(t('workbench.artifactDocument.sourceConflict')))
      return
    }
    if (dirty.value) {
      enterHeadConflict(new Error(t('workbench.artifactDocument.sourceConflict')))
      return
    }
    void loadSource()
  },
)

onBeforeUnmount(() => {
  stopHeartbeat()
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
