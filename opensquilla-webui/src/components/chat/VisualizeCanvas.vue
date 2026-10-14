<template>
  <div class="visualize-position" data-chat-find-ignore :style="expanded ? { height: `${inlineHeight}px` } : undefined">
    <dialog ref="canvas" open class="visualize-canvas" :class="{ 'visualize-canvas--expanded': expanded }"
      :role="expanded ? 'dialog' : 'group'" :aria-modal="expanded || undefined" :aria-label="receipt.name"
      data-visualize-canvas @cancel.prevent="onEscape" @keydown.esc.stop.prevent="onEscape">
      <header class="visualize-toolbar">
        <span v-if="expanded" class="visualize-title" :title="receipt.name">{{ receipt.name }}</span>
        <p v-if="exportBusy || exportFeedback" class="visualize-export-feedback" :class="{ 'visualize-export-feedback--error': exportFailed }" role="status" :title="exportBusy ? t('visualize.exporting') : exportFeedback">{{ exportBusy ? t('visualize.exporting') : exportFeedback }}</p>
        <div class="visualize-actions">
          <div ref="menuAnchor" class="visualize-menu-anchor" @focusout="onMenuFocusOut">
            <button ref="menuButton" type="button" class="visualize-icon" :aria-label="t('visualize.actions')" :title="t('visualize.actions')"
              aria-haspopup="menu" :aria-expanded="menuOpen" :aria-controls="menuId" @click="toggleMenu" @keydown.down.prevent="openMenu(true)" @keydown.up.prevent="openMenu(false)"><span aria-hidden="true">···</span></button>
            <div v-if="menuOpen" :id="menuId" ref="menu" class="visualize-menu" role="menu" :aria-label="t('visualize.actions')" @keydown="onMenuKeydown">
              <span class="visualize-menu-name" :title="receipt.name">{{ receipt.name }}</span>
              <button type="button" role="menuitem" :disabled="loading || exportBusy" :title="t('visualize.reloadHint')" :aria-description="t('visualize.reloadHint')" @click="reloadFromMenu">{{ t('visualize.reload') }}</button>
              <button type="button" role="menuitem" :disabled="!canExport" @click="exportCanvas('copy')">{{ t('visualize.copyImage') }}</button>
              <button type="button" role="menuitem" :disabled="!canExport" @click="exportCanvas('download')">{{ t('visualize.downloadPng') }}</button>
              <button type="button" role="menuitem" :disabled="!canDownloadHtml" @click="exportCanvas('html')">{{ t('visualize.downloadHtml') }}</button>
            </div>
          </div>
          <button ref="expandButton" type="button" class="visualize-icon" :aria-expanded="expanded"
            :aria-label="t(expanded ? 'visualize.collapse' : 'visualize.expand')"
            :title="t(expanded ? 'visualize.collapse' : 'visualize.expand')" @click="toggleExpanded">
            <svg v-if="!expanded" viewBox="0 0 20 20" aria-hidden="true"><path d="M12 3h5v5M17 3l-6 6M8 17H3v-5M3 17l6-6" /></svg>
            <svg v-else viewBox="0 0 20 20" aria-hidden="true"><path d="M17 7h-4V3M13 7l5-5M3 13h4v4M7 13l-5 5" /></svg>
          </button>
        </div>
      </header>
      <div v-if="error" class="visualize-feedback" role="alert">
        <p>{{ t('visualize.error') }}</p><p class="visualize-error-detail">{{ error }}</p>
        <div class="visualize-feedback-actions">
          <button type="button" @click="reload">{{ t('visualize.reload') }}</button>
          <button v-if="canDraft" type="button" @click="draftRepair">{{ t('visualize.repair') }}</button>
        </div>
      </div>
      <p v-else-if="loading" class="visualize-feedback" role="status">{{ t('visualize.loading') }}</p>
      <p v-if="expandUnavailable" class="visualize-feedback" role="status">{{ t('visualize.expandUnavailable') }}</p>
      <div ref="container" class="visualize-surface" :aria-busy="loading" />
      <footer v-if="context" class="visualize-context">
        <p class="visualize-context-summary">{{ context.summary }}</p>
        <button type="button" :disabled="!canDraft || loading || Boolean(error)" @click="attachContext">{{ t('visualize.askWithContext') }}</button>
        <p v-if="contextAttached" class="visualize-context-attached" role="status">{{ t('visualize.contextAttached') }}</p>
        <p v-if="contextAttachFailed" class="visualize-context-attached" role="alert">{{ t('visualize.contextAttachFailed') }}</p>
      </footer>
      <footer v-if="proposal" class="visualize-proposal">
        <p>{{ proposal }}</p>
        <div class="visualize-feedback-actions">
          <button type="button" :disabled="!canDraft" @click="acceptProposal">{{ t('visualize.continue') }}</button>
          <button type="button" @click="proposal = ''">{{ t('visualize.dismiss') }}</button>
        </div>
      </footer>
      <p v-if="drafted" class="visualize-feedback" role="status">{{ t('visualize.drafted') }}</p>
    </dialog>
  </div>
