<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ARTIFACT_WORKBENCH_KEY, type WorkingFileMetadata, type WorkingFileRequest } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import type { ArtifactPayload } from '@/types/artifacts'
import { usePlatform } from '@/platform'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import { copyTextWithFallback, downloadBlob, isMacPlatform } from '@/utils/browser'
import { artifactWorkbenchPreviewKind } from '@/utils/workbench/artifactPreview'
import { isPreviewPagePath } from '@/utils/workbench/previewPagePath'
import Icon from '@/components/Icon.vue'

const props = withDefaults(defineProps<{
  artifact?: ArtifactPayload
  sessionKey?: string
  trigger?: boolean
  previewable?: boolean
}>(), { sessionKey: '', trigger: false, previewable: true })
const emit = defineEmits<{ open: [artifact: ArtifactPayload] }>()
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
const html = computed(() => !!selected.value && artifactWorkbenchPreviewKind({
  ...selected.value,
  ...(metadata.value ? { name: metadata.value.name, mime: metadata.value.mime } : {}),
}) === 'html')
const revealLabel = computed(() => t(isMacPlatform() ? 'resourceActions.revealFinder'
  : typeof navigator !== 'undefined' && /Windows/i.test(navigator.userAgent)
    ? 'resourceActions.revealExplorer' : 'resourceActions.reveal'))
const signature = computed(() => [props.sessionKey, props.artifact?.id, props.artifact?.documentId,
  props.artifact?.previewPagePath, props.artifact?.download_url].join('\0'))

function close() {
  visible.value = false
  invoker?.isConnected && invoker.focus({ preventScroll: true })
}
function cancel() {
  generation++
  pending?.abort()
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
  if (!artifact || !workbench) return
  if (event instanceof KeyboardEvent && event.key !== 'ContextMenu'
    && !(event.shiftKey && event.key === 'F10')) return
  event.preventDefault()
  event.stopPropagation()
  cancel()
  const attempt = ++generation
  pending = new AbortController()
  const signal = pending.signal
  session = props.sessionKey || String(artifact.session_key || artifact.sessionKey || '')
  epoch = gateway?.subscriptionEpoch ?? 0
  selected.value = { ...artifact }
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
    if (source.value) {
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
    if (attempt === generation) pushToast(t('resourceActions.unavailable'), { tone: 'danger' })
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

async function perform(action: 'preview' | 'save' | 'path' | 'contents' | 'open' | 'reveal' | 'copy-open') {
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
      pushToast(t('chat.copied'), { tone: 'ok' })
      return
    }
    let blob: Blob
    if (request) {
      if (!workbench.content.fetchWorkingFile) throw new Error(t('resourceActions.unavailable'))
      blob = await workbench.content.fetchWorkingFile(request)
    } else {
      const result = await workbench.content.fetchArtifact(artifact, { sessionKey: capturedSession, signal: pending.signal })
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
      pushToast(t('chat.copied'), { tone: 'ok' })
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
      pushToast(error instanceof Error ? error.message : t('resourceActions.failed'), { tone: 'danger' })
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
defineExpose({ show })
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
        <button v-if="previewable" role="menuitem" type="button" @click="perform('preview')">{{ t('resourceActions.preview') }}</button>
        <template v-if="localInstance && metadata">
          <button role="menuitem" type="button" @click="perform('open')">{{ t('resourceActions.openSource') }}</button>
          <button role="menuitem" type="button" @click="perform('reveal')">{{ revealLabel }}</button>
        </template>
        <button v-if="!source && platform.files.openArtifact" role="menuitem" type="button" @click="perform('copy-open')">{{ t('resourceActions.openCopy') }}</button>
        <template v-if="readable">
          <button role="menuitem" type="button" :title="source ? t('resourceActions.singleFile') : undefined" @click="perform('save')">
            {{ t(platform.files.saveArtifact ? 'resourceActions.saveAs' : 'chat.download') }}
          </button>
          <button v-if="source && !html" role="menuitem" type="button" @click="perform('path')">{{ t(localInstance ? 'resourceActions.copyPath' : 'resourceActions.copyGatewayPath') }}</button>
          <button v-if="!html" role="menuitem" type="button" @click="perform('contents')">{{ t('resourceActions.copyContents') }}</button>
        </template>
        <small v-if="source && !readable" role="status">{{ t(loading ? 'common.loading' : 'resourceActions.unavailable') }}</small>
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
.resource-actions-menu small { padding: 8px 12px; color: var(--text-muted); max-width: 280px; }
</style>
