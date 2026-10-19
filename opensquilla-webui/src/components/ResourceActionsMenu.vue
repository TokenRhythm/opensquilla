<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ARTIFACT_WORKBENCH_KEY, HtmlBundleExportError, type WorkingFileMetadata, type WorkingFileRequest } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import type { ArtifactPayload } from '@/types/artifacts'
import { usePlatform } from '@/platform'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import { useImageClipboard, isSvgClipboardCandidate } from '@/composables/useImageClipboard'
import { ImageClipboardError } from '@/utils/imageClipboard'
import { copyTextWithFallback, downloadBlob, isMacPlatform } from '@/utils/browser'
import { artifactWorkbenchPreviewKind } from '@/utils/workbench/artifactPreview'
import { isPreviewPagePath } from '@/utils/workbench/previewPagePath'
import { htmlBundleExportTarget } from '@/utils/workbench/htmlBundleExport'
import Icon from '@/components/Icon.vue'

const props = withDefaults(defineProps<{
  artifact?: ArtifactPayload
  /** Tab menus deliberately retain only download/reveal plus tab actions. */
  compact?: boolean
  /** Retain the panel's established download even without a local source file. */
  downloadAction?: string
  extraActions?: readonly { id: string; label: string; disabled?: boolean; separator?: boolean }[]
  sessionKey?: string
  trigger?: boolean
  previewable?: boolean
  requireSameOrigin?: boolean
  /** New media-card shortcuts must not turn a spoofed MIME into a script opener. */
  nativeOpenMediaOnly?: boolean
}>(), { sessionKey: '', trigger: false, previewable: true })
const emit = defineEmits<{ open: [artifact: ArtifactPayload]; action: [id: string] }>()
const { t } = useI18n()
const { pushToast } = useToasts()
const platform = usePlatform()
const workbench = inject(ARTIFACT_WORKBENCH_KEY, null)
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const visible = ref(false)
const menu = ref<HTMLElement | null>(null)
const point = ref({ left: 0, top: 0 })
const metadata = shallowRef<WorkingFileMetadata | null>(null)
const localInstance = ref<string | null>(null)
const loading = ref(false)
const selected = shallowRef<ArtifactPayload | null>(null)
const topmost = useDialogLayer(visible)
let invoker: HTMLElement | null = null
let pending: AbortController | null = null
let generation = 0
let session = ''
let epoch = 0

const source = computed(() => selected.value?.source === 'workspace-preview'
  && typeof selected.value.documentId === 'string')
const readable = computed(() => source.value ? !!metadata.value : !!selected.value)
const bundleTarget = computed(() => selected.value ? htmlBundleExportTarget(selected.value) : null)
const canOpenCopy = computed(() => !source.value && !!platform.files.openArtifact
  && (!props.nativeOpenMediaOnly || /\.(?:png|jpe?g|gif|webp|svg|bmp|ico|tiff?|avif|heic|heif|jxl|apng|mp3|wav|m4a|aac|flac|ogg|oga|opus|mp4|m4v|mov|webm|ogv|mkv|avi|wmv)$/i
    .test(String(selected.value?.name || '').trim())))
const html = computed(() => !!selected.value && artifactWorkbenchPreviewKind({
  ...selected.value,
  ...(metadata.value ? { name: metadata.value.name, mime: metadata.value.mime } : {}),
}) === 'html')
const clipboardArtifact = computed(() => selected.value ? {
  ...selected.value,
  ...(metadata.value ? { name: metadata.value.name, mime: metadata.value.mime } : {}),
} : null)
const svg = computed(() => !!clipboardArtifact.value && isSvgClipboardCandidate(clipboardArtifact.value))
const imageClipboard = useImageClipboard({
  source: () => clipboardArtifact.value ? { kind: 'artifact', artifact: clipboardArtifact.value } : null,
  sessionKey: () => props.sessionKey || session,
  loadBlob: async (signal, maxBytes) => {
    const artifact = selected.value
    if (!artifact || !workbench) throw new ImageClipboardError('failed')
    if (source.value) {
      if (!workbench.content.fetchWorkingFile) throw new ImageClipboardError('failed')
      let blob: Blob
      try {
        blob = await workbench.content.fetchWorkingFile({ ...currentRequest(artifact, signal), maxBytes })
      } catch (error) {
        if (error && typeof error === 'object' && 'code' in error && error.code === 'too_large') {
          throw new ImageClipboardError('tooLarge')
        }
        throw error
      }
      if (blob.size > maxBytes) throw new ImageClipboardError('tooLarge')
      return blob
    }
    const result = await workbench.content.fetchArtifact(artifact, { sessionKey: session, signal, maxBytes,
      ...(props.requireSameOrigin ? { requireSameOrigin: true } : {}) })
    if (!result.ok) throw new ImageClipboardError(result.errorCode === 'too_large' ? 'tooLarge' : 'failed')
    return result.blob
  },
})
function copySvgSource() {
  close()
  void imageClipboard.copy('svg-source')
}
const revealLabel = computed(() => t(isMacPlatform() ? 'resourceActions.revealFinder'
  : typeof navigator !== 'undefined' && /Windows/i.test(navigator.userAgent)
    ? 'resourceActions.revealExplorer' : 'resourceActions.reveal'))
