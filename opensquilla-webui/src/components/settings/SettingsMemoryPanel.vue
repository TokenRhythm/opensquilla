<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import { copyTextWithFallback } from '@/utils/browser'
import { useRpcStore } from '@/stores/rpc'

const INFO_METHOD = 'memory.import.info'
const START_METHOD = 'memory.import.start'
const STATUS_METHOD = 'memory.import.status'
const CANCEL_METHOD = 'memory.import.cancel'
const RETRY_METHOD = 'memory.import.retry'
const APPLY_METHOD = 'memory.import.apply'
const UNDO_METHOD = 'memory.import.undo'
const DISCARD_METHOD = 'memory.import.discard'
const EXPORT_PROMPT_VERSION = 'profile-export-v1'
const CLIENT_SCHEMA_VERSION = 1
const CLIENT_INPUT_LIMIT_BYTES = 256 * 1024

type PanelState =
  | 'loading'
  | 'unsupported'
  | 'input'
  | 'analyzing'
  | 'paused'
  | 'preview'
  | 'no-change'
  | 'success'
  | 'stale-undo'
  | 'error'
type AnalysisPhase = 'reading' | 'model' | 'diff'
type ImportTarget = 'USER' | 'MEMORY' | 'IMPORT'
type Operation = 'info' | 'copy' | 'preview' | 'apply' | 'undo'
type JobStatus = 'queued' | 'analyzing' | 'cancelling' | 'cancelled'
  | 'interrupted' | 'ready' | 'failed' | 'applied' | 'discarded'

interface ImportInfo {
  schemaVersion: number
  available: boolean
  provider: string
  model: string
  isLocal: boolean
  maxInputBytes: number
  promptVersion: string
  recentImport: RecentImport | null
  draftJob: ImportJob | null
}

interface RecentImport {
  receiptId: string
  batchId: string
  appliedAt: string
  summary: string[]
  provider: string
  model: string
  status: string
  indexStatus: string
  fileCount: number
  targets: ImportTarget[]
}

interface ImportDiffFile {
  target: ImportTarget
  displayName: string
  relativePath: string
  status: 'created' | 'modified' | 'deleted'
  additions: number
  deletions: number
  diff: string
}

interface ImportPreview {
  schemaVersion: number
  previewId: string
  batchId: string
  candidateHash: string
  provider: string
  model: string
  summary: string[]
  decisionCounts: {
    applied: number
    duplicate: number
    unresolved: number
  }
  files: ImportDiffFile[]
}

interface ImportJob {
  schemaVersion: number
  jobId: string
  batchId: string
  status: JobStatus
  stage: AnalysisPhase
  provider: string
  model: string
  startedAt: string
  canRetry: boolean
  errorCode: string
  preview: ImportPreview | null
}

interface RpcError extends Error {
  code?: string
}

const { t, tm, locale } = useI18n()
const rpc = useRpcStore()
const state = ref<PanelState>('loading')
const phase = ref<AnalysisPhase>('reading')
const rawText = ref('')
const promptOpen = ref(true)
const promptCopied = ref(false)
const submitAttempted = ref(false)
const busy = ref(false)
const errorCode = ref('')
const previewErrorVisible = ref(false)
const retryErrorVisible = ref(false)
const lastOperation = ref<Operation>('info')
const previewMode = ref<'import' | 'undo'>('import')
const successKind = ref<'import' | 'undo'>('import')
const info = shallowRef<ImportInfo | null>(null)
const preview = shallowRef<ImportPreview | null>(null)
const importJob = shallowRef<ImportJob | null>(null)
const slowAnalysis = ref(false)
const recentImport = shallowRef<RecentImport | null>(null)
const previewHeading = ref<HTMLElement | null>(null)
const previewRequestId = ref('')
const applyIdempotencyKey = ref('')
const undoRequestId = ref('')
let copyResetTimer: ReturnType<typeof setTimeout> | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
let slowTimer: ReturnType<typeof setTimeout> | null = null
let stateEnterResolver: (() => void) | null = null
let focusHeadingAfterEnter = false

const exportPrompt = computed(() => {
  // This prompt intentionally contains a literal "<name>" placeholder. Read
  // the locale message without compiling it as HTML or an interpolated UI
  // label so the copied text remains exact and Vue I18n emits no HTML warning.
  void locale.value
  return String(tm('settings.memoryImport.exportPrompt'))
})
const inputBytes = computed(() => new TextEncoder().encode(rawText.value).byteLength)
const maxInputBytes = computed(() => Math.min(
  CLIENT_INPUT_LIMIT_BYTES,
  info.value?.maxInputBytes || CLIENT_INPUT_LIMIT_BYTES,
))
const inputError = computed(() => {
  if (!submitAttempted.value) return ''
  if (!rawText.value.trim()) return t('settings.memoryImport.inputRequired')
  if (inputBytes.value > maxInputBytes.value) {
    return t('settings.memoryImport.inputTooLarge', {
      size: formatBytes(inputBytes.value),
      limit: formatBytes(maxInputBytes.value),
    })
  }
  return ''
})
const providerLabel = computed(() => info.value?.provider || preview.value?.provider || '')
const modelLabel = computed(() => info.value?.model || preview.value?.model || '')
const hasCanonicalRemovalOnly = computed(() => Boolean(
  preview.value?.files.some(file => {
    if (file.target !== 'USER' && file.target !== 'MEMORY') return false
    return file.status === 'deleted'
      || (file.status === 'modified' && file.deletions > 0 && file.additions === 0)
  }),
))
const totalAdditions = computed(() => (
  preview.value?.files.reduce((total, file) => total + file.additions, 0) || 0
))
const totalDeletions = computed(() => (
  preview.value?.files.reduce((total, file) => total + file.deletions, 0) || 0
))
const errorMessage = computed(() => {
  const keyByCode: Record<string, string> = {
    MEMORY_IMPORT_UNAVAILABLE: 'unavailable',
    MEMORY_IMPORT_INPUT_TOO_LARGE: 'inputTooLarge',
    MEMORY_IMPORT_MODEL_FAILED: 'modelFailed',
    MEMORY_IMPORT_INVALID_OUTPUT: 'invalidOutput',
    MEMORY_IMPORT_PREVIEW_EXPIRED: 'previewExpired',
    MEMORY_IMPORT_STALE_PREVIEW: 'stalePreview',
    MEMORY_IMPORT_WRITE_FAILED: 'writeFailed',
  }
  const key = keyByCode[errorCode.value] || 'default'
  return t(`settings.memoryImport.errors.${key}`)
})
const formattedRecentDate = computed(() => {
  const value = recentImport.value?.appliedAt
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat(locale.value, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
})
const recentTargets = computed(() => {
  const targets = new Set(recentImport.value?.targets || [])
  return (['USER', 'MEMORY', 'IMPORT'] as ImportTarget[]).filter(target => targets.has(target))
})
const pausedMessage = computed(() => {
  const current = importJob.value
  if (
    current?.status === 'failed'
    && (
      current.errorCode === 'MEMORY_IMPORT_MODEL_FAILED'
      || current.errorCode === 'MEMORY_IMPORT_INVALID_OUTPUT'
    )
  ) {
    const key = current.errorCode === 'MEMORY_IMPORT_MODEL_FAILED' ? 'modelFailed' : 'invalidOutput'
    return t(`settings.memoryImport.errors.${key}`)
  }
  return t(`settings.memoryImport.jobStates.${current?.status || 'failed'}.description`)
})

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function textValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string') : []
}

