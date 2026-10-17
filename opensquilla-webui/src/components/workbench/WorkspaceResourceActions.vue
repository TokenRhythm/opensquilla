<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { WORKSPACE_FILES_KEY, WorkspaceFilesChangedError, type WorkspaceFile, type WorkspaceRoot } from '@/modules/workspaceFiles'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { copyTextWithFallback, downloadBlob } from '@/utils/browser'
import { useDialogLayer } from '@/composables/useDialogA11y'
import { readWorkspaceAppPreference, rememberWorkspaceAppPreference } from '@/workbench/workspaceAppPreference'
import Icon from '@/components/Icon.vue'
import WorkspaceApplicationIcon from './WorkspaceApplicationIcon.vue'

const props = defineProps<{ sessionKey: string; file: WorkspaceFile | null; root?: WorkspaceRoot | null }>()
const { t } = useI18n()
const files = inject(WORKSPACE_FILES_KEY, null)
const gateway = inject(GATEWAY_ACCESS_KEY, null)
type MenuKind = 'apps' | 'more'
type CatalogState = 'loading' | 'ready' | 'unavailable' | 'error'
const activeMenu = ref<MenuKind | null>(null), busy = ref(false), error = ref('')
const catalogState = ref<CatalogState>('loading')
const catalogReason = ref('')
const apps = ref<Array<{ id: string; label: string }>>([])
const preferredAppId = ref<string | null>(null)
const appTrigger = ref<HTMLButtonElement | null>(null), moreTrigger = ref<HTMLButtonElement | null>(null)
const primary = ref<HTMLButtonElement | null>(null), menu = ref<HTMLElement | null>(null)
const position = ref({ left: '0px', top: '0px' })
const open = computed(() => activeMenu.value !== null)
const topmost = useDialogLayer(open)
const target = computed(() => props.file ?? props.root ?? null)
const isRoot = computed(() => !props.file && props.root?.kind === 'workspace-root')
const validTarget = computed(() => Boolean(files && props.sessionKey && target.value?.path && target.value.workspaceBinding))
const targetLabel = computed(() => t(isRoot.value ? 'workbench.workspaceFiles.workspaceRoot' : 'workbench.workspaceFiles.currentFile', { name: target.value?.name || '' }))
const connected = computed(() => !gateway || gateway.availability === 'available' && gateway.isAvailable
  && gateway.connectionHealth === 'healthy' && !gateway.isResuming
  && (!gateway.connectionPhase || gateway.connectionPhase === 'healthy'))
const mayOpenApps = computed(() => !gateway || gateway.canExecuteHost === true && gateway.isLocalOwner === true)
const selectedApp = computed(() => apps.value.find(app => app.id === preferredAppId.value)
  ?? apps.value.find(app => app.id === 'reveal') ?? apps.value[0] ?? null)
const canOpenApp = computed(() => mayOpenApps.value && catalogState.value === 'ready' && Boolean(selectedApp.value))
const primaryLabel = computed(() => {
  if (catalogState.value === 'loading') return t('workbench.workspaceFiles.checkingApplications')
  if (canOpenApp.value && selectedApp.value) {
    if (!isRoot.value) return appLabel(selectedApp.value)
    return ['reveal', 'default'].includes(selectedApp.value.id) ? t('workbench.workspaceFiles.openWorkspace')
      : t('workbench.workspaceFiles.openWorkspaceWith', { app: appLabel(selectedApp.value) })
  }
  return t(isRoot.value ? 'workbench.workspaceFiles.copyDirectoryPath' : 'chat.download')
})
const unavailableHint = computed(() => {
  if (!validTarget.value) return t('workbench.workspaceFiles.selectFile')
  if (!connected.value) return t('workbench.newPanel.requiresConnection')
  const key = catalogReason.value === 'remote-client' ? 'remoteApplicationsUnavailable'
    : catalogReason.value === 'host-permission-required' ? 'applicationsPermissionRequired'
      : catalogReason.value === 'desktop-unavailable' ? 'applicationsDesktopUnavailable' : 'applicationsUnavailable'
  return t(`workbench.workspaceFiles.${key}`)
})
const fileDownloadHint = computed(() => !isRoot.value
  ? ` ${t('workbench.workspaceFiles.downloadToOpenOnDevice')}` : '')