</template>
<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex } from '@noble/hashes/utils.js'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import { downloadBlob } from '@/utils/browser'
import { abortableClipboardPreparation, assertClipboardPreparationActive, ImageClipboardError, IMAGE_CLIPBOARD_PREPARATION_TIMEOUT_MS, writePreparedClipboard } from '@/utils/imageClipboard'
import { getManifest } from '@/themes/registry'
import { VISUALIZE_CONVERSATION_KEY, type VisualizeContext, type VisualizeReceipt, type VisualizeRuntime } from '@/visualize/types'
import { VISUALIZE_MESSAGE_PIN_KEY } from '@/visualize/presentation'
import { readVisualizeState, saveVisualizeState } from '@/visualize/state'
import { normalizeVisualizeContext, readVisualizeContext, saveVisualizeContext } from '@/visualize/context'
const props = defineProps<{ receipt: VisualizeReceipt; sessionKey: string; sampleHtml?: string }>()
const { t } = useI18n()
const { pushToast } = useToasts()
const content = inject(ARTIFACT_WORKBENCH_KEY, null)?.content
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const conversation = inject(VISUALIZE_CONVERSATION_KEY, null)
const messagePins = inject(VISUALIZE_MESSAGE_PIN_KEY, null)
const canvas = ref<HTMLDialogElement | null>(null)
const expandButton = ref<HTMLButtonElement | null>(null)
const menuButton = ref<HTMLButtonElement | null>(null)
const menuAnchor = ref<HTMLElement | null>(null)
const menu = ref<HTMLElement | null>(null)
const menuId = `visualize-actions-${useId()}`
const container = ref<HTMLElement | null>(null)
const loading = ref(true)
const error = ref('')
const proposal = ref('')
const context = shallowRef<VisualizeContext | null>(null)
const contextAttached = ref(false)
const contextAttachFailed = ref(false)
const drafted = ref(false)
const expanded = ref(false)
const menuOpen = ref(false)
const exportBusy = ref(false)
const exportFeedback = ref('')
const exportFailed = ref(false)
const expandUnavailable = ref(false)
const inlineHeight = ref(120)
const isTopmost = useDialogLayer(expanded)
// A revision changes after edit_widget, but saved interaction state belongs to
// the widget/session rather than to one immutable source revision.
const widgetIdentity = computed(() => props.receipt.widgetId || props.receipt.documentId)
const scope = computed(() => JSON.stringify([gateway?.deliveryIdentity, props.sessionKey, widgetIdentity.value]))
const receiptVersion = computed(() => JSON.stringify([props.receipt.documentId, props.receipt.revisionId, props.receipt.sha256, props.receipt.size, props.receipt.version]))
const canDraft = computed(() => conversation?.enabled(props.sessionKey) ?? false)
const canExport = computed(() => !loading.value && !error.value && !exportBusy.value)
const canDownloadHtml = computed(() => canExport.value && Boolean(props.sampleHtml !== undefined || (props.sessionKey && gateway?.isAvailable && content?.fetchVisualize)))
let runtime: VisualizeRuntime | null = null
let request: AbortController | null = null
let observer: MutationObserver | null = null
let releasePin: (() => void) | undefined
let restoreScroll: (() => void) | undefined
let alive = false
let exportRequest: AbortController | null = null
let contextEncoding = 'null'
let contextFeedbackTimer: ReturnType<typeof setTimeout> | undefined
const theme = (): 'light' | 'dark' => {
  const themeId = container.value?.closest<HTMLElement>('[data-theme]')?.dataset.theme ?? document.documentElement.dataset.theme ?? ''
  const declared = getManifest(themeId)?.capabilities.colorScheme
  if (declared === 'light' || declared === 'dark') return declared
  const schemes = getComputedStyle(container.value ?? document.documentElement).colorScheme.split(/\s+/)
  if (schemes.includes('light') && !schemes.includes('dark')) return 'light'
  if (schemes.includes('dark') && !schemes.includes('light')) return 'dark'
  const explicit = container.value?.closest<HTMLElement>('.light, .dark')
  if (explicit) return explicit.classList.contains('dark') ? 'dark' : 'light'
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}
const diagnostic = (cause: unknown) => (cause instanceof Error ? cause.message : String((cause as { message?: string })?.message ?? cause)).slice(0, 1000)