function normalizeRecent(value: unknown): RecentImport | null {
  const data = objectValue(value)
  const receiptId = textValue(data.receiptId)
  if (!receiptId) return null
  return {
    receiptId,
    batchId: textValue(data.batchId),
    appliedAt: textValue(data.appliedAt),
    summary: stringList(data.summary),
    provider: textValue(data.provider),
    model: textValue(data.model),
    status: textValue(data.status) || 'applied',
    indexStatus: textValue(data.indexStatus),
    fileCount: numberValue(data.fileCount),
    targets: Array.isArray(data.targets)
      ? data.targets.map(normalizeTarget).filter((target): target is ImportTarget => target !== null)
      : [],
  }
}

function normalizeInfo(value: unknown): ImportInfo {
  const data = objectValue(value)
  return {
    schemaVersion: numberValue(data.schemaVersion),
    available: data.available === true,
    provider: textValue(data.provider),
    model: textValue(data.model),
    isLocal: data.isLocal === true || data.isLoopback === true || data.isLocalEndpoint === true,
    maxInputBytes: numberValue(data.maxInputBytes ?? data.maxRawBytes) || CLIENT_INPUT_LIMIT_BYTES,
    promptVersion: textValue(data.promptVersion),
    recentImport: normalizeRecent(data.recentImport),
    draftJob: normalizeJob(data.draftJob),
  }
}

function normalizeTarget(value: unknown): ImportTarget | null {
  if (value === 'USER' || value === 'MEMORY' || value === 'IMPORT') return value
  return null
}

function normalizePreview(value: unknown): ImportPreview | null {
  const data = objectValue(value)
  const counts = objectValue(data.decisionCounts)
  const files: ImportDiffFile[] = []
  if (Array.isArray(data.files)) {
    for (const entry of data.files) {
      const file = objectValue(entry)
      const target = normalizeTarget(file.target ?? file.logicalTarget)
      const diff = textValue(file.diff)
      if (!target || !diff) continue
      files.push({
        target,
        displayName: textValue(file.displayName),
        relativePath: textValue(file.relativePath),
        status: file.status === 'created' || file.status === 'deleted' ? file.status : 'modified',
        additions: numberValue(file.additions),
        deletions: numberValue(file.deletions),
        diff,
      })
    }
  }
  const normalized: ImportPreview = {
    schemaVersion: numberValue(data.schemaVersion),
    previewId: textValue(data.previewId),
    batchId: textValue(data.batchId),
    candidateHash: textValue(data.candidateHash),
    provider: textValue(data.provider),
    model: textValue(data.model),
    summary: stringList(data.summary),
    decisionCounts: {
      applied: numberValue(counts.applied),
      duplicate: numberValue(counts.duplicate),
      unresolved: numberValue(counts.unresolved),
    },
    files,
  }
  return normalized.previewId && normalized.candidateHash ? normalized : null
}

function normalizeJob(value: unknown): ImportJob | null {
  const data = objectValue(value)
  const jobId = textValue(data.jobId)
  if (!jobId) return null
  const status = textValue(data.status) as JobStatus
  if (![
    'queued',
    'analyzing',
    'cancelling',
    'cancelled',
    'interrupted',
    'ready',
    'failed',
    'applied',
    'discarded',
  ].includes(status)) return null
  const stageValue = textValue(data.stage)
  return {
    schemaVersion: numberValue(data.schemaVersion),
    jobId,
    batchId: textValue(data.batchId),
    status,
    stage: stageValue === 'model' || stageValue === 'diff' ? stageValue : 'reading',
    provider: textValue(data.provider),
    model: textValue(data.model),
    startedAt: textValue(data.startedAt),
    canRetry: data.canRetry === true,
    errorCode: textValue(data.errorCode),
    preview: normalizePreview(data.preview),
  }
}

function rpcErrorCode(error: unknown): string {
  return textValue((error as RpcError | undefined)?.code)
}

function isMethodMissing(error: unknown): boolean {
  return rpcErrorCode(error) === 'METHOD_NOT_FOUND'
    || /method not found|unknown method|not registered/i.test(
      error instanceof Error ? error.message : String(error),
    )
}

function createRequestId(): string {
  if (typeof crypto?.randomUUID === 'function') return crypto.randomUUID()
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  return `${Math.ceil(bytes / 1024)} KiB`
}

function targetLabel(target: ImportTarget): string {
  return t(`settings.memoryImport.targets.${target}`)
}

function fileStatusLabel(file: ImportDiffFile): string {
  return t(`settings.memoryImport.fileStatus.${file.status}`)
}

function diffLineKind(line: string): string {
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')) return 'meta'
  if (line.startsWith('+')) return 'addition'
  if (line.startsWith('-')) return 'deletion'
  return 'context'
}

function diffLineSign(line: string): string {
  const kind = diffLineKind(line)
  if (kind === 'addition') return '+'
  if (kind === 'deletion') return '-'
  return ' '
}

function diffLineContent(line: string): string {
  const kind = diffLineKind(line)
  return kind === 'addition' || kind === 'deletion' ? line.slice(1) : line
}

async function focusPreviewHeading() {
  await nextTick()
  previewHeading.value?.focus()
}

function waitForNextStateEnter(): Promise<void> {
  return new Promise((resolve) => {
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      if (stateEnterResolver === finish) stateEnterResolver = null
      resolve()
    }
    stateEnterResolver = finish
    window.setTimeout(finish, 250)
  })
}

function focusPreviewHeadingAfterTransition() {
  focusHeadingAfterEnter = true
  void focusPreviewHeading()
}

function handleStateEntered() {
  stateEnterResolver?.()
  if (!focusHeadingAfterEnter) return
  focusHeadingAfterEnter = false
  void focusPreviewHeading()
}

async function loadInfo() {
  state.value = 'loading'
  lastOperation.value = 'info'
  errorCode.value = ''
  previewErrorVisible.value = false
  retryErrorVisible.value = false
  try {
    await rpc.waitForConnection(8000)
    if (!rpc.supportsMethod(INFO_METHOD)) {
      state.value = 'unsupported'
      return
    }
    const result = normalizeInfo(await rpc.call(INFO_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
    }))
    if (result.schemaVersion !== CLIENT_SCHEMA_VERSION) {
      state.value = 'unsupported'
      return
    }
    info.value = result
    recentImport.value = result.recentImport
    if (result.draftJob) {
      await handleJob(result.draftJob)
    } else {
      state.value = result.available ? 'input' : 'unsupported'
    }
  } catch (error) {
    if (isMethodMissing(error)) {
      rpc.markMethodUnavailable(INFO_METHOD)
      state.value = 'unsupported'
      return
    }
    errorCode.value = rpcErrorCode(error)
    state.value = 'error'
  }
}

async function copyPrompt() {
  try {
    await copyTextWithFallback(exportPrompt.value)
    promptCopied.value = true
    if (copyResetTimer) clearTimeout(copyResetTimer)
    copyResetTimer = setTimeout(() => {
      promptCopied.value = false
      copyResetTimer = null
    }, 1500)
  } catch {
    errorCode.value = ''
    lastOperation.value = 'copy'
    state.value = 'error'
  }
}

