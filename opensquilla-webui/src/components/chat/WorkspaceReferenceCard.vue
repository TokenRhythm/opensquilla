<template>
  <div class="workspace-reference" data-testid="workspace-reference-card">
    <div class="workspace-reference__row">
      <button class="workspace-reference__open" type="button" :disabled="busy || blocked"
        :aria-label="t('workspaceReference.open', { path: label })" @click="open">
        <Icon name="fileText" :size="17" aria-hidden="true" />
        <span :title="label">{{ label }}</span>
        <Icon name="chevronRight" :size="15" aria-hidden="true" />
      </button>
      <details class="workspace-reference__menu" @keydown.esc.prevent.stop="closeMenu($event)">
        <summary :aria-label="t('workspaceReference.actions')" :title="t('workspaceReference.actions')">
          <Icon name="moreHorizontal" :size="16" aria-hidden="true" />
        </summary>
        <div class="workspace-reference__options">
          <button type="button" @click="copy(reference.locator.relativePath, $event)">{{ t('workspaceReference.copyPath') }}</button>
          <button type="button" @click="copy(label, $event)">{{ t('workspaceReference.copyLocation') }}</button>
        </div>
      </details>
    </div>
    <p v-if="busy || errorKey" role="status" aria-live="polite" class="workspace-reference__status">
      {{ t(busy ? 'workspaceReference.loading' : errorKey) }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, inject, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { WORKSPACE_REFERENCES_KEY, workspaceReferenceErrorKey } from '@/modules/workspaceReferences'
import { WORKSPACE_FILES_KEY } from '@/modules/workspaceFiles'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { CONVERSATION_WORKBENCH_OWNER_KEY } from '@/modules/conversationPolicy'
import type { WorkspaceFileReferenceV1 } from '@/types/references'
import { copyTextWithFallback } from '@/utils/browser'
import { useWorkbenchStore } from '@/workbench/store'
import { createResolvedWorkspaceFileItem, createWorkspaceFileItem } from '@/workbench/workspaceFileItems'

const props = defineProps<{ reference: WorkspaceFileReferenceV1; sessionKey: string }>()
const { t } = useI18n()
const access = inject(WORKSPACE_REFERENCES_KEY, null)
const files = inject(WORKSPACE_FILES_KEY, null)
const gateway = inject(GATEWAY_ACCESS_KEY, null)
const workbenchOwner = inject(CONVERSATION_WORKBENCH_OWNER_KEY, null)
const referenceGatewayEndpoint = gateway?.loadConnectionEndpoint() ?? null
const store = useWorkbenchStore()
const busy = ref(false)
const errorKey = ref('')
const blocked = computed(() => props.reference.state?.available === false
  || props.reference.capabilities.open === false
  || Boolean(props.reference.scope.gatewayInstanceId)
  || !access
  || !props.sessionKey
  || Boolean(gateway && (
    !gateway.isAvailable
    || !gateway.isLocalOwner
    || gateway.loadConnectionEndpoint() !== referenceGatewayEndpoint
  )))
const label = computed(() => {
  const { relativePath, startLine, endLine } = props.reference.locator
  return relativePath + (startLine ? `:${startLine}${endLine && endLine !== startLine ? `-${endLine}` : ''}` : '')
})
let pending: AbortController | null = null
function cancel() { pending?.abort(); pending = null; busy.value = false; errorKey.value = '' }
// Streaming may recreate the same reference object for each token. Cancel
// only when the resource identity, permissions, or Gateway context changes.
watch([
  () => props.sessionKey,
  () => workbenchOwner?.value,
  () => props.reference.id,
  () => props.reference.scope.sessionKey,
  () => props.reference.scope.workspaceId,
  () => props.reference.scope.gatewayInstanceId,
  () => props.reference.locator.relativePath,
  () => props.reference.locator.startLine,
  () => props.reference.locator.endLine,
  () => props.reference.state?.revision,
  () => props.reference.state?.available,
  () => props.reference.capabilities.open,
  () => gateway?.loadConnectionEndpoint(),
  () => gateway?.subscriptionEpoch,
  () => gateway?.isAvailable,
  () => gateway?.isLocalOwner,
], cancel, { flush: 'sync' })
onBeforeUnmount(cancel)

async function open() {
  if (!access || blocked.value || busy.value) return
  const attempt = new AbortController()
  pending?.abort()
  pending = attempt
  busy.value = true
  errorKey.value = ''
  const sessionKey = props.sessionKey
  const ownerSessionKey = workbenchOwner?.value || sessionKey
  const activeWorkbenchSession = store.activeSessionId
  const gatewayEpoch = gateway?.subscriptionEpoch
  const isCurrent = () => !(
      attempt.signal.aborted
      || pending !== attempt
      || sessionKey !== props.sessionKey
      || ownerSessionKey !== (workbenchOwner?.value || props.sessionKey)
      || activeWorkbenchSession !== store.activeSessionId
      || blocked.value
      || gateway?.subscriptionEpoch !== gatewayEpoch
      || (gateway && gateway.loadConnectionEndpoint() !== referenceGatewayEndpoint)
    )
  try {
    if (files?.resolveReference && /\.(?:html?|docx|pptx|xlsx)$/i.test(props.reference.locator.relativePath)) {
      // The server validates the reference's session/workspace scope before
      // returning file metadata. Binary files do not pass through UTF-8.
      const matches = await files.resolveReference(sessionKey, props.reference, attempt.signal)
      if (!isCurrent()) return
      const file = matches.find(candidate => candidate.path === props.reference.locator.relativePath)
      if (file) {
        const item = createResolvedWorkspaceFileItem(sessionKey, file, ownerSessionKey)
        if (store.openItem(item) && store.activeItemId === item.id) store.requestReveal()
        return
      }
    }
    const snapshot = await access.read(sessionKey, props.reference, attempt.signal)
    if (!isCurrent()) return
    const item = createWorkspaceFileItem(sessionKey, snapshot.reference, ownerSessionKey)
    if (store.openItem(item) && store.activeItemId === item.id) store.requestReveal()
  } catch (error) {
    if (!attempt.signal.aborted) errorKey.value = workspaceReferenceErrorKey(error)
  } finally {
    if (pending === attempt) { busy.value = false; pending = null }
  }
}

async function copy(value: string, event: Event) {
  closeMenu(event)
  try {
    await copyTextWithFallback(value)
    errorKey.value = ''
  } catch {
    errorKey.value = 'workspaceReference.copyFailed'
  }
}

function closeMenu(event: Event) {
  const menu = (event.currentTarget as HTMLElement).closest('details')
  menu?.removeAttribute('open')
  menu?.querySelector('summary')?.focus()
}
</script>

<style scoped>
.workspace-reference { margin: var(--sp-2) 0; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-surface); }
.workspace-reference__row { display: flex; align-items: stretch; min-width: 0; }
.workspace-reference__open { display: flex; align-items: center; gap: var(--sp-2); flex: 1; min-width: 0; border: 0; background: transparent; color: var(--text); padding: var(--sp-3); font: inherit; font-size: var(--fs-sm); text-align: left; cursor: pointer; }
.workspace-reference__open > span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-reference__open:disabled { cursor: default; opacity: var(--state-disabled-opacity); }
.workspace-reference__open:hover:not(:disabled), summary:hover { background: var(--bg-hover); }
.workspace-reference__menu { position: relative; flex-shrink: 0; }
summary { display: flex; height: 100%; align-items: center; padding: 0 var(--sp-3); list-style: none; cursor: pointer; color: var(--text-muted); }
summary::-webkit-details-marker { display: none; }
.workspace-reference__options { position: absolute; right: 0; top: 100%; z-index: 5; display: flex; flex-direction: column; min-width: max-content; padding: var(--sp-1); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-surface); }
.workspace-reference__options button { padding: var(--sp-2) var(--sp-3); border: 0; background: transparent; font: inherit; font-size: var(--fs-sm); color: var(--text); text-align: left; cursor: pointer; }
.workspace-reference__options button:hover { background: var(--bg-hover); }
button:focus-visible, summary:focus-visible { outline: 2px solid var(--border-focus); outline-offset: 2px; }
.workspace-reference__status { margin: 0; padding: 0 var(--sp-3) var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); }
</style>
