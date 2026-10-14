<template>
  <div class="visualize-position" :style="expanded ? { height: `${inlineHeight}px` } : undefined">
    <dialog ref="canvas" open class="visualize-canvas" :class="{ 'visualize-canvas--expanded': expanded }"
      :role="expanded ? 'dialog' : 'group'" :aria-modal="expanded || undefined" :aria-label="receipt.name"
      data-visualize-canvas @cancel.prevent="collapse()" @keydown.esc.stop.prevent="onEscape">
      <header class="visualize-toolbar">
        <span v-if="expanded" class="visualize-title">{{ receipt.name }}</span>
        <div class="visualize-actions">
          <button type="button" class="visualize-icon" :aria-label="t('visualize.actions')" :title="t('visualize.actions')"
            :aria-expanded="menuOpen" @click="menuOpen = !menuOpen"><span aria-hidden="true">···</span></button>
          <button ref="expandButton" type="button" class="visualize-icon" :aria-expanded="expanded"
            :aria-label="t(expanded ? 'visualize.collapse' : 'visualize.expand')"
            :title="t(expanded ? 'visualize.collapse' : 'visualize.expand')" @click="toggleExpanded">
            <svg v-if="!expanded" viewBox="0 0 20 20" aria-hidden="true"><path d="M12 3h5v5M17 3l-6 6M8 17H3v-5M3 17l6-6" /></svg>
            <svg v-else viewBox="0 0 20 20" aria-hidden="true"><path d="M17 7h-4V3M13 7l5-5M3 13h4v4M7 13l-5 5" /></svg>
          </button>
        </div>
      </header>
      <div v-if="menuOpen" class="visualize-menu">
        <span>{{ receipt.name }}</span>
        <button type="button" @click="reloadFromMenu">{{ t('visualize.reload') }}</button>
      </div>
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
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex } from '@noble/hashes/utils.js'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { getManifest } from '@/themes/registry'
import { VISUALIZE_CONVERSATION_KEY, type VisualizeReceipt, type VisualizeRuntime } from '@/visualize/types'
import { VISUALIZE_MESSAGE_PIN_KEY } from '@/visualize/presentation'
import { readVisualizeState, saveVisualizeState } from '@/visualize/state'
const props = defineProps<{ receipt: VisualizeReceipt; sessionKey: string; sampleHtml?: string }>()
const { t } = useI18n()
const content = inject(ARTIFACT_WORKBENCH_KEY, null)?.content
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const conversation = inject(VISUALIZE_CONVERSATION_KEY, null)
const messagePins = inject(VISUALIZE_MESSAGE_PIN_KEY, null)
const canvas = ref<HTMLDialogElement | null>(null)
const expandButton = ref<HTMLButtonElement | null>(null)
const container = ref<HTMLElement | null>(null)
const loading = ref(true)
const error = ref('')
const proposal = ref('')
const drafted = ref(false)
const expanded = ref(false)
const menuOpen = ref(false)
const expandUnavailable = ref(false)
const inlineHeight = ref(120)
const isTopmost = useDialogLayer(expanded)
const scope = computed(() => JSON.stringify([gateway?.deliveryIdentity, props.sessionKey, props.receipt.documentId, props.receipt.revisionId, props.receipt.callId]))
const canDraft = computed(() => conversation?.enabled(props.sessionKey) ?? false)
let runtime: VisualizeRuntime | null = null
let request: AbortController | null = null
let observer: MutationObserver | null = null
let releasePin: (() => void) | undefined
let restoreScroll: (() => void) | undefined
let alive = false
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
  if (menuOpen.value) { menuOpen.value = false; return }
  if (isTopmost.value) collapse()
}
function closeMenuOutside(event: PointerEvent) {
  if (menuOpen.value && event.target instanceof Node && !canvas.value?.contains(event.target)) menuOpen.value = false
}