async function requestPreview() {
  submitAttempted.value = true
  if (inputError.value || !info.value?.available) return
  if (!rpc.supportsMethod(START_METHOD) || !rpc.supportsMethod(STATUS_METHOD)) {
    state.value = 'unsupported'
    return
  }

  const analyzingEntered = waitForNextStateEnter()
  phase.value = 'reading'
  previewMode.value = 'import'
  lastOperation.value = 'preview'
  errorCode.value = ''
  previewErrorVisible.value = false
  retryErrorVisible.value = false
  if (!previewRequestId.value) previewRequestId.value = createRequestId()
  state.value = 'analyzing'
  await analyzingEntered

  try {
    const result = normalizeJob(await rpc.call(START_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
      rawText: rawText.value,
      uiLocale: locale.value,
      exportPromptVersion: EXPORT_PROMPT_VERSION,
      expectedProvider: info.value.provider,
      expectedModel: info.value.model,
      expectedIsLocal: info.value.isLocal,
      clientRequestId: previewRequestId.value,
    }))
    if (!result || result.schemaVersion !== CLIENT_SCHEMA_VERSION) {
      throw Object.assign(new Error('Invalid profile import job'), {
        code: 'MEMORY_IMPORT_INVALID_OUTPUT',
      })
    }
    await handleJob(result)
  } catch (error) {
    if (isMethodMissing(error)) {
      rpc.markMethodUnavailable(START_METHOD)
      state.value = 'unsupported'
      return
    }
    errorCode.value = rpcErrorCode(error)
    previewErrorVisible.value = true
    state.value = 'input'
  }
}

function clearJobTimers() {
  if (pollTimer) clearTimeout(pollTimer)
  if (slowTimer) clearTimeout(slowTimer)
  pollTimer = null
  slowTimer = null
}

function armSlowNotice(current: ImportJob) {
  slowAnalysis.value = false
  if (!current.startedAt || current.stage !== 'model') return
  const elapsed = Date.now() - new Date(current.startedAt).getTime()
  const remaining = Math.max(0, 60_000 - elapsed)
  slowTimer = setTimeout(() => {
    slowAnalysis.value = true
  }, remaining)
}

function scheduleJobPoll() {
  if (!importJob.value || document.hidden) return
  if (!['queued', 'analyzing', 'cancelling'].includes(importJob.value.status)) return
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = setTimeout(() => void pollJob(), 2000)
}

async function pollJob() {
  const current = importJob.value
  if (!current || document.hidden) return
  try {
    const result = normalizeJob(await rpc.call(STATUS_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
      jobId: current.jobId,
    }))
    if (!result) throw new Error('Invalid profile import job status')
    await handleJob(result)
  } catch (error) {
    errorCode.value = rpcErrorCode(error)
    state.value = 'error'
  }
}

async function handleJob(current: ImportJob) {
  clearJobTimers()
  retryErrorVisible.value = false
  importJob.value = current
  phase.value = current.stage
  if (current.status === 'ready' && current.preview) {
    preview.value = current.preview
    applyIdempotencyKey.value = createRequestId()
    if (current.preview.files.length) {
      state.value = 'preview'
      focusPreviewHeadingAfterTransition()
    } else {
      await applyPreview()
    }
    return
  }
  if (['queued', 'analyzing', 'cancelling'].includes(current.status)) {
    state.value = 'analyzing'
    armSlowNotice(current)
    scheduleJobPoll()
    return
  }
  if (['failed', 'cancelled', 'interrupted'].includes(current.status)) {
    errorCode.value = current.errorCode
    state.value = 'paused'
    return
  }
  state.value = 'input'
}

async function cancelImport() {
  const current = importJob.value
  if (!current || busy.value) return
  busy.value = true
  try {
    const result = normalizeJob(await rpc.call(CANCEL_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
      jobId: current.jobId,
      clientRequestId: createRequestId(),
    }))
    if (result) await handleJob(result)
  } finally {
    busy.value = false
  }
}

async function retryImport() {
  const current = importJob.value
  if (!current || !info.value || busy.value) return
  busy.value = true
  retryErrorVisible.value = false
  try {
    const result = normalizeJob(await rpc.call(RETRY_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
      jobId: current.jobId,
      clientRequestId: createRequestId(),
      expectedProvider: info.value.provider,
      expectedModel: info.value.model,
      expectedIsLocal: info.value.isLocal,
    }))
    if (!result || result.schemaVersion !== CLIENT_SCHEMA_VERSION) {
      throw Object.assign(new Error('Invalid profile import retry job'), {
        code: 'MEMORY_IMPORT_INVALID_OUTPUT',
      })
    }
    await handleJob(result)
  } catch {
    retryErrorVisible.value = true
  } finally {
    busy.value = false
  }
}

async function discardJob() {
  const current = importJob.value
  if (!current || busy.value) return
  busy.value = true
  try {
    await rpc.call(DISCARD_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
      jobId: current.jobId,
    })
    clearJobTimers()
    importJob.value = null
    preview.value = null
    previewRequestId.value = ''
    retryErrorVisible.value = false
    state.value = 'input'
  } finally {
    busy.value = false
  }
}

async function discardPreview() {
  const previewId = preview.value?.previewId
  if (!previewId || !rpc.supportsMethod(DISCARD_METHOD)) return
  try {
    await rpc.call(DISCARD_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
      previewId,
    })
  } catch (error) {
    if (isMethodMissing(error)) rpc.markMethodUnavailable(DISCARD_METHOD)
  }
}

async function backFromPreview() {
  if (previewMode.value === 'import' && importJob.value) await discardJob()
  else await discardPreview()
  preview.value = null
  previewRequestId.value = ''
  applyIdempotencyKey.value = ''
  submitAttempted.value = false
  errorCode.value = ''
  previewErrorVisible.value = false
  retryErrorVisible.value = false
  state.value = 'input'
}

async function applyPreview() {
  const current = preview.value
  if (!current || busy.value) return
  if (!rpc.supportsMethod(APPLY_METHOD)) {
    state.value = 'unsupported'
    return
  }
  busy.value = true
  lastOperation.value = 'apply'
  errorCode.value = ''
  if (!applyIdempotencyKey.value) applyIdempotencyKey.value = createRequestId()
  const isNoChangeImport = previewMode.value === 'import' && current.files.length === 0
  try {
    const data = objectValue(await rpc.call(APPLY_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
      previewId: current.previewId,
      candidateHash: current.candidateHash,
      idempotencyKey: applyIdempotencyKey.value,
    }))
    if (Number(data.schemaVersion) !== CLIENT_SCHEMA_VERSION) {
      throw Object.assign(new Error('Invalid profile import apply result'), {
        code: 'MEMORY_IMPORT_INVALID_OUTPUT',
      })
    }
    const returnedRecent = normalizeRecent(data.recentImport)
    recentImport.value = returnedRecent || {
      receiptId: textValue(data.receiptId),
      batchId: textValue(data.batchId) || current.batchId,
      appliedAt: textValue(data.appliedAt) || new Date().toISOString(),
      summary: current.summary,
      provider: current.provider,
      model: current.model,
      status: previewMode.value === 'undo' ? 'undone' : 'applied',
      indexStatus: textValue(data.indexStatus),
      fileCount: current.files.length,
      targets: Array.from(new Set(current.files.map(file => file.target))),
    }
    successKind.value = previewMode.value
    state.value = isNoChangeImport ? 'no-change' : 'success'
    rawText.value = ''
    previewRequestId.value = ''
    submitAttempted.value = false
    preview.value = null
    importJob.value = null
    applyIdempotencyKey.value = ''
    if (isNoChangeImport) focusPreviewHeadingAfterTransition()
  } catch (error) {
    errorCode.value = rpcErrorCode(error)
    if (
      errorCode.value === 'MEMORY_IMPORT_PREVIEW_EXPIRED'
      || errorCode.value === 'MEMORY_IMPORT_STALE_PREVIEW'
    ) {
      await discardPreview()
      preview.value = null
      previewRequestId.value = ''
      applyIdempotencyKey.value = ''
      if (previewMode.value === 'undo') {
        undoRequestId.value = ''
        lastOperation.value = 'undo'
      } else {
        lastOperation.value = 'preview'
      }
    }
    state.value = 'error'
  } finally {
    busy.value = false
  }
}

