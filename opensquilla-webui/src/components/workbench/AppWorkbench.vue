<template>
  <WorkbenchHost
    :enabled="enabled"
    :route-active="routeActive"
    :modal-blocked="surfaceBlocked"
    :aria-label="t('workbench.title')"
    :empty-label="t('workbench.empty')"
    :open-items-label="t('workbench.openItems')"
    :collapse-label="t('workbench.collapse')"
    :close-item-label="t('workbench.closeItem')"
    :resize-label="t('workbench.resize')"
    :pixels-label="t('workbench.pixels')"
    @collapsed="restoreWorkbenchFocus"
    @emptied="restoreWorkbenchFocus"
    @surface-rect="onSurfaceRect"
  >
    <template #title="{ item }">
      <span class="app-workbench__identity">
        <Icon
          v-if="panelHeader(item).icon"
          :name="panelHeader(item).icon!"
          :size="18"
          aria-hidden="true"
        />
        <span class="app-workbench__identity-copy">
          <strong>{{ panelHeader(item).title }}</strong>
          <small v-if="panelHeader(item).subtitle">
            {{ panelHeader(item).subtitle }}
          </small>
        </span>
      </span>
    </template>

    <template #actions="{ item }">
      <select
        v-if="artifactNavigationItems(item).length > 1"
        class="app-workbench__switcher"
        :value="item?.id"
        :aria-label="t('chat.sessionDeliverables')"
        :title="t('chat.sessionDeliverables')"
        data-testid="workbench-artifact-switcher"
        @change="selectNavigationArtifact(item, $event)"
      >
        <option
          v-for="artifact in artifactNavigationItems(item)"
          :key="artifactNavigationId(item, artifact)"
          :value="artifactNavigationId(item, artifact)"
        >
          {{ artifactFileTitle(artifact) }}
        </option>
      </select>
      <template
        v-for="toolbarItem in panelToolbarItems(item)"
        :key="toolbarItem.id"
      >
        <span
          v-if="toolbarItem.kind === 'status'"
          class="app-workbench__warning"
          :title="toolbarItem.label"
          role="status"
        >
          <Icon
            v-if="toolbarItem.icon"
            :name="toolbarItem.icon"
            :size="14"
            aria-hidden="true"
          />
          <span>{{ toolbarItem.text }}</span>
        </span>
        <button
          v-else
          type="button"
          class="app-workbench__action"
          :class="{ 'is-active': toolbarItem.pressed }"
          :aria-label="toolbarItem.label"
          :aria-pressed="toolbarItem.pressed"
          :disabled="toolbarItem.disabled"
          :title="toolbarItem.label"
          @click="performPanelAction(item, toolbarItem.id)"
        >
          <Icon :name="toolbarItem.icon" :size="15" aria-hidden="true" />
        </button>
      </template>
    </template>

    <template #panel="{ item, active }">
      <component
        :is="panelComponent(item)"
        v-if="panelComponent(item)"
        :ref="panelRefSetter(item)"
        v-bind="panelProps(item, active, false)"
        @workbench-event="handlePanelEvent(item, $event)"
      />
      <div v-else class="app-workbench__unsupported">
        {{ t('workbench.empty') }}
      </div>
    </template>

    <template #native-surface="{ item, active }">
      <component
        :is="panelComponent(item)"
        v-if="panelComponent(item)"
        :ref="panelRefSetter(item)"
        v-bind="panelProps(item, active, true)"
        @workbench-event="handlePanelEvent(item, $event)"
      />
    </template>
  </WorkbenchHost>
</template>