async function reload() {
  request?.abort(); runtime?.dispose(); runtime = null
  const controller = new AbortController(); request = controller
  const expectedScope = scope.value
  const current = () => alive && !controller.signal.aborted && expectedScope === scope.value
  loading.value = true; error.value = ''; proposal.value = ''; drafted.value = false
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
      onError(cause) { if (current()) error.value = diagnostic(cause) },
    })
    if (!current()) { mounted.dispose(); return }
    runtime = mounted
    updatePresentation()
  } catch (cause) {
    if (current()) error.value = diagnostic(cause)
  } finally { if (current()) loading.value = false }
}
function reloadFromMenu() { menuOpen.value = false; void reload() }
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
  window.visualViewport?.addEventListener('resize', updatePresentation)
})
watch([proposal, error, loading, menuOpen], () => { void nextTick(updatePresentation) })
watch(scope, () => { collapse(false); if (alive) void reload() })
onBeforeUnmount(() => {
  collapse(false); alive = false; request?.abort(); runtime?.dispose(); observer?.disconnect()
  window.removeEventListener('resize', updatePresentation)
  document.removeEventListener('pointerdown', closeMenuOutside)
  window.visualViewport?.removeEventListener('resize', updatePresentation)
})
</script>
<style scoped>
.visualize-position { margin: var(--sp-4) 0; min-width: 0; }
.visualize-canvas { position: relative; display: block; width: 100%; max-width: none; max-height: none; min-width: 0; margin: 0; padding: 0; border: 1px solid transparent; border-radius: var(--radius-lg); overflow: hidden; color: var(--text); background: transparent; }
.visualize-canvas:hover, .visualize-canvas:focus-within { border-color: var(--border); }
.visualize-toolbar { display: flex; align-items: center; justify-content: flex-end; min-height: 32px; gap: var(--sp-2); padding: 0 var(--sp-2); color: var(--text-muted); font-size: var(--fs-sm); }
.visualize-title { margin-right: auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.visualize-actions, .visualize-feedback-actions { display: flex; flex-shrink: 0; gap: var(--sp-2); flex-wrap: wrap; }
button { border: 1px solid var(--border); border-radius: var(--radius-control); color: var(--text); background: var(--bg-hover); padding: var(--sp-1) var(--sp-2); cursor: pointer; }
button:disabled { opacity: 0.5; cursor: default; }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.visualize-icon { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 32px; padding: 0; border-color: transparent; background: transparent; color: var(--text-muted); }
.visualize-icon:hover, .visualize-icon[aria-expanded="true"] { background: var(--bg-hover); color: var(--text); }
.visualize-icon svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linecap: round; stroke-linejoin: round; }
.visualize-icon span { font-size: var(--fs-xl); line-height: 1; }
.visualize-menu { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); border-bottom: 1px solid var(--border); color: var(--text-muted); font-size: var(--fs-sm); }
.visualize-menu span { overflow-wrap: anywhere; min-width: 0; }
.visualize-surface { width: 100%; min-height: 120px; }
.visualize-surface :deep(iframe) { display: block; width: 100%; border: 0; }
.visualize-proposal, .visualize-feedback { margin: 0; padding: var(--sp-3); overflow-wrap: anywhere; }
.visualize-feedback p { margin: 0 0 var(--sp-2); }
.visualize-error-detail { max-height: 120px; overflow: auto; color: var(--text-muted); font-size: var(--fs-sm); }
.visualize-proposal { border-top: 1px solid var(--border); }
.visualize-proposal > p { margin: 0 0 var(--sp-2); }
.visualize-canvas--expanded { position: fixed; inset: var(--sp-3); width: calc(100% - var(--sp-3) * 2); max-width: 1440px; max-height: calc(100dvh - var(--sp-3) * 2); margin: auto; overflow: auto; overscroll-behavior: contain; border-color: var(--border); background: var(--bg-surface); }
.visualize-canvas--expanded::backdrop { background: color-mix(in srgb, var(--text) 35%, transparent); }
@media (hover: hover) and (pointer: fine) {
  .visualize-canvas:not(.visualize-canvas--expanded) .visualize-actions { opacity: 0; }
  .visualize-canvas:hover .visualize-actions, .visualize-canvas:focus-within .visualize-actions { opacity: 1; }
}
@media (pointer: coarse) { .visualize-icon { width: 44px; height: 44px; } }
</style>