async function undoRecent() {
  const recent = recentImport.value
  if (!recent?.receiptId || busy.value) return
  if (!rpc.supportsMethod(UNDO_METHOD)) {
    state.value = 'unsupported'
    return
  }
  busy.value = true
  lastOperation.value = 'undo'
  errorCode.value = ''
  if (!undoRequestId.value) undoRequestId.value = createRequestId()
  try {
    const data = objectValue(await rpc.call(UNDO_METHOD, {
      schemaVersion: CLIENT_SCHEMA_VERSION,
      agentId: 'main',
      receiptId: recent.receiptId,
      clientRequestId: undoRequestId.value,
      expectedProvider: info.value?.provider,
      expectedModel: info.value?.model,
      expectedIsLocal: info.value?.isLocal,
    }))
    if (Number(data.schemaVersion) !== CLIENT_SCHEMA_VERSION) {
      throw Object.assign(new Error('Invalid profile import undo result'), {
        code: 'MEMORY_IMPORT_INVALID_OUTPUT',
      })
    }
    const status = textValue(data.status)
    if (status === 'undone' || status === 'alreadyUndone') {
      recentImport.value = {
        ...recent,
        status: 'undone',
        indexStatus: textValue(data.indexStatus),
      }
      successKind.value = 'undo'
      state.value = 'success'
      undoRequestId.value = ''
      return
    }
    if (status === 'reviewRequired') {
      const result = normalizePreview(data.preview)
      if (!result) {
        throw Object.assign(new Error('Invalid undo preview'), {
          code: 'MEMORY_IMPORT_INVALID_OUTPUT',
        })
      }
      if (
        result.provider !== info.value?.provider
        || result.model !== info.value?.model
      ) {
        throw Object.assign(new Error('Undo preview model changed'), {
          code: 'MEMORY_IMPORT_INVALID_OUTPUT',
        })
      }
      preview.value = result
      previewMode.value = 'undo'
      applyIdempotencyKey.value = createRequestId()
      undoRequestId.value = ''
      // Even a zero-file undo preview must be confirmed so the original
      // receipt can be marked undone without changing current files.
      state.value = 'stale-undo'
      focusPreviewHeadingAfterTransition()
      return
    }
    throw Object.assign(new Error('Invalid undo result'), {
      code: 'MEMORY_IMPORT_INVALID_OUTPUT',
    })
  } catch (error) {
    if (isMethodMissing(error)) {
      rpc.markMethodUnavailable(UNDO_METHOD)
      state.value = 'unsupported'
      return
    }
    errorCode.value = rpcErrorCode(error)
    state.value = 'error'
  } finally {
    busy.value = false
  }
}

function resetForAnother() {
  rawText.value = ''
  preview.value = null
  importJob.value = null
  previewRequestId.value = ''
  applyIdempotencyKey.value = ''
  undoRequestId.value = ''
  submitAttempted.value = false
  errorCode.value = ''
  previewErrorVisible.value = false
  retryErrorVisible.value = false
  previewMode.value = 'import'
  state.value = 'input'
}

function retryLastOperation() {
  if (lastOperation.value === 'info') void loadInfo()
  else if (lastOperation.value === 'copy') void copyPrompt()
  else if (lastOperation.value === 'preview') void requestPreview()
  else if (lastOperation.value === 'apply') void applyPreview()
  else void undoRecent()
}

function handleVisibilityChange() {
  if (
    !document.hidden
    && importJob.value
    && ['queued', 'analyzing', 'cancelling'].includes(importJob.value.status)
  ) {
    void pollJob()
  }
}

function handleRawInput() {
  submitAttempted.value = false
  previewRequestId.value = ''
  errorCode.value = ''
  previewErrorVisible.value = false
  retryErrorVisible.value = false
}

onMounted(() => {
  document.addEventListener('visibilitychange', handleVisibilityChange)
  void loadInfo()
})