function updatePresentation() {
  const viewportHeight = window.visualViewport?.height ?? window.innerHeight
  const extra = [...(canvas.value?.children ?? [])].filter(node => node !== container.value)
    .reduce((height, node) => height + node.getBoundingClientRect().height, 0)
  runtime?.setPresentation({ mode: expanded.value ? 'expanded' : 'inline', availableHeight: Math.max(160, viewportHeight - extra - 48) })
}

function lockScroll() {
  const elements = new Set<HTMLElement>([document.body])
  for (let node = canvas.value?.parentElement; node; node = node.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(node).overflowY)) elements.add(node)
  }
  const saved = [...elements].map(element => ({ element, overflow: element.style.overflow, top: element.scrollTop, left: element.scrollLeft }))
  saved.forEach(({ element }) => { element.style.overflow = 'hidden' })
  return () => saved.forEach(({ element, overflow, top, left }) => {
    element.style.overflow = overflow; element.scrollTop = top; element.scrollLeft = left
  })
}

async function toggleExpanded() {
  if (expanded.value) { collapse(); return }
  const element = canvas.value
  if (!element || typeof element.showModal !== 'function') { expandUnavailable.value = true; return }
  menuOpen.value = false
  inlineHeight.value = Math.max(120, element.getBoundingClientRect().height)
  releasePin = messagePins?.pin(element)
  restoreScroll = lockScroll()
  // Native top layer escapes clipped chat rows without moving the iframe.
  element.close()
  try { element.showModal() } catch {
    element.setAttribute('open', '')
    restoreScroll?.(); restoreScroll = undefined; releasePin?.(); releasePin = undefined
    expandUnavailable.value = true
    return
  }
  expanded.value = true
  expandUnavailable.value = false
  await nextTick()
  if (!alive || !expanded.value) return
  updatePresentation()
  expandButton.value?.focus({ preventScroll: true })
}

function collapse(restoreFocus = true) {
  closeMenu(false)
  if (!expanded.value) return
  canvas.value?.close()
  canvas.value?.setAttribute('open', '')
  expanded.value = false
  restoreScroll?.(); restoreScroll = undefined
  releasePin?.(); releasePin = undefined
  void nextTick(() => {
    if (!alive || expanded.value) return
    updatePresentation()
    if (restoreFocus && expandButton.value?.isConnected) expandButton.value.focus({ preventScroll: true })
  })
}