const signature = computed(() => [props.sessionKey, props.artifact?.id, props.artifact?.documentId,
  props.artifact?.document_id, props.artifact?.revisionId, props.artifact?.sha256,
  props.artifact?.previewPagePath, props.artifact?.download_url, props.requireSameOrigin, props.nativeOpenMediaOnly].join('\0'))

function close() {
  visible.value = false
  invoker?.isConnected && invoker.focus({ preventScroll: true })
}
function cancel() {
  generation++
  pending?.abort()
  imageClipboard.cancel()
  close()
}
watch(signature, cancel)
watch(() => gateway?.subscriptionEpoch, cancel)
onBeforeUnmount(cancel)

function currentRequest(artifact: ArtifactPayload, signal: AbortSignal): WorkingFileRequest {
  const pagePath = artifact.previewPagePath
  if (pagePath !== undefined && !isPreviewPagePath(pagePath)) throw new Error(t('resourceActions.unavailable'))
  return { sessionKey: session, documentId: String(artifact.documentId),
    ...(typeof pagePath === 'string' ? { pagePath } : {}), signal }
}

async function show(event: MouseEvent | KeyboardEvent, artifact = props.artifact) {
  if ((!artifact || !workbench) && !props.extraActions?.length) return
  if (event instanceof KeyboardEvent && event.key !== 'ContextMenu'
    && !(event.shiftKey && event.key === 'F10')) return
  event.preventDefault()
  event.stopPropagation()
  cancel()
  const attempt = ++generation
  pending = new AbortController()
  const signal = pending.signal
  session = props.sessionKey || String(artifact?.session_key || artifact?.sessionKey || '')
  epoch = gateway?.subscriptionEpoch ?? 0
  selected.value = artifact ? { ...artifact } : null
  metadata.value = null
  localInstance.value = null
  invoker = (event.target instanceof Element
    ? event.target.closest<HTMLElement>('button, a, [tabindex]') : null)
    || (document.activeElement instanceof HTMLElement ? document.activeElement : null)
  const rect = invoker?.getBoundingClientRect()
  point.value = {
    left: event instanceof MouseEvent && event.type === 'contextmenu' ? event.clientX : rect?.left ?? 8,
    top: event instanceof MouseEvent && event.type === 'contextmenu' ? event.clientY : rect?.bottom ?? 8,
  }
  visible.value = true
  loading.value = !!source.value
  await nextTick()
  const bounds = menu.value?.getBoundingClientRect()
  point.value = { left: Math.max(8, Math.min(point.value.left, window.innerWidth - (bounds?.width || 250) - 8)),
    top: Math.max(8, Math.min(point.value.top, window.innerHeight - (bounds?.height || 320) - 8)) }
  const initialFocus = menu.value?.querySelector<HTMLButtonElement>('button:not(:disabled)') ?? menu.value
  initialFocus?.focus()
  try {
    if (source.value && artifact && workbench) {
      const info = await workbench.content.workingFileMetadata?.(currentRequest(artifact, signal))
      if (attempt !== generation) return
      metadata.value = info ?? null
      if (info && platform.id === 'desktop' && platform.files.sourceFileAction && platform.gateway.getConnection) {
        const [status, connection] = await Promise.all([platform.gateway.getStatus(), platform.gateway.getConnection()])
        if (attempt !== generation) return
        if (status.owned && status.status === 'ready' && connection.status === 'ready') {
          localInstance.value = connection.instanceId
        }
      }
    }
  } catch {
    if (attempt === generation && !props.downloadAction) pushToast(t('resourceActions.unavailable'), { tone: 'danger' })
  } finally {
    if (attempt === generation) {
      loading.value = false
      await nextTick()
      const height = menu.value?.getBoundingClientRect().height || 0
      point.value = { ...point.value, top: Math.max(8, Math.min(point.value.top, window.innerHeight - height - 8)) }
      if (visible.value && document.activeElement === menu.value) {
        menu.value?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus()
      }
    }
  }
}