<script setup lang="ts">
import {
  computed,
  nextTick,
  onBeforeUnmount,
  onMounted,
  watch,
} from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useArtifactImageLightbox } from '@/composables/chat/useArtifactImageLightbox'
import { useConfirm } from '@/composables/useConfirm'
import { useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import { usePlatform } from '@/platform'
import type { NativeWorkbenchSurfaceEvent } from '@/platform/types'
import type { ArtifactPayload } from '@/types/rpc'
import { workbenchPanelRegistry } from '@/workbench/registry'
import {
  artifactWorkbenchItemId,
  createArtifactPreviewWorkbenchItem,
  navigationArtifactsFromWorkbenchItem,
  previewableNavigationArtifactsFromWorkbenchItem,
  sessionKeyFromWorkbenchItem,
} from '@/workbench/artifactItems'
import {
  artifactCategory,
  artifactFileTitle,
} from '@/utils/chat/artifacts'
import {
  attachWorkbenchRuntime,
  WorkbenchRuntimeManager,
} from '@/workbench/runtime'
import { useWorkbenchStore } from '@/workbench/store'
import type {
  NativeSurfaceRect,
  WorkbenchComponentEvent,
  WorkbenchItem,
  WorkbenchPanelHeader,
  WorkbenchPanelRenderState,
  WorkbenchToolbarItem,
} from '@/workbench/types'
import { createArtifactWorkbenchDefinitions } from './artifactWorkbenchProvider'
import WorkbenchHost from './WorkbenchHost.vue'

const props = withDefaults(defineProps<{
  enabled?: boolean
  modalBlocked?: boolean
  routeActive?: boolean
  sessionId?: string
}>(), {
  enabled: true,
  modalBlocked: false,
  routeActive: false,
  sessionId: '',
})

const { t } = useI18n()
const { confirm } = useConfirm()
const { pushToast } = useToasts()
const platform = usePlatform()
const store = useWorkbenchStore()
const artifactImageLightbox = useArtifactImageLightbox()
const nativeSurfaceOccluded = useNativeSurfaceOcclusionState()
const surfaceBlocked = computed(() => props.modalBlocked || nativeSurfaceOccluded.value)
const baseOrigin = typeof window === 'undefined' ? 'http://localhost' : window.location.origin
const nativeApi = platform.workbench.native
const runtimeManager = new WorkbenchRuntimeManager(workbenchPanelRegistry, {
  nativeWorkbenchApi: nativeApi,
  setExpanded: expanded => {
    store.setExpanded(expanded)
    if (!expanded) restoreWorkbenchFocus()
  },
})
let stopSurfaceEvents: (() => void) | null = null
let detachRuntime: (() => Promise<void>) | null = null

function readAuthToken(): string {
  if (typeof sessionStorage === 'undefined') return ''
  try {
    return sessionStorage.getItem('opensquilla.wsToken') || ''
  } catch {
    return ''
  }
}

for (const definition of createArtifactWorkbenchDefinitions({
  authToken: readAuthToken,
  baseOrigin,
  confirmRemoteResources: () => confirm({
    title: t('workbench.artifactPreview.remoteResourcesConfirmTitle'),
    body: t('workbench.artifactPreview.remoteResourcesConfirmBody'),
    primaryLabel: t('workbench.artifactPreview.remoteResourcesConfirmAction'),
    primaryClass: 'btn--primary',
  }),
  currentSessionId: () => store.activeSessionId || props.sessionId,
  openArtifact: (artifact, sessionKey, navigationArtifacts) => {
    if (artifactCategory(artifact) === 'visual') {
      artifactImageLightbox.open({
        artifact,
        navigationArtifacts,
        sessionKey,
      })
      return
    }
    store.openItem(createArtifactPreviewWorkbenchItem({
      artifact,
      navigationArtifacts,
      nativeHtml: Boolean(
        platform.capabilities.hasNativeWorkbenchSurfaces
        && platform.workbench.native,
      ),
      sessionKey,
    }))
  },
  platform,
  pushToast: (message, options) => pushToast(message, options),
  t: (key, params) => String(t(key, params || {})),
})) {
  workbenchPanelRegistry.register(definition, { replace: true })
}
detachRuntime = attachWorkbenchRuntime(store, runtimeManager)

function panelComponent(item: WorkbenchItem) {
  return workbenchPanelRegistry.resolve(item)?.component || null
}

function panelRenderState(
  item: WorkbenchItem,
  active: boolean,
  nativeSurface: boolean,
): WorkbenchPanelRenderState {
  return {
    active,
    hostAvailable: store.hostAvailable,
    nativeSurface,
    runtimeState: runtimeManager.getRenderState(item.id),
  }
}

function panelProps(
  item: WorkbenchItem,
  active: boolean,
  nativeSurface: boolean,
): Readonly<Record<string, unknown>> {
  return workbenchPanelRegistry.resolve(item)?.getProps?.(
    item,
    panelRenderState(item, active, nativeSurface),
  ) || {}
}

function artifactNavigationItems(
  item: WorkbenchItem | null,
): readonly ArtifactPayload[] {
  return previewableNavigationArtifactsFromWorkbenchItem(item)
}

function artifactNavigationId(
  item: WorkbenchItem | null,
  artifact: ArtifactPayload,
): string {
  return artifactWorkbenchItemId(sessionKeyFromWorkbenchItem(item), artifact)
}

function selectNavigationArtifact(
  item: WorkbenchItem | null,
  event: Event,
) {
  if (!item) return
  const select = event.currentTarget as HTMLSelectElement
  const artifact = artifactNavigationItems(item).find(
    candidate => artifactNavigationId(item, candidate) === select.value,
  )
  if (!artifact || select.value === item.id) return
  const navigationArtifacts = navigationArtifactsFromWorkbenchItem(item)
  const sessionKey = sessionKeyFromWorkbenchItem(item)
  store.openItem(createArtifactPreviewWorkbenchItem({
    artifact,
    navigationArtifacts,
    nativeHtml: Boolean(
      platform.capabilities.hasNativeWorkbenchSurfaces
      && platform.workbench.native,
    ),
    sessionKey,
  }))
}

function panelHeader(item: WorkbenchItem | null): WorkbenchPanelHeader {
  if (!item) return { title: '' }
  return workbenchPanelRegistry.resolve(item)?.getHeader?.(
    item,
    panelRenderState(
      item,
      item.id === store.activeItemId,
      item.hostKind === 'native-webcontents',
    ),
  ) || { title: item.title }
}

function panelToolbarItems(
  item: WorkbenchItem | null,
): readonly WorkbenchToolbarItem[] {
  if (!item) return []
  return workbenchPanelRegistry.resolve(item)?.getToolbarItems?.(
    item,
    panelRenderState(
      item,
      item.id === store.activeItemId,
      item.hostKind === 'native-webcontents',
    ),
  ) || []
}

function setPanelHandle(item: WorkbenchItem, value: unknown) {
  runtimeManager.setComponentHandle(item, value)
}

function panelRefSetter(item: WorkbenchItem): (value: unknown) => void {
  return value => setPanelHandle(item, value)
}

function handlePanelEvent(item: WorkbenchItem, event: WorkbenchComponentEvent) {
  if (event.type === 'request-collapse') {
    store.setExpanded(false)
    restoreWorkbenchFocus()
    return
  }
  runtimeManager.handleComponentEvent(item, event)
}

function performPanelAction(item: WorkbenchItem | null, actionId: string) {
  if (item) runtimeManager.performAction(item, actionId)
}

function restoreWorkbenchFocus() {
  void nextTick(() => {
    const candidates = document.querySelectorAll<HTMLElement>([
      '[data-testid="chat-session-action-deliverables"]',
      '[data-testid="chat-header-primary-action"][data-action="deliverables"]',
      '.chat-textarea',
    ].join(','))
    const target = [...candidates].find(element => element.getClientRects().length > 0)
    target?.focus({ preventScroll: true })
  })
}

function onSurfaceRect(rect: NativeSurfaceRect) {
  runtimeManager.handleSurfaceRect(rect)
}

function onNativeSurfaceEvent(event: NativeWorkbenchSurfaceEvent) {
  runtimeManager.handleNativeSurfaceEvent(event)
}

watch(
  () => [props.routeActive, props.sessionId] as const,
  ([routeActive, sessionId]) => {
    if (routeActive) store.setSessionScope(sessionId || null)
  },
  { immediate: true },
)

watch(
  () => props.enabled,
  enabled => {
    if (!enabled) store.reset()
  },
)

onMounted(() => {
  if (nativeApi) stopSurfaceEvents = nativeApi.onSurfaceEvent(onNativeSurfaceEvent)
})

onBeforeUnmount(() => {
  stopSurfaceEvents?.()
  stopSurfaceEvents = null
  if (detachRuntime) void detachRuntime()
  detachRuntime = null
})
</script>

<style scoped>
.app-workbench__identity {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: var(--sp-2);
  color: var(--text-dim);
}

.app-workbench__identity-copy {
  display: grid;
  min-width: 0;
  gap: 1px;
}

.app-workbench__identity-copy strong,
.app-workbench__identity-copy small,
.app-workbench__collection-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-workbench__identity-copy strong,
.app-workbench__collection-title {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
}

.app-workbench__identity-copy small {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 400;
}

.app-workbench__action {
  display: inline-flex;
  width: 30px;
  height: 30px;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}

.app-workbench__switcher {
  width: min(150px, 24vw);
  min-width: 84px;
  height: 30px;
  padding: 0 24px 0 var(--sp-2);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  background-color: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
}

.app-workbench__switcher:hover {
  border-color: var(--border);
  background-color: var(--bg-hover);
  color: var(--text);
}

.app-workbench__switcher:focus-visible {
  border-color: var(--accent);
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.app-workbench__action:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.app-workbench__action.is-active {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent);
}

.app-workbench__action:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.app-workbench__warning {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: var(--sp-1);
  color: var(--warn);
  font-size: var(--fs-xs);
}

.app-workbench__warning span {
  overflow: hidden;
  max-width: 150px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.app-workbench__unsupported {
  display: grid;
  min-height: 100%;
  place-items: center;
  padding: var(--sp-5);
  color: var(--text-dim);
  font-size: var(--fs-sm);
}

@media (max-width: 600px) {
  .app-workbench__switcher {
    width: min(128px, 35vw);
  }

  .app-workbench__warning span {
    display: none;
  }
}
</style>