onUnmounted(() => {
  if (copyResetTimer) clearTimeout(copyResetTimer)
  clearJobTimers()
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<template>
  <section class="memory-import" data-testid="settings-memory-panel">
    <header class="memory-import__head">
      <h3 class="memory-import__title">{{ t('settings.memoryImport.title') }}</h3>
      <p class="memory-import__description">{{ t('settings.memoryImport.description') }}</p>
    </header>

    <div v-if="state === 'loading'" class="memory-import__center" aria-live="polite">
      <LoadingSpinner />
    </div>

    <div v-else-if="state === 'unsupported'" class="memory-import__state">
      <div class="memory-import__notice" role="status">
        <Icon name="info" :size="18" aria-hidden="true" />
        <div>
          <h4>{{ info?.available === false ? t('settings.memoryImport.unavailableTitle') : t('settings.memoryImport.upgradeTitle') }}</h4>
          <p>{{ info?.available === false ? t('settings.memoryImport.unavailableDescription') : t('settings.memoryImport.upgradeDescription') }}</p>
        </div>
      </div>
      <article v-if="recentImport" class="memory-import__recent">
        <div class="memory-import__recent-head">
          <div>
            <h4>{{ t('settings.memoryImport.recentTitle') }}</h4>
            <p v-if="formattedRecentDate">{{ formattedRecentDate }}</p>
          </div>
        </div>
        <div v-if="recentTargets.length" class="memory-import__target-chips">
          <span v-for="target in recentTargets" :key="target">
            {{ targetLabel(target) }}
          </span>
        </div>
        <details class="memory-import__details">
          <summary>
            {{ t('settings.memoryImport.processingDetails') }}
            <Icon name="chevronDown" :size="14" aria-hidden="true" />
          </summary>
          <div class="memory-import__details-body">
            <dl>
              <div>
                <dt>{{ t('settings.memoryImport.changedFiles') }}</dt>
                <dd>{{ recentImport.fileCount }}</dd>
              </div>
              <div>
                <dt>{{ t('settings.memoryImport.processingModel') }}</dt>
                <dd>{{ recentImport.provider }} / {{ recentImport.model }}</dd>
              </div>
            </dl>
            <p v-if="recentImport.indexStatus === 'pending'" class="memory-import__index-note">
              {{ t('settings.memoryImport.indexPending') }}
            </p>
          </div>
        </details>
        <button
          v-if="recentImport.status !== 'undone'"
          type="button"
          class="btn btn--ghost"
          :disabled="busy"
          data-testid="memory-import-undo"
          @click="undoRecent"
        >
          <span v-if="busy" class="memory-import__spinner" aria-hidden="true"></span>
          <Icon v-else name="refresh" :size="15" aria-hidden="true" />
          {{ busy ? t('settings.memoryImport.undoing') : t('settings.memoryImport.undoAction') }}
        </button>
      </article>
      </div>

    <Transition name="memory-import-swap" mode="out-in" @after-enter="handleStateEntered">
      <div
        v-if="state === 'input'"
        key="input"
        class="memory-import__state memory-import__input-state"
      >
        <ol
          class="memory-import__steps"
          :aria-label="t('settings.memoryImport.stepsLabel')"
        >
          <li
            class="memory-import__step"
            data-testid="memory-import-source-step"
          >
            <div class="memory-import__step-head">
              <span class="memory-import__step-number" aria-hidden="true">1</span>
              <div>
                <h4>{{ t('settings.memoryImport.sourceStepTitle') }}</h4>
                <p>{{ t('settings.memoryImport.sourceStepDescription') }}</p>
              </div>
            </div>

            <div class="memory-import__step-body">
              <div class="memory-import__prompt-actions">
                <button
                  type="button"
                  class="memory-import__prompt-toggle"
                  :aria-expanded="promptOpen ? 'true' : 'false'"
                  aria-controls="memory-import-export-prompt"
                  @click="promptOpen = !promptOpen"
                >
                  {{ promptOpen ? t('settings.memoryImport.hidePrompt') : t('settings.memoryImport.showPrompt') }}
                  <Icon :name="promptOpen ? 'chevronDown' : 'chevronRight'" :size="14" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  class="btn btn--ghost memory-import__copy"
                  data-testid="memory-import-copy-prompt"
                  @click="copyPrompt"
                >
                  <Icon :name="promptCopied ? 'check' : 'copy'" :size="15" aria-hidden="true" />
                  <span>{{ promptCopied ? t('settings.memoryImport.copiedPrompt') : t('settings.memoryImport.copyPrompt') }}</span>
                </button>
              </div>

              <pre
                v-show="promptOpen"
                id="memory-import-export-prompt"
                class="memory-import__prompt"
                data-testid="memory-import-export-prompt"
              >{{ exportPrompt }}</pre>
            </div>
          </li>

          <li
            class="memory-import__step"
            data-testid="memory-import-paste-step"
          >
            <div class="memory-import__step-head">
              <span class="memory-import__step-number" aria-hidden="true">2</span>
              <div>
                <h4>{{ t('settings.memoryImport.pasteStepTitle') }}</h4>
                <p>{{ t('settings.memoryImport.pasteStepDescription') }}</p>
              </div>
            </div>

            <div class="memory-import__step-body">
              <label class="memory-import__input-label" for="memory-import-raw-text">
                {{ t('settings.memoryImport.textareaLabel') }}
              </label>
              <textarea
                id="memory-import-raw-text"
                v-model="rawText"
                class="memory-import__textarea"
                data-testid="memory-import-textarea"
                :placeholder="t('settings.memoryImport.textareaPlaceholder')"
                :aria-invalid="inputError ? 'true' : 'false'"
                aria-describedby="memory-import-input-meta memory-import-input-error memory-import-privacy"
                @input="handleRawInput"
              ></textarea>
              <div id="memory-import-input-meta" class="memory-import__input-meta">
                <span>{{ t('settings.memoryImport.inputSize', {
                  size: formatBytes(inputBytes),
                  limit: formatBytes(maxInputBytes),
                }) }}</span>
                <span v-if="inputError" id="memory-import-input-error" class="memory-import__input-error" role="alert">
                  {{ inputError }}
                </span>
              </div>
            </div>
          </li>
        </ol>

        <div
          v-if="previewErrorVisible"
          class="memory-import__notice memory-import__notice--error"
          role="alert"
          data-testid="memory-import-preview-error"
        >
          <Icon name="info" :size="18" aria-hidden="true" />
          <div>
            <h4>{{ t('settings.memoryImport.errorTitle') }}</h4>
            <p>{{ errorMessage }}</p>
            <button
              type="button"
              class="btn btn--primary"
              @click="requestPreview"
            >
              {{ t('settings.memoryImport.retry') }}
            </button>
          </div>
        </div>

        <div id="memory-import-privacy" class="memory-import__privacy">
          <Icon :name="info?.isLocal ? 'monitor' : 'lock'" :size="15" aria-hidden="true" />
          <span>{{
            t(
              info?.isLocal
                ? 'settings.memoryImport.privacyLocal'
                : 'settings.memoryImport.privacyRemote',
              { provider: providerLabel, model: modelLabel },
            )
          }}</span>
        </div>

        <div class="memory-import__actions memory-import__actions--input">
          <button
            type="button"
            class="btn btn--primary"
            data-testid="memory-import-preview"
            @click="requestPreview"
          >
            {{ t('settings.memoryImport.previewAction') }}
          </button>
        </div>

        <article v-if="recentImport" class="memory-import__recent">
          <div class="memory-import__recent-head">
            <div>
              <h4>{{ t('settings.memoryImport.recentTitle') }}</h4>
              <p v-if="formattedRecentDate">{{ formattedRecentDate }}</p>
            </div>
          </div>
          <div v-if="recentTargets.length" class="memory-import__target-chips">
            <span v-for="target in recentTargets" :key="target">
              {{ targetLabel(target) }}
            </span>
          </div>
          <details class="memory-import__details">
            <summary>
              {{ t('settings.memoryImport.processingDetails') }}
              <Icon name="chevronDown" :size="14" aria-hidden="true" />
            </summary>
            <div class="memory-import__details-body">
              <dl>
                <div>
                  <dt>{{ t('settings.memoryImport.changedFiles') }}</dt>
                  <dd>{{ recentImport.fileCount }}</dd>
                </div>
                <div>
                  <dt>{{ t('settings.memoryImport.processingModel') }}</dt>
                  <dd>{{ recentImport.provider }} / {{ recentImport.model }}</dd>
                </div>
              </dl>
              <p v-if="recentImport.indexStatus === 'pending'" class="memory-import__index-note">
                {{ t('settings.memoryImport.indexPending') }}
              </p>
            </div>
          </details>
          <button
            v-if="recentImport.status !== 'undone'"
            type="button"
            class="btn btn--ghost"
            :disabled="busy"
            data-testid="memory-import-undo"
            @click="undoRecent"
          >
            <span v-if="busy" class="memory-import__spinner" aria-hidden="true"></span>
            <Icon v-else name="refresh" :size="15" aria-hidden="true" />
            {{ busy ? t('settings.memoryImport.undoing') : t('settings.memoryImport.undoAction') }}
          </button>
        </article>
      </div>

      <div
        v-else-if="state === 'analyzing'"
        key="analyzing"
        class="memory-import__state memory-import__analyzing"
        aria-live="polite"
        aria-atomic="true"
      >
        <span class="memory-import__spinner memory-import__spinner--large" aria-hidden="true"></span>
        <h4>{{ t('settings.memoryImport.analyzingTitle') }}</h4>
        <ol class="memory-import__phases">
          <li :class="{ 'is-active': phase === 'reading', 'is-complete': phase !== 'reading' }">
            <Icon v-if="phase !== 'reading'" name="check" :size="14" aria-hidden="true" />
            <span v-else class="memory-import__phase-dot" aria-hidden="true"></span>
            {{ t('settings.memoryImport.phases.reading') }}
          </li>
          <li :class="{ 'is-active': phase === 'model', 'is-complete': phase === 'diff' }">
            <Icon v-if="phase === 'diff'" name="check" :size="14" aria-hidden="true" />
            <span v-else class="memory-import__phase-dot" aria-hidden="true"></span>
            {{ t('settings.memoryImport.phases.model', { model: modelLabel }) }}
          </li>
          <li :class="{ 'is-active': phase === 'diff' }">
            <span class="memory-import__phase-dot" aria-hidden="true"></span>
            {{ t('settings.memoryImport.phases.diff') }}
          </li>
        </ol>
        <p v-if="slowAnalysis" class="memory-import__index-note" role="status">
          {{ t('settings.memoryImport.slowAnalysis') }}
        </p>
        <button
          type="button"
          class="btn btn--ghost"
          :disabled="busy || importJob?.status === 'cancelling'"
          data-testid="memory-import-cancel"
          @click="cancelImport"
        >
          {{ t('settings.memoryImport.cancelImport') }}
        </button>
      </div>

      <div
        v-else-if="state === 'paused'"
        key="paused"
        class="memory-import__state memory-import__result"
        role="status"
      >
        <Icon name="info" :size="24" aria-hidden="true" />
        <h4>{{ t(`settings.memoryImport.jobStates.${importJob?.status || 'failed'}.title`) }}</h4>
        <p>{{ pausedMessage }}</p>
        <div
          v-if="retryErrorVisible"
          class="memory-import__notice memory-import__notice--error"
          role="alert"
          data-testid="memory-import-retry-error"
        >
          <Icon name="info" :size="18" aria-hidden="true" />
          <div>
            <h4>{{ t('settings.memoryImport.retryFailedTitle') }}</h4>
            <p>{{ t('settings.memoryImport.retryFailedDescription') }}</p>
          </div>
        </div>
        <div class="memory-import__actions">
          <button
            type="button"
            class="btn btn--primary"
            :disabled="busy || !importJob?.canRetry"
            data-testid="memory-import-retry-job"
            @click="retryImport"
          >
            {{ t('settings.memoryImport.regeneratePreview') }}
          </button>
          <button
            type="button"
            class="btn btn--ghost"
            :disabled="busy"
            data-testid="memory-import-discard-job"
            @click="discardJob"
          >
            {{ t('settings.memoryImport.discardImport') }}
          </button>
        </div>
      </div>

      <div
        v-else-if="state === 'preview' || state === 'stale-undo'"
        key="preview"
        class="memory-import__state memory-import__preview"
      >
        <div v-if="state === 'stale-undo'" class="memory-import__notice memory-import__notice--warn">
          <Icon name="info" :size="18" aria-hidden="true" />
          <div>
            <h4>{{ t('settings.memoryImport.staleUndoTitle') }}</h4>
            <p>{{ t('settings.memoryImport.staleUndoDescription') }}</p>
          </div>
        </div>

        <div>
          <h4 ref="previewHeading" class="memory-import__preview-title" tabindex="-1">
            {{ state === 'stale-undo' ? t('settings.memoryImport.undoPreviewTitle') : t('settings.memoryImport.previewTitle') }}
          </h4>
          <p class="memory-import__preview-description">
            {{ t('settings.memoryImport.previewDescription', {
              count: preview?.files.length || 0,
              additions: totalAdditions,
              deletions: totalDeletions,
            }) }}
          </p>
        </div>

        <div v-if="preview?.summary.length" class="memory-import__model-analysis">
          <h5>{{ t('settings.memoryImport.modelAnalysisTitle') }}</h5>
          <ul class="memory-import__summary-list">
            <li v-for="item in preview.summary" :key="item">{{ item }}</li>
          </ul>
        </div>

        <div class="memory-import__decision-counts">
          <span v-if="preview?.decisionCounts.duplicate">
            {{ t('settings.memoryImport.duplicateCount', { count: preview.decisionCounts.duplicate }) }}
          </span>
          <span v-if="preview?.decisionCounts.unresolved">
            {{ t('settings.memoryImport.unresolvedCount', { count: preview.decisionCounts.unresolved }) }}
          </span>
        </div>

        <div
          v-if="hasCanonicalRemovalOnly"
          class="memory-import__notice memory-import__notice--warn"
        >
          <Icon name="info" :size="18" aria-hidden="true" />
          <div>
            <h4>{{ t('settings.memoryImport.removalWarningTitle') }}</h4>
            <p>{{ t('settings.memoryImport.removalWarning') }}</p>
          </div>
        </div>

        <div class="memory-import__files">
          <details
            v-for="file in preview?.files"
            :key="file.relativePath || file.target"
            class="memory-import__file"
          >
            <summary>
              <div class="memory-import__file-heading">
                <strong>{{ targetLabel(file.target) }}</strong>
                <span>{{ file.relativePath }}</span>
              </div>
              <div class="memory-import__file-meta">
                <span class="memory-import__file-chip">{{ fileStatusLabel(file) }}</span>
                <span class="memory-import__additions">+{{ file.additions }}</span>
                <span class="memory-import__deletions">-{{ file.deletions }}</span>
                <Icon name="chevronDown" :size="14" aria-hidden="true" />
              </div>
            </summary>
            <p v-if="file.target === 'MEMORY'" class="memory-import__impact-note">
              <Icon name="info" :size="14" aria-hidden="true" />
              {{ t('settings.memoryImport.memoryImpact') }}
            </p>
            <div
              class="memory-import__diff"
              role="region"
              tabindex="0"
              :aria-label="targetLabel(file.target)"
            >
              <div
                v-for="(line, index) in file.diff.split('\n')"
                :key="`${index}-${line}`"
                class="memory-import__diff-line"
                :class="`is-${diffLineKind(line)}`"
              >
                <span class="memory-import__diff-sign" aria-hidden="true">{{ diffLineSign(line) }}</span>
                <span
                  v-if="diffLineKind(line) === 'addition' || diffLineKind(line) === 'deletion'"
                  class="memory-import__diff-marker"
                >
                  {{ diffLineKind(line) === 'addition'
                    ? t('settings.memoryImport.diffLineAdded')
                    : t('settings.memoryImport.diffLineRemoved') }}
                </span>
                <span class="memory-import__diff-content">{{ diffLineContent(line) || ' ' }}</span>
              </div>
            </div>
          </details>
        </div>

        <div class="memory-import__actions memory-import__actions--split">
          <button type="button" class="btn btn--ghost" :disabled="busy" @click="backFromPreview">
            {{ previewMode === 'undo' ? t('settings.memoryImport.back') : t('settings.memoryImport.backToEdit') }}
          </button>
          <button
            type="button"
            class="btn btn--primary"
            :disabled="busy"
            data-testid="memory-import-apply"
            @click="applyPreview"
          >
            <span v-if="busy" class="memory-import__spinner" aria-hidden="true"></span>
            {{ previewMode === 'undo' ? t('settings.memoryImport.applyUndo') : t('settings.memoryImport.applyAll') }}
          </button>
        </div>
      </div>

      <div
        v-else-if="state === 'no-change'"
        key="no-change"
        class="memory-import__state memory-import__result"
        role="status"
      >
        <Icon name="check" :size="24" aria-hidden="true" />
        <h4 ref="previewHeading" tabindex="-1">{{ t('settings.memoryImport.noChangesTitle') }}</h4>
        <p>{{ t('settings.memoryImport.noChangesDescription') }}</p>
        <button type="button" class="btn btn--primary" @click="backFromPreview">
          {{ previewMode === 'undo' ? t('settings.memoryImport.done') : t('settings.memoryImport.pasteAnother') }}
        </button>
      </div>

      <div
        v-else-if="state === 'success'"
        key="success"
        class="memory-import__state memory-import__result"
        role="status"
      >
        <Icon name="check" :size="24" aria-hidden="true" />
        <h4>{{ successKind === 'undo' ? t('settings.memoryImport.undoSuccessTitle') : t('settings.memoryImport.successTitle') }}</h4>
        <p>{{
          successKind === 'undo'
            ? t('settings.memoryImport.undoSuccessDescription')
            : recentTargets.length
              ? t('settings.memoryImport.updatedCategories')
              : t('settings.memoryImport.successDescription')
        }}</p>

        <article
          v-if="recentImport && successKind === 'import'"
          class="memory-import__recent memory-import__recent--result"
        >
          <div class="memory-import__recent-head">
            <div>
              <h4>{{ t('settings.memoryImport.recentTitle') }}</h4>
              <p v-if="formattedRecentDate">{{ formattedRecentDate }}</p>
            </div>
          </div>
          <div v-if="recentTargets.length" class="memory-import__target-chips">
            <span v-for="target in recentTargets" :key="target">
              {{ targetLabel(target) }}
            </span>
          </div>
          <details class="memory-import__details" data-testid="memory-import-details">
            <summary>
              {{ t('settings.memoryImport.processingDetails') }}
              <Icon name="chevronDown" :size="14" aria-hidden="true" />
            </summary>
            <div class="memory-import__details-body">
              <dl>
                <div>
                  <dt>{{ t('settings.memoryImport.changedFiles') }}</dt>
                  <dd>{{ recentImport.fileCount }}</dd>
                </div>
                <div>
                  <dt>{{ t('settings.memoryImport.processingModel') }}</dt>
                  <dd>{{ recentImport.provider }} / {{ recentImport.model }}</dd>
                </div>
              </dl>
              <p v-if="recentImport.indexStatus === 'pending'" class="memory-import__index-note">
                {{ t('settings.memoryImport.indexPending') }}
              </p>
            </div>
          </details>
        </article>

        <div class="memory-import__actions">
          <button type="button" class="btn btn--ghost" @click="resetForAnother">
            {{ t('settings.memoryImport.pasteAnother') }}
          </button>
          <button
            v-if="recentImport?.receiptId && recentImport.status !== 'undone'"
            type="button"
            class="btn"
            :disabled="busy"
            data-testid="memory-import-undo"
            @click="undoRecent"
          >
            <span v-if="busy" class="memory-import__spinner" aria-hidden="true"></span>
            <Icon v-else name="refresh" :size="15" aria-hidden="true" />
            {{ busy ? t('settings.memoryImport.undoing') : t('settings.memoryImport.undoAction') }}
          </button>
        </div>
      </div>

      <div
        v-else-if="state === 'error'"
        key="error"
        class="memory-import__state memory-import__notice memory-import__notice--error"
        role="alert"
      >
        <Icon name="info" :size="18" aria-hidden="true" />
        <div>
          <h4>{{ t('settings.memoryImport.errorTitle') }}</h4>
          <p>{{ errorMessage }}</p>
          <div class="memory-import__inline-actions">
            <button type="button" class="btn btn--primary" @click="retryLastOperation">
              {{ t('settings.memoryImport.retry') }}
            </button>
            <button
              v-if="lastOperation !== 'info'"
              type="button"
              class="btn btn--ghost"
              @click="state = preview ? (previewMode === 'undo' ? 'stale-undo' : 'preview') : 'input'"
            >
              {{ t('settings.memoryImport.back') }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </section>
</template>

<style scoped>
.memory-import {
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
  margin: 0 auto;
  max-width: 880px;
  min-height: 100%;
}

.memory-import__head {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}

.memory-import__title,
.memory-import__head h3,
.memory-import__state h4,
.memory-import__notice h4,
.memory-import__recent h4 {
  color: var(--text);
  margin: 0;
}

.memory-import__title {
  font-size: var(--fs-lg);
}

.memory-import__description,
.memory-import__state p,
.memory-import__notice p,
.memory-import__recent p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  line-height: 1.55;
  margin: 0;
}

.memory-import__state {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  min-width: 0;
}

.memory-import-swap-enter-active,
.memory-import-swap-leave-active {
  transition:
    opacity var(--dur-base) var(--ease-out),
    transform var(--dur-base) var(--ease-out);
}

.memory-import-swap-enter-from,
.memory-import-swap-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

.memory-import__center {
  align-items: center;
  display: flex;
  flex: 1;
  justify-content: center;
  min-height: 220px;
}

.memory-import__steps {
  display: grid;
  gap: var(--sp-3);
  list-style: none;
  margin: 0;
  padding: 0;
}

.memory-import__step {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  min-width: 0;
  padding: var(--sp-4);
}

.memory-import__step-head {
  align-items: flex-start;
  display: flex;
  gap: var(--sp-3);
}

.memory-import__step-head > div {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}

.memory-import__step-head h4 {
  font-size: var(--fs-sm);
  line-height: 1.4;
}

.memory-import__step-number {
  align-items: center;
  background: var(--accent);
  border-radius: var(--radius-full);
  color: var(--accent-foreground);
  display: inline-flex;
  flex: 0 0 24px;
  font-size: var(--fs-xs);
  font-weight: 700;
  height: 24px;
  justify-content: center;
  line-height: 1;
  width: 24px;
}

.memory-import__step-body {
  display: grid;
  gap: var(--sp-2);
  min-width: 0;
}

.memory-import__prompt-actions {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: space-between;
}

.memory-import__prompt-toggle {
  align-items: center;
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: var(--fs-xs);
  gap: var(--sp-1);
  padding: 0;
  width: fit-content;
}

.memory-import__prompt-toggle:hover {
  color: var(--text);
}

.memory-import__copy {
  flex-shrink: 0;
}

.memory-import__prompt {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: 1.55;
  margin: 0;
  max-height: 180px;
  overflow: auto;
  padding: var(--sp-4);
  white-space: pre-wrap;
}

.memory-import__input-label {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.memory-import__textarea {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
  line-height: 1.55;
  min-height: 190px;
  padding: var(--sp-4);
  resize: vertical;
  transition:
    border-color var(--dur-fast) var(--ease-out),
    box-shadow var(--dur-fast) var(--ease-out);
  width: 100%;
}

.memory-import__textarea:hover {
  border-color: var(--border-strong);
}

.memory-import__textarea:focus-visible {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--focus-ring);
  outline: none;
}

.memory-import__textarea[aria-invalid="true"] {
  border-color: var(--danger);
}

.memory-import__input-meta {
  color: var(--text-dim);
  display: flex;
  font-size: var(--fs-xs);
  justify-content: space-between;
}

.memory-import__input-error,
.memory-import__deletions {
  color: var(--danger);
}

.memory-import__privacy {
  align-items: flex-start;
  color: var(--text-muted);
  display: flex;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  line-height: 1.5;
}

.memory-import__privacy .icon {
  margin-top: 2px;
}

.memory-import__actions {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
  justify-content: flex-end;
}

.memory-import__actions--split {
  border-top: 1px solid var(--border);
  justify-content: space-between;
  padding-bottom: max(var(--sp-1), env(safe-area-inset-bottom));
  padding-top: var(--sp-4);
  position: sticky;
  bottom: 0;
  background: var(--bg-surface);
}

.memory-import__notice {
  align-items: flex-start;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  gap: var(--sp-3);
  padding: var(--sp-4);
}

.memory-import__notice > div {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: var(--sp-1);
}

.memory-import__notice--warn {
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--warn) 32%, var(--border));
}

