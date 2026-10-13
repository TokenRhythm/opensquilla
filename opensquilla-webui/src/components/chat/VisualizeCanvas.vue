<template>
  <section class="visualize-canvas" :class="{ 'visualize-canvas--expanded': expanded }" :aria-label="receipt.name" data-visualize-canvas>
    <header class="visualize-toolbar">
      <span>{{ receipt.name }}</span>
      <div>
        <button type="button" @click="reload">{{ t('visualize.reload') }}</button>
        <button type="button" :aria-expanded="expanded" @click="expanded = !expanded">{{ t(expanded ? 'visualize.collapse' : 'visualize.expand') }}</button>
      </div>
    </header>
    <p v-if="error" role="alert">{{ t('visualize.error') }} {{ error }}</p>
    <p v-else-if="loading" role="status">{{ t('visualize.loading') }}</p>
    <div ref="container" class="visualize-surface" />
    <footer v-if="proposal" class="visualize-proposal">
      <p>{{ proposal }}</p>
      <button type="button" :disabled="!canDraft" @click="acceptProposal">{{ t('visualize.continue') }}</button>
      <button type="button" @click="proposal = ''">{{ t('visualize.dismiss') }}</button>
    </footer>
    <p v-if="drafted" role="status">{{ t('visualize.drafted') }}</p>
  </section>
</template>
<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { sha256 } from '@noble/hashes/sha2.js'
import { bytesToHex } from '@noble/hashes/utils.js'
import { ARTIFACT_WORKBENCH_KEY } from '@/modules/artifactWorkbench'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { VISUALIZE_CONVERSATION_KEY, type VisualizeReceipt, type VisualizeRuntime } from '@/visualize/types'
import { readVisualizeState, saveVisualizeState } from '@/visualize/state'
const props = defineProps<{ receipt: VisualizeReceipt; sessionKey: string; sampleHtml?: string }>()
const { t } = useI18n()
const content = inject(ARTIFACT_WORKBENCH_KEY, null)?.content
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const conversation = inject(VISUALIZE_CONVERSATION_KEY, null)
const container = ref<HTMLElement | null>(null)
const loading = ref(true)
const error = ref('')
const proposal = ref('')
const drafted = ref(false)
const expanded = ref(false)
const scope = computed(() => JSON.stringify([gateway?.deliveryIdentity, props.sessionKey, props.receipt.documentId, props.receipt.revisionId, props.receipt.callId]))
const canDraft = computed(() => conversation?.enabled(props.sessionKey) ?? false)
let runtime: VisualizeRuntime | null = null
let request: AbortController | null = null
let observer: MutationObserver | null = null
let alive = false
const theme = () => document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'

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
      onError(cause) { if (current()) error.value = cause instanceof Error ? cause.message : String((cause as { message?: string })?.message ?? cause).slice(0, 250) },
    })
    if (!current()) { mounted.dispose(); return }
    runtime = mounted
  } catch (cause) {
    if (current()) error.value = cause instanceof Error ? cause.message : String(cause)
  } finally { if (current()) loading.value = false }
}
function acceptProposal() {
  if (!canDraft.value || !proposal.value) return
  if (conversation?.draft(props.sessionKey, props.receipt, proposal.value)) { proposal.value = ''; drafted.value = true }
}
onMounted(() => {
  alive = true; void reload()
  observer = new MutationObserver(() => runtime?.updateTheme(theme()))
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
})
watch(scope, () => { if (alive) void reload() })
onBeforeUnmount(() => { alive = false; request?.abort(); runtime?.dispose(); observer?.disconnect() })
</script>
<style scoped>
.visualize-canvas { margin: var(--sp-4) 0; min-width: 0; border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; background: var(--bg-surface); }
.visualize-toolbar { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); padding: var(--sp-2) var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); }
.visualize-toolbar span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.visualize-toolbar div { display: flex; flex-shrink: 0; gap: var(--sp-2); }
button { border: 1px solid var(--border); border-radius: var(--radius-control); color: var(--text); background: var(--bg-hover); padding: var(--sp-1) var(--sp-2); cursor: pointer; }
button:disabled { opacity: 0.5; cursor: default; }
.visualize-surface { width: 100%; min-height: 120px; }
.visualize-surface :deep(iframe) { display: block; width: 100%; border: 0; }
.visualize-proposal, .visualize-canvas > p { padding: var(--sp-3); overflow-wrap: anywhere; }
.visualize-proposal { border-top: 1px solid var(--border); }
.visualize-proposal button { margin-right: var(--sp-2); }
.visualize-canvas--expanded { position: fixed; inset: var(--sp-3); z-index: 1000; overflow: auto; margin: 0; }
</style>