async function perform(action: 'preview' | 'save' | 'export-zip' | 'path' | 'contents' | 'open' | 'reveal' | 'copy-open') {
  const artifact = selected.value
  if (!artifact || !workbench || !pending) return
  const isSource = source.value
  const request = isSource ? currentRequest(artifact, pending.signal) : null
  const attempt = generation
  const capturedSession = session
  const assertCurrent = () => {
    if (attempt !== generation || pending?.signal.aborted || (gateway?.subscriptionEpoch ?? 0) !== epoch) {
      throw new DOMException('Cancelled', 'AbortError')
    }
  }
  close()
  try {
    assertCurrent()
    if (action === 'export-zip') {
      const target = bundleTarget.value
      if (!target || !workbench.content.exportHtmlBundle) return
      const result = await workbench.content.exportHtmlBundle({ target, sessionKey: capturedSession, signal: pending.signal })
      assertCurrent()
      if (platform.files.saveArtifact) {
        const data = await result.blob.arrayBuffer()
        assertCurrent()
        const saved = await platform.files.saveArtifact({ data, name: result.filename, mime: 'application/zip' })
        assertCurrent()
        if (saved.status === 'saved') pushToast(t('resourceActions.saved'), { tone: 'ok' })
      } else downloadBlob(result.blob, result.filename)
      return
    }
    if (action === 'copy-open' && !canOpenCopy.value) return
    if (action === 'preview') { emit('open', artifact); return }
    if (action === 'open' || action === 'reveal') {
      if (!request || !localInstance.value || !platform.files.sourceFileAction) return
      await platform.files.sourceFileAction({ gatewayInstanceId: localInstance.value,
        sessionKey: capturedSession, documentId: request.documentId, pagePath: request.pagePath, action })
      return
    }
    if (action === 'path') {
      if (!request) return
      const info = await workbench.content.workingFileMetadata?.(request)
      assertCurrent()
      if (!info) throw new Error(t('resourceActions.unavailable'))
      await copyTextWithFallback(info.sourcePath)
      return
    }
    let blob: Blob
    if (request) {
      if (!workbench.content.fetchWorkingFile) throw new Error(t('resourceActions.unavailable'))
      blob = await workbench.content.fetchWorkingFile(request)
    } else {
      const result = await workbench.content.fetchArtifact(artifact, { sessionKey: capturedSession, signal: pending.signal,
        ...(props.requireSameOrigin ? { requireSameOrigin: true } : {}) })
      if (!result.ok) throw new Error(result.message)
      blob = result.blob
    }
    assertCurrent()
    if (action === 'contents') {
      const mime = (blob.type || String(artifact.mime || '')).split(';')[0]!
      if (blob.size > 1024 * 1024 || !/^(text\/|application\/(json|xml|xhtml\+xml|javascript)$)/.test(mime)) {
        throw new Error(t('resourceActions.textOnly'))
      }
      const bytes = await blob.arrayBuffer()
      assertCurrent()
      let text: string
      try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes) }
      catch { throw new Error(t('resourceActions.textOnly')) }
      if (text.includes('\0')) throw new Error(t('resourceActions.textOnly'))
      await copyTextWithFallback(text)
      return
    }
    const name = metadata.value?.name || String(artifact.name || 'file')
    if (action === 'copy-open' || platform.files.saveArtifact) {
      const data = await blob.arrayBuffer()
      assertCurrent()
      if (action === 'copy-open') {
        const result = await platform.files.openArtifact?.({ data, name, mime: blob.type })
        if (!result?.ok) throw new Error(result?.message || t('resourceActions.unavailable'))
      } else {
        const result = await platform.files.saveArtifact!({ data, name, mime: blob.type })
        if (result.status === 'saved') pushToast(t('resourceActions.saved'), { tone: 'ok' })
      }
    } else { downloadBlob(blob, name) }
  } catch (error) {
    if (attempt === generation && !(error instanceof DOMException && error.name === 'AbortError')) {
      const exportKey = error instanceof HtmlBundleExportError ? {
        unavailable: 'resourceActions.zipUnavailable', incomplete: 'resourceActions.zipIncomplete',
        'too-large': 'resourceActions.zipTooLarge', 'source-forbidden': 'resourceActions.zipSourceForbidden',
        'source-changed': 'resourceActions.zipSourceChanged', failed: 'resourceActions.zipFailed',
      }[error.code] : null
      pushToast(exportKey ? t(exportKey) : error instanceof Error ? error.message : t('resourceActions.failed'), { tone: 'danger' })
    }
  }
}