.memory-import__notice--error {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 32%, var(--border));
}

.memory-import__inline-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
}

.memory-import__analyzing {
  align-items: center;
  justify-content: center;
  min-height: 320px;
  text-align: center;
}

.memory-import__spinner {
  animation: memory-import-spin 0.8s linear infinite;
  border: 2px solid currentColor;
  border-radius: 50%;
  border-top-color: transparent;
  display: inline-block;
  height: 14px;
  width: 14px;
}

.memory-import__spinner--large {
  color: var(--accent);
  height: 28px;
  width: 28px;
}

@keyframes memory-import-spin {
  to { transform: rotate(360deg); }
}

.memory-import__phases {
  color: var(--text-dim);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  list-style: none;
  margin: 0;
  padding: 0;
  text-align: left;
}

.memory-import__phases li {
  align-items: center;
  display: flex;
  gap: var(--sp-2);
}

.memory-import__phases li.is-active {
  color: var(--text);
  font-weight: 600;
}

.memory-import__phases li.is-complete {
  color: var(--ok);
}

.memory-import__phase-dot {
  border: 1px solid currentColor;
  border-radius: 50%;
  display: inline-block;
  height: 10px;
  width: 10px;
}

.memory-import__preview-title {
  border-radius: var(--radius-sm);
  font-size: var(--fs-md);
  outline: none;
}