const primaryTitle = computed(() => canOpenApp.value
  ? `${isRoot.value ? primaryLabel.value : t('workbench.workspaceFiles.openWith', { app: primaryLabel.value })} — ${t('workbench.workspaceFiles.openOnHost')}${isRoot.value ? ` — ${props.root!.path}` : ''}`
  : catalogState.value === 'loading' ? primaryLabel.value
    : isRoot.value ? `${t('workbench.workspaceFiles.copyGatewayDirectoryPath')} — ${props.root!.path}. ${unavailableHint.value}` : `${unavailableHint.value}${fileDownloadHint.value}`)
let generation = 0, controller: AbortController | null = null
let invoker: HTMLButtonElement | null = null

function appLabel(app: { id: string; label: string }): string {
  return app.id === 'reveal' ? t('workbench.workspaceFiles.fileManager')
    : app.id === 'default' ? t('workbench.workspaceFiles.defaultApplication') : app.label
}
function close(restoreFocus = true) {
  activeMenu.value = null
  if (restoreFocus) (invoker?.isConnected ? invoker : primary.value)?.focus()
  invoker = null
}
function reset() {
  generation++; controller?.abort(); controller = null
  close(false); busy.value = false; error.value = ''; apps.value = []; catalogReason.value = ''
  preferredAppId.value = readWorkspaceAppPreference(gateway?.deliveryIdentity)
  catalogState.value = 'unavailable'
}
async function loadApps() {
  const current = ++generation
  controller?.abort(); controller = new AbortController()
  const signal = controller.signal
  apps.value = []; error.value = ''; catalogReason.value = ''
  const supportsNative = isRoot.value ? files?.rootApps && files.openRoot : files?.apps && files.openWith && props.file?.revision
  if (!validTarget.value || !connected.value || !supportsNative) {
    catalogState.value = 'unavailable'; return
  }
  if (!mayOpenApps.value) {
    catalogReason.value = 'host-permission-required'; catalogState.value = 'unavailable'; return
  }
  catalogState.value = 'loading'
  try {
    const catalog = isRoot.value ? await files!.rootApps!({ sessionKey: props.sessionKey }, signal)
      : await files!.apps!(props.sessionKey, signal)
    if (current !== generation) return
    apps.value = catalog.available ? catalog.apps : []
    catalogReason.value = catalog.reason || ''
    catalogState.value = apps.value.length ? 'ready' : 'unavailable'
  } catch {
    if (current === generation) catalogState.value = 'error'
  }
}
async function show(kind: MenuKind, focus: 'first' | 'last' = 'first') {
  if (!validTarget.value || busy.value) return
  if (activeMenu.value === kind) { close(); return }
  close(false)
  invoker = kind === 'apps' ? appTrigger.value : moreTrigger.value
  activeMenu.value = kind
  error.value = ''
  const rect = invoker?.getBoundingClientRect()
  await nextTick()
  if (activeMenu.value !== kind || !menu.value) return
  const width = menu.value.offsetWidth || 264, height = menu.value.offsetHeight || 240
  position.value = {
    left: `${Math.max(8, Math.min((rect?.right || 0) - width, innerWidth - width - 8))}px`,
    top: `${Math.max(8, Math.min((rect?.bottom || 0) + 6, innerHeight - height - 8))}px`,
  }
  const buttons = [...menu.value.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
  const selected = menu.value.querySelector<HTMLButtonElement>('[aria-checked="true"]:not(:disabled)')
  const target = focus === 'last' ? buttons[buttons.length - 1] : selected ?? buttons[0]
  if (target) target.focus()
  else menu.value.focus()
}
async function perform(action: string) {
  if (!validTarget.value || !files || !target.value || busy.value || action !== 'copy-path' && !connected.value) return
  if (action === 'download' && !props.file) return
  if (!['copy-path', 'download'].includes(action)
    && (!canOpenApp.value || !apps.value.some(app => app.id === action))) return
  const current = generation, sessionKey = props.sessionKey, file = props.file, root = props.root, currentTarget = target.value
  const signal = controller?.signal
  busy.value = true; error.value = ''
  try {
    if (action === 'copy-path') await copyTextWithFallback(currentTarget.path)
    else if (action === 'download' && file) {
      const blob = await files.read(sessionKey, file, signal)
      if (current !== generation) return
      downloadBlob(blob, file.name)
    } else if (file) {
      // The same authorized descriptor, including its pinned revision, goes
      // through Gateway openWith. Never pass a bare path to a native fallback.
      await files.openWith!(sessionKey, file, action, signal)
    } else if (root) {
      await files.openRoot!({ sessionKey }, root, action, signal)
    }
    if (current === generation) close()
  } catch (cause) {
    if (current === generation) error.value = t(cause instanceof WorkspaceFilesChangedError
      ? 'workbench.workspaceFiles.changed' : root && !file ? 'workbench.workspaceFiles.openWorkspaceFailed' : 'workbench.workspaceFiles.openFailed')
  } finally { if (current === generation) busy.value = false }
}
function selectApp(id: string) {
  if (!canOpenApp.value || busy.value || !apps.value.some(app => app.id === id)) return
  preferredAppId.value = id
  rememberWorkspaceAppPreference(gateway?.deliveryIdentity, id)
  void perform(id)
}
function performPrimary() {
  if (catalogState.value === 'loading') return
  invoker = primary.value
  void perform(canOpenApp.value ? selectedApp.value!.id : isRoot.value ? 'copy-path' : 'download')
}
function keydown(event: KeyboardEvent) {
  if (!topmost.value) return
  if (event.key === 'Escape' || event.key === 'Tab') {
    if (event.key === 'Escape') event.preventDefault()
    event.stopPropagation(); close(); return
  }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault(); event.stopPropagation()
  const buttons = [...menu.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || []]
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
  buttons[event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus()
}
watch(() => [props.sessionKey, props.file?.workspaceBinding, props.file?.path, props.file?.revision,
  props.root?.kind, props.root?.workspaceBinding, props.root?.path,
  gateway?.deliveryIdentity, gateway?.subscriptionEpoch, connected.value, mayOpenApps.value], () => {
  reset(); void loadApps()
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(reset)
</script>

<template>
  <div class="workspace-resource-actions">
    <div class="workspace-resource-actions__split" :aria-busy="catalogState === 'loading' || busy">
      <button ref="primary" type="button" class="workspace-resource-actions__primary" data-testid="workspace-open-primary"
        :disabled="!validTarget || (!connected && !isRoot) || busy || catalogState === 'loading'" :aria-label="primaryTitle" :title="primaryTitle" @click="performPrimary">
        <WorkspaceApplicationIcon v-if="canOpenApp && selectedApp" :app-id="selectedApp.id" />
        <Icon v-else :name="catalogState === 'loading' ? 'folder' : isRoot ? 'copy' : 'download'" :size="18" />
        <span class="workspace-resource-actions__primary-label">{{ primaryLabel }}</span>
      </button>
      <button v-if="canOpenApp || catalogState === 'loading'" ref="appTrigger" type="button" class="workspace-resource-actions__arrow"
        data-testid="workspace-open-app-menu" :disabled="!validTarget || !connected || busy" :aria-label="t('workbench.workspaceFiles.chooseApplication')"
        :title="t('workbench.workspaceFiles.chooseApplication')" aria-haspopup="menu" :aria-expanded="activeMenu === 'apps'"
        @click="show('apps')" @keydown.down.prevent="show('apps')" @keydown.up.prevent="show('apps', 'last')">
        <Icon name="chevronDown" :size="13" />
      </button>
    </div>
    <button ref="moreTrigger" type="button" class="workspace-resource-actions__trigger" data-testid="workspace-file-more"
      :disabled="!validTarget || busy" :aria-label="t('resourceActions.more')" :title="t('resourceActions.more')"
      aria-haspopup="menu" :aria-expanded="activeMenu === 'more'" @click="show('more')"
      @keydown.down.prevent="show('more')" @keydown.up.prevent="show('more', 'last')"><Icon name="moreHorizontal" :size="16" /></button>
    <span v-if="error && !open" class="workspace-resource-actions__error" role="alert" :title="error">{{ error }}</span>
    <Teleport to="body">
      <div v-if="open" class="workspace-resource-actions__backdrop" @pointerdown.self="close()">
        <div ref="menu" class="workspace-resource-actions__menu" role="menu" tabindex="-1" :aria-label="t(activeMenu === 'apps' ? 'workbench.workspaceFiles.chooseApplication' : 'resourceActions.more')" :style="position" @keydown="keydown">
          <p class="workspace-resource-actions__target" data-testid="workspace-action-target" :title="target?.path">{{ targetLabel }}</p>
          <template v-if="activeMenu === 'apps'">
            <p class="workspace-resource-actions__label">{{ t('workbench.workspaceFiles.openOnHost') }}</p>
            <p v-if="catalogState === 'loading'" role="status">{{ t('workbench.workspaceFiles.checkingApplications') }}</p>
            <button v-for="app in apps" :key="app.id" role="menuitemradio" :aria-checked="selectedApp?.id === app.id" type="button"
              :data-app-id="app.id" :disabled="busy || !connected" @click="selectApp(app.id)">
              <WorkspaceApplicationIcon :app-id="app.id" />
              <span class="workspace-resource-actions__item-label">{{ appLabel(app) }}</span>
              <span class="workspace-resource-actions__item-check" aria-hidden="true"><Icon v-if="selectedApp?.id === app.id" name="check" :size="14" /></span>
            </button>
          </template>
          <template v-else>
            <button role="menuitem" type="button" :disabled="busy" @click="perform('copy-path')"><Icon class="workspace-resource-actions__item-icon" name="copy" :size="15" /><span class="workspace-resource-actions__item-label">{{ t(isRoot ? 'workbench.workspaceFiles.copyDirectoryPath' : 'workspaceReference.copyPath') }}</span></button>
            <button v-if="file" role="menuitem" type="button" :disabled="busy || !connected" @click="perform('download')"><Icon class="workspace-resource-actions__item-icon" name="download" :size="15" /><span class="workspace-resource-actions__item-label">{{ t('chat.download') }}</span></button>
          </template>
          <template v-if="catalogState === 'unavailable' || catalogState === 'error'">
            <p>{{ unavailableHint }}</p>
            <p v-if="catalogState === 'error'" role="status">{{ t('workbench.workspaceFiles.applicationsFailed') }}</p>
            <button role="menuitem" type="button" :disabled="busy || !connected" @click="loadApps"><Icon class="workspace-resource-actions__item-icon" name="refresh" :size="15" /><span class="workspace-resource-actions__item-label">{{ t('workbench.workspaceFiles.retryApplications') }}</span></button>
          </template>
          <p v-if="error" role="alert">{{ error }}</p>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.workspace-resource-actions { display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0; min-width: 0; }
.workspace-resource-actions__split { display: inline-flex; align-items: stretch; min-width: 0; height: 29px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg); overflow: hidden; }
.workspace-resource-actions button { color: var(--text); font: inherit; font-size: 12px; cursor: pointer; }
.workspace-resource-actions__primary, .workspace-resource-actions__arrow, .workspace-resource-actions__trigger { display: inline-flex; align-items: center; justify-content: center; gap: 6px; border: 0; background: transparent; }
.workspace-resource-actions__primary { min-width: 0; padding: 0 9px; }
.workspace-resource-actions__primary-label { max-width: 132px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-resource-actions__arrow { width: 23px; border-left: 1px solid var(--border); color: var(--text-muted); }
.workspace-resource-actions__trigger { border-radius: var(--radius-sm); width: 28px; height: 28px; padding: 0; color: var(--text-muted); }
.workspace-resource-actions button:hover:not(:disabled), .workspace-resource-actions__menu button:hover:not(:disabled) { background: var(--bg-hover); }
.workspace-resource-actions button:focus-visible, .workspace-resource-actions__menu button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; background: var(--bg-hover); }
.workspace-resource-actions button:disabled, .workspace-resource-actions__menu button:disabled { opacity: .5; cursor: default; }
.workspace-resource-actions__error { max-width: 220px; font-size: 12px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workspace-resource-actions__backdrop { position: fixed; inset: 0; z-index: 10000; }
.workspace-resource-actions__menu { position: fixed; width: 264px; max-width: calc(100vw - 16px); max-height: calc(100vh - 16px); overflow: auto; padding: var(--sp-1); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--bg-surface); color: var(--text); box-shadow: var(--shadow-lg); }
.workspace-resource-actions__menu button { display: grid; grid-template-columns: 20px minmax(0, 1fr) 16px; align-items: center; column-gap: 9px; width: 100%; text-align: start; border: 0; border-radius: var(--radius-sm); padding: 7px 10px; font: inherit; font-size: 12px; color: inherit; background: transparent; cursor: pointer; }
.workspace-resource-actions__item-label { grid-column: 2; min-width: 0; text-align: start; overflow-wrap: anywhere; }
.workspace-resource-actions__item-icon { grid-column: 1; display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; }
.workspace-resource-actions__item-check { grid-column: 3; display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 20px; }
.workspace-resource-actions__menu p { font-size: 12px; line-height: 1.5; margin: 6px 10px; color: var(--text-muted); white-space: normal; }
.workspace-resource-actions__menu .workspace-resource-actions__target { color: var(--text); font-weight: 500; overflow-wrap: anywhere; }
</style>