function onKeydown(event: KeyboardEvent) {
  if (!topmost.value) return
  if (event.key === 'Escape' || event.key === 'Tab') { event.preventDefault(); cancel(); return }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const buttons = [...menu.value!.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
  if (!buttons.length) return
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
  buttons[next]?.focus()
}
function performExtra(id: string) {
  cancel()
  emit('action', id)
}

defineExpose({ show, cancel })
</script>

<template>
  <button v-if="trigger" type="button" class="resource-actions-trigger btn btn--icon btn--ghost"
    :aria-label="t('resourceActions.more')" :title="t('resourceActions.more')" aria-haspopup="menu"
    :aria-expanded="visible" @click="show($event)" @keydown="show($event)">
    <Icon name="moreHorizontal" :size="16" />
  </button>
  <Teleport to="body">
    <div v-if="visible" class="resource-actions-backdrop" @pointerdown.self="cancel">
      <div ref="menu" class="resource-actions-menu" role="menu" tabindex="-1" :aria-label="t('resourceActions.more')"
        :style="{ left: `${point.left}px`, top: `${point.top}px` }" @keydown="onKeydown" @contextmenu.prevent>
        <button v-if="previewable && !compact" role="menuitem" type="button" @click="perform('preview')">{{ t('resourceActions.preview') }}</button>
        <template v-if="localInstance && metadata">
          <button v-if="!compact" role="menuitem" type="button" @click="perform('open')">{{ t('resourceActions.openSource') }}</button>
          <button role="menuitem" type="button" @click="perform('reveal')">{{ revealLabel }}</button>
        </template>
        <button v-if="canOpenCopy && !compact" role="menuitem" type="button" @click="perform('copy-open')">{{ t('resourceActions.openCopy') }}</button>
        <button v-if="!compact && bundleTarget && workbench?.content.exportHtmlBundle" role="menuitem" type="button"
          :title="t(source ? 'resourceActions.zipWorking' : 'resourceActions.zipSnapshot')"
          @click="perform('export-zip')">{{ t('resourceActions.downloadZip') }}</button>
        <button v-if="readable || downloadAction" role="menuitem" type="button"
          :title="source && !downloadAction ? t('resourceActions.singleFile') : undefined"
          @click="downloadAction ? performExtra(downloadAction) : perform('save')">
            {{ t(compact ? 'chat.download' : platform.files.saveArtifact ? 'resourceActions.saveAs' : 'chat.download') }}
        </button>
        <template v-if="readable">
          <button v-if="!compact && source && !html" role="menuitem" type="button" @click="perform('path')">{{ t(localInstance ? 'resourceActions.copyPath' : 'resourceActions.copyGatewayPath') }}</button>
          <button v-if="!compact && svg" role="menuitem" type="button" :disabled="imageClipboard.busy.value"
            @click="copySvgSource">{{ t('imageClipboard.copySource') }}</button>
          <button v-else-if="!compact && !html" role="menuitem" type="button" @click="perform('contents')">{{ t('resourceActions.copyContents') }}</button>
        </template>
        <template v-for="action in extraActions" :key="action.id">
          <hr v-if="action.separator" class="resource-actions-separator" role="separator" />
          <button role="menuitem" type="button" :disabled="action.disabled" @click="performExtra(action.id)">{{ action.label }}</button>
        </template>
        <small v-if="source && !readable && !downloadAction" role="status">{{ t(loading ? 'shared.loading' : 'resourceActions.unavailable') }}</small>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.resource-actions-trigger { flex-shrink: 0; }
.resource-actions-backdrop { position: fixed; inset: 0; z-index: 10000; }
.resource-actions-menu { position: fixed; display: flex; flex-direction: column; min-width: 220px; max-width: calc(100vw - 16px); max-height: calc(100vh - 16px); overflow-y: auto; padding: 5px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-surface); color: var(--text); box-shadow: var(--shadow-lg); }
.resource-actions-menu button { border: 0; border-radius: var(--radius-sm); padding: 8px 12px; background: transparent; color: inherit; text-align: start; font: inherit; font-size: 0.8125rem; cursor: pointer; }
.resource-actions-menu button:hover, .resource-actions-menu button:focus-visible { background: var(--bg-hover); outline: 1px solid var(--accent); }
.resource-actions-separator { width: 100%; margin: 4px 0; border: 0; border-top: 1px solid var(--border); }
.resource-actions-menu button:disabled { opacity: .5; cursor: default; }
.resource-actions-menu small { padding: 8px 12px; color: var(--text-muted); max-width: 280px; }
</style>