.memory-import__preview-title:focus-visible {
  box-shadow: 0 0 0 2px var(--focus-ring);
}

.memory-import__model-analysis h5 {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: 0 0 var(--sp-1);
}

.memory-import__summary-list {
  color: var(--text-muted);
  display: flex;
  flex-direction: column;
  font-size: var(--fs-sm);
  gap: var(--sp-1);
  margin: 0;
  padding-left: var(--sp-5);
}

.memory-import__decision-counts {
  color: var(--text-dim);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  gap: var(--sp-3);
}

.memory-import__files {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

.memory-import__file {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  overflow: clip;
}

.memory-import__file summary {
  align-items: center;
  background: var(--bg-elevated);
  cursor: pointer;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  list-style: none;
  padding: var(--sp-3) var(--sp-4);
}

.memory-import__file summary::-webkit-details-marker {
  display: none;
}

.memory-import__file-heading {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.memory-import__file-heading strong {
  color: var(--text);
  font-size: var(--fs-sm);
}

.memory-import__file-heading span {
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.memory-import__file-meta {
  align-items: center;
  color: var(--text-dim);
  display: flex;
  flex-shrink: 0;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  gap: var(--sp-2);
}

.memory-import__file[open] .memory-import__file-meta .icon {
  transform: rotate(180deg);
}

.memory-import__file-chip {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  color: var(--text-muted);
  font-family: var(--font-sans);
  font-size: var(--fs-xs);
  padding: 2px var(--sp-2);
}

.memory-import__additions {
  color: var(--ok);
}

.memory-import__impact-note {
  align-items: center;
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-surface));
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  display: flex;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-4);
}