function onEscape() {
  if (menuOpen.value) { closeMenu(true); return }
  if (isTopmost.value) collapse()
}
function closeMenu(restoreFocus = false) {
  menuOpen.value = false
  if (restoreFocus && menuButton.value?.isConnected) menuButton.value.focus({ preventScroll: true })
}
function toggleMenu() { if (menuOpen.value) closeMenu(true); else void openMenu() }
async function openMenu(first?: boolean) {
  menuOpen.value = true
  if (first === undefined) return
  await nextTick()
  const items = menu.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')
  if (items?.length) items[first ? 0 : items.length - 1]?.focus({ preventScroll: true })
}
function onMenuKeydown(event: KeyboardEvent) {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  const items = [...(menu.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])]
  if (!items.length) return
  event.preventDefault()
  const current = items.indexOf(document.activeElement as HTMLButtonElement)
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
    : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
  items[index]?.focus({ preventScroll: true })
}
function onMenuFocusOut(event: FocusEvent) {
  if (event.relatedTarget instanceof Node && !menuAnchor.value?.contains(event.relatedTarget)) closeMenu(false)
}
function closeMenuOutside(event: PointerEvent) {
  if (menuOpen.value && event.target instanceof Node && !menuAnchor.value?.contains(event.target)) closeMenu(false)
}
function closeMenuOnFrameFocus() {
  // Pointer/focus events do not bubble out of an opaque iframe. Also dismiss
  // when focus leaves this browser window, without stealing the next focus.
  closeMenu(false)
}
function cancelExport() {
  exportRequest?.abort(); exportRequest = null; exportBusy.value = false
  exportFeedback.value = ''; exportFailed.value = false
}
async function exportCanvas(mode: 'copy' | 'download' | 'html') {
  if (!canExport.value || !runtime) return
  const mounted = runtime
  const expectedScope = scope.value
  const expectedReceiptVersion = receiptVersion.value
  const controller = new AbortController(); exportRequest = controller
  const current = () => alive && !controller.signal.aborted && scope.value === expectedScope
    && receiptVersion.value === expectedReceiptVersion && runtime === mounted
  const assertCurrent = () => {
    assertClipboardPreparationActive(controller.signal)
    if (!current()) throw new DOMException('Cancelled', 'AbortError')
  }
  const baseName = (props.receipt.name.split(/[/\\]/).pop() ?? 'visualization').replace(/\.[^.]*$/, '').replace(/[<>:"|?*\u0000-\u001f]/g, '_') || 'visualization'
  const filename = mode === 'html' ? `${baseName}.html` : `${baseName}.png`
  const timeout = setTimeout(() => controller.abort(new ImageClipboardError('timedOut')), IMAGE_CLIPBOARD_PREPARATION_TIMEOUT_MS)
  exportBusy.value = true; exportFeedback.value = ''; exportFailed.value = false
  closeMenu(true)
  let preparationError: unknown
  const prepare = async () => {
    try {
      assertCurrent()
      const blob = await abortableClipboardPreparation(mounted.capture({ signal: controller.signal }), controller.signal)
      assertCurrent()
      // capture() validates PNG pixels and bounds; the HTML receipt is never
      // used as a clipboard image descriptor or rasterized a second time.
      return blob
    } catch (cause) { preparationError = cause; throw cause }
  }
  try {
    if (mode === 'html') {
      assertCurrent()
      let blob: Blob
      if (props.sampleHtml !== undefined) {
        blob = new Blob([props.sampleHtml], { type: 'text/html;charset=utf-8' })
      } else {
        if (!props.sessionKey || !gateway?.isAvailable || !content?.fetchVisualize) throw new Error(t('visualize.unavailable'))
        blob = await content.fetchVisualize({ sessionKey: props.sessionKey, documentId: props.receipt.documentId, revisionId: props.receipt.revisionId, signal: controller.signal, maxBytes: 1024 * 1024 })
        const bytes = new Uint8Array(await blob.arrayBuffer())
        if (bytes.length !== props.receipt.size || bytesToHex(sha256(bytes)) !== props.receipt.sha256) throw new Error(t('visualize.integrity'))
        blob = new Blob([bytes], { type: 'text/html;charset=utf-8' })
      }
      assertCurrent(); downloadBlob(blob, filename)
    } else if (mode === 'copy') {
      // Invoke while still inside the user's click. ClipboardItem owns the
      // preparation promise, preserving activation in Safari and mobile Web.
      await abortableClipboardPreparation(writePreparedClipboard('image', prepare), controller.signal)
    } else {
      const png = await prepare()
      assertCurrent(); downloadBlob(png, filename)
    }
    assertCurrent()
    exportFeedback.value = t(mode === 'copy' ? 'imageClipboard.copiedImage' : 'visualize.downloadStarted')
    pushToast(exportFeedback.value, { tone: 'ok' })
  } catch (cause) {
    const failure = preparationError ?? cause
    if (alive && scope.value === expectedScope && exportRequest === controller
      && !(failure instanceof DOMException && failure.name === 'AbortError')) {
      exportFailed.value = true
      exportFeedback.value = t(failure instanceof ImageClipboardError && failure.kind === 'unsupported'
        ? 'visualize.copyUnsupported' : mode === 'copy' ? 'visualize.copyFailed' : 'visualize.downloadFailed')
      pushToast(exportFeedback.value, { tone: 'danger' })
    }
  } finally {
    clearTimeout(timeout); controller.abort()
    if (exportRequest === controller) { exportRequest = null; exportBusy.value = false }
  }
}

async function reload() {
  closeMenu(false); cancelExport()
  request?.abort(); runtime?.dispose(); runtime = null
  const controller = new AbortController(); request = controller
  const expectedScope = scope.value
  const current = () => alive && !controller.signal.aborted && expectedScope === scope.value
  loading.value = true; error.value = ''; proposal.value = ''; drafted.value = false
  clearContextAttachmentFeedback()
  context.value = readVisualizeContext(expectedScope); contextAttachFailed.value = false
  contextEncoding = JSON.stringify(context.value)
  try {
    if (!container.value) return
    let html = props.sampleHtml
    if (html === undefined) {
      if (!props.sessionKey || !gateway?.isAvailable || !content?.fetchVisualize) throw new Error(t('visualize.unavailable'))
      const blob = await content.fetchVisualize({ sessionKey: props.sessionKey, documentId: props.receipt.documentId, revisionId: props.receipt.revisionId, signal: controller.signal, maxBytes: 1024 * 1024 })
      const bytes = new Uint8Array(await blob.arrayBuffer())
      if (bytes.length !== props.receipt.size || bytesToHex(sha256(bytes)) !== props.receipt.sha256) throw new Error(t('visualize.integrity'))
      html = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
    }
    if (!current()) return
    const { mountVisualize } = await import('@/visualize/runtime')
    if (!current()) return
    const mounted = await mountVisualize(container.value, { html, signal: controller.signal, initialState: readVisualizeState(expectedScope), theme: theme(),
      onPrompt(text) { if (current() && typeof text === 'string') { proposal.value = text.trim().slice(0, 4000); drafted.value = false } },
      onState(state) { if (current()) saveVisualizeState(expectedScope, state) },
      onContext(value) {
        if (!current()) return false
        const normalized = normalizeVisualizeContext(value)
        const encoded = JSON.stringify(normalized)
        // Generated controls may publish on every render. Do not rewrite
        // storage or erase an attachment acknowledgement for unchanged data.
        if (normalized && encoded === contextEncoding) return true
        saveVisualizeContext(expectedScope, normalized)
        contextEncoding = encoded
        clearContextAttachmentFeedback()
        context.value = normalized; contextAttachFailed.value = false
        return true
      },
      onError(cause) { if (current()) error.value = diagnostic(cause) },
      onDismiss() {
        if (current()) onEscape()
        return expanded.value ? 'fullscreen' : 'inline'
      },
      onPointerDown() { if (current()) closeMenu(false) },
    })
    if (!current()) { mounted.dispose(); return }
    runtime = mounted
    updatePresentation()
  } catch (cause) {
    if (current()) error.value = diagnostic(cause)
  } finally { if (current()) loading.value = false }
}
function reloadFromMenu() { closeMenu(true); void reload() }
function clearContextAttachmentFeedback() {
  if (contextFeedbackTimer !== undefined) clearTimeout(contextFeedbackTimer)
  contextFeedbackTimer = undefined
  contextAttached.value = false
}
function attachContext() {
  if (!context.value || !canDraft.value || loading.value || error.value) return
  const snapshot = normalizeVisualizeContext(context.value)
  clearContextAttachmentFeedback()
  try {
    contextAttached.value = Boolean(snapshot && conversation?.attach(props.sessionKey, props.receipt, snapshot))
    contextAttachFailed.value = !contextAttached.value
    if (contextAttached.value) {
      // The composer owns the actual chip lifecycle; this is a brief receipt
      // of the click, not a permanent claim that a chip is still attached.
      contextFeedbackTimer = setTimeout(clearContextAttachmentFeedback, 2000)
    }
  } catch { contextAttachFailed.value = true }
}
function acceptProposal() {
  if (!canDraft.value || !proposal.value) return
  if (conversation?.draft(props.sessionKey, props.receipt, proposal.value)) { proposal.value = ''; drafted.value = true }
}
function draftRepair() {
  if (!canDraft.value || !error.value) return
  if (conversation?.draft(props.sessionKey, props.receipt, t('visualize.repairPrompt', { name: props.receipt.name, error: error.value }))) drafted.value = true
}
onMounted(() => {
  alive = true; void reload()
  observer = new MutationObserver(() => runtime?.updateTheme(theme()))
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'style'] })
  window.addEventListener('resize', updatePresentation)
  document.addEventListener('pointerdown', closeMenuOutside)
  window.addEventListener('blur', closeMenuOnFrameFocus)
  window.visualViewport?.addEventListener('resize', updatePresentation)
})
watch([proposal, context, contextAttached, contextAttachFailed, error, loading], () => { void nextTick(updatePresentation) })
watch(scope, () => { collapse(false); if (alive) void reload() })
// Keep one Canvas instance when edit_widget advances the source revision. The
// stable scope preserves saved state; this watcher refreshes only guest code.
watch(receiptVersion, () => { if (alive) void reload() })
onBeforeUnmount(() => {
  clearContextAttachmentFeedback()
  collapse(false); alive = false; cancelExport(); request?.abort(); runtime?.dispose(); observer?.disconnect()
  window.removeEventListener('resize', updatePresentation)
  document.removeEventListener('pointerdown', closeMenuOutside)
  window.removeEventListener('blur', closeMenuOnFrameFocus)
  window.visualViewport?.removeEventListener('resize', updatePresentation)
})
</script>
<style scoped>
.visualize-position { margin: var(--sp-4) 0; min-width: 0; }
.visualize-canvas { position: relative; display: block; width: 100%; max-width: none; max-height: none; min-width: 0; margin: 0; padding: 0; border: 1px solid transparent; border-radius: var(--radius-lg); overflow: visible; color: var(--text); background: transparent; }
.visualize-canvas:hover, .visualize-canvas:focus-within { border-color: var(--border); }
.visualize-toolbar { display: flex; align-items: center; justify-content: flex-end; min-height: 32px; gap: var(--sp-2); padding: 0 var(--sp-2); color: var(--text-muted); font-size: var(--fs-sm); }
.visualize-title { margin-right: auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.visualize-actions, .visualize-feedback-actions { display: flex; flex-shrink: 0; gap: var(--sp-2); flex-wrap: wrap; }
.visualize-feedback-actions button { min-height: 44px; }
button { border: 1px solid var(--border); border-radius: var(--radius-control); color: var(--text); background: var(--bg-hover); padding: var(--sp-1) var(--sp-2); cursor: pointer; }
button:disabled { opacity: 0.5; cursor: default; }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.visualize-icon { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; padding: 0; border-color: transparent; background: transparent; color: var(--text-muted); }
.visualize-icon:hover, .visualize-icon[aria-expanded="true"] { background: var(--bg-hover); color: var(--text); }
.visualize-icon svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; }
.visualize-icon span { font-size: var(--fs-xl); line-height: 1; }
.visualize-menu-anchor { position: relative; }
.visualize-menu { position: absolute; z-index: var(--z-dropdown, 50); top: calc(100% + var(--sp-1)); right: 0; width: 260px; max-width: calc(100vw - var(--sp-6)); padding: var(--sp-1); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); color: var(--text); background: var(--bg-surface); font-size: var(--fs-sm); }
.visualize-menu-name { display: block; min-width: 0; padding: var(--sp-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }
.visualize-menu button { display: block; width: 100%; min-height: 44px; padding: var(--sp-2); border-color: transparent; background: transparent; text-align: left; }
.visualize-menu button:hover:not(:disabled), .visualize-menu button:focus-visible { background: var(--bg-hover); }
.visualize-surface { width: 100%; min-height: 120px; overflow: hidden; border-radius: 0 0 var(--radius-lg) var(--radius-lg); }
.visualize-surface :deep(iframe) { display: block; width: 100%; border: 0; }
.visualize-proposal, .visualize-feedback { margin: 0; padding: var(--sp-3); overflow-wrap: anywhere; }
.visualize-feedback p { margin: 0 0 var(--sp-2); }
.visualize-error-detail { max-height: 120px; overflow: auto; color: var(--text-muted); font-size: var(--fs-sm); }
.visualize-proposal { border-top: 1px solid var(--border); padding-bottom: calc(var(--sp-3) + env(safe-area-inset-bottom, 0px)); }
.visualize-proposal > p { margin: 0 0 var(--sp-2); max-height: min(28dvh, 220px); overflow-y: auto; overscroll-behavior: contain; }
.visualize-context { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); padding: var(--sp-3); border-top: 1px solid var(--border); }
.visualize-context-summary { flex: 1 1 240px; min-width: 0; max-height: min(20dvh, 160px); margin: 0; overflow: auto; overflow-wrap: anywhere; }
.visualize-context button { min-height: 44px; }
.visualize-context-attached { width: 100%; margin: 0; color: var(--text-muted); font-size: var(--fs-sm); }
.visualize-export-feedback { min-width: 0; margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font-size: var(--fs-sm); }
.visualize-export-feedback--error { color: var(--danger, var(--text)); }
.visualize-canvas--expanded { position: fixed; inset: var(--sp-3); width: calc(100% - var(--sp-3) * 2); max-width: 1440px; max-height: calc(100dvh - var(--sp-3) * 2); margin: auto; overflow: auto; overscroll-behavior: contain; border-color: var(--border); background: var(--bg-surface); }
.visualize-canvas--expanded::backdrop { background: color-mix(in srgb, var(--text) 35%, transparent); }
@media (hover: hover) and (pointer: fine) {
  .visualize-canvas:not(.visualize-canvas--expanded) .visualize-actions { opacity: 0; }
  .visualize-canvas:hover .visualize-actions, .visualize-canvas:focus-within .visualize-actions { opacity: 1; }
}
@media (pointer: coarse) { .visualize-icon { width: 44px; height: 44px; } }
@media (max-width: 480px) { .visualize-proposal .visualize-feedback-actions button { flex: 1 1 0; min-width: 0; } }
@media (max-width: 480px) { .visualize-context button { width: 100%; } }
</style>