.memory-import__diff {
  background: var(--bg);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
  padding: var(--sp-2) 0;
}

.memory-import__diff-line {
  display: grid;
  grid-template-columns: 28px max-content minmax(max-content, 1fr);
  min-height: 20px;
  padding: 0 var(--sp-3) 0 0;
  white-space: pre;
}

.memory-import__diff-content {
  grid-column: 3;
}

.memory-import__diff-line.is-context .memory-import__diff-content,
.memory-import__diff-line.is-hunk .memory-import__diff-content,
.memory-import__diff-line.is-meta .memory-import__diff-content {
  grid-column: 2 / 4;
}

.memory-import__diff-line.is-addition {
  background: color-mix(in srgb, var(--ok) 10%, transparent);
}

.memory-import__diff-line.is-deletion {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
}

.memory-import__diff-line.is-hunk,
.memory-import__diff-line.is-meta {
  color: var(--text-dim);
}

.memory-import__diff-sign {
  color: var(--text-dim);
  padding-right: var(--sp-2);
  text-align: right;
  user-select: none;
}

.memory-import__diff-line.is-addition .memory-import__diff-sign {
  color: var(--ok);
}

.memory-import__diff-line.is-deletion .memory-import__diff-sign {
  color: var(--danger);
}

.memory-import__diff-marker {
  color: var(--text-dim);
  font-family: var(--font-sans);
  font-size: 0.7rem;
  font-weight: 600;
  min-width: 4.75rem;
  padding-right: var(--sp-2);
  text-transform: uppercase;
  user-select: none;
}

.memory-import__diff-line.is-addition .memory-import__diff-marker {
  color: var(--ok);
}

.memory-import__diff-line.is-deletion .memory-import__diff-marker {
  color: var(--danger);
}

.memory-import__recent {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  margin-top: var(--sp-2);
  padding: var(--sp-4);
}

.memory-import__recent--result {
  text-align: left;
  width: min(560px, 100%);
}

.memory-import__recent-head {
  align-items: flex-start;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
}

.memory-import__target-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.memory-import__target-chips span {
  background: color-mix(in srgb, var(--accent) 8%, var(--bg-surface));
  border: 1px solid color-mix(in srgb, var(--accent) 22%, var(--border));
  border-radius: var(--radius-pill);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  padding: 3px var(--sp-2);
}

.memory-import__details {
  border-top: 1px solid var(--border);
  padding-top: var(--sp-2);
}

.memory-import__details summary {
  align-items: center;
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  font-size: var(--fs-xs);
  gap: var(--sp-1);
  list-style: none;
  width: fit-content;
}

.memory-import__details summary::-webkit-details-marker {
  display: none;
}

.memory-import__details summary:focus-visible {
  border-radius: var(--radius-sm);
  box-shadow: 0 0 0 2px var(--focus-ring);
  outline: none;
}

.memory-import__details summary .icon {
  transition: transform var(--dur-fast) var(--ease-out);
}

.memory-import__details[open] summary .icon {
  transform: rotate(180deg);
}

.memory-import__details-body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding-top: var(--sp-3);
}

.memory-import__details-body dl {
  display: grid;
  gap: var(--sp-2);
  margin: 0;
}

.memory-import__details-body dl > div {
  display: grid;
  font-size: var(--fs-xs);
  gap: var(--sp-3);
  grid-template-columns: minmax(6rem, 0.35fr) minmax(0, 1fr);
}

.memory-import__details-body dt {
  color: var(--text-dim);
}

.memory-import__details-body dd {
  color: var(--text-muted);
  margin: 0;
  overflow-wrap: anywhere;
}

.memory-import__index-note {
  color: var(--warn) !important;
}

.memory-import__result {
  align-items: center;
  justify-content: center;
  min-height: 300px;
  text-align: center;
}

.memory-import__result > .icon {
  color: var(--ok);
}

@media (max-width: 640px) {
  .memory-import {
    gap: var(--sp-4);
  }

  .memory-import__recent-head {
    align-items: stretch;
    flex-direction: column;
  }

  .memory-import__step {
    padding: var(--sp-3);
  }

  .memory-import__prompt {
    max-height: 150px;
  }

  .memory-import__copy {
    align-self: flex-start;
  }

  .memory-import__textarea {
    min-height: 160px;
  }

  .memory-import__input-meta,
  .memory-import__file summary {
    align-items: flex-start;
    flex-direction: column;
  }

  .memory-import__actions--split {
    margin: 0 calc(-1 * var(--sp-3));
    padding-left: var(--sp-3);
    padding-right: var(--sp-3);
  }

  .memory-import__actions--input {
    background: var(--bg-surface);
    bottom: 0;
    margin: 0 calc(-1 * var(--sp-3));
    padding: var(--sp-3);
    padding-bottom: max(var(--sp-3), env(safe-area-inset-bottom));
    position: sticky;
    z-index: 2;
  }

  .memory-import__file-meta {
    width: 100%;
  }

  .memory-import__file-meta .icon {
    margin-left: auto;
  }
}

@media (prefers-reduced-motion: reduce) {
  .memory-import-swap-enter-active,
  .memory-import-swap-leave-active {
    transition: opacity var(--dur-fast) var(--ease-out);
  }

  .memory-import-swap-enter-from,
  .memory-import-swap-leave-to {
    transform: none;
  }

  .memory-import__spinner {
    animation-duration: 1.6s; /* motion-allow: spinner cadence under reduced motion */
  }
}
</style>
