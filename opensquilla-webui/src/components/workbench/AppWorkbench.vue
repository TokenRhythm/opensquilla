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
    :before-close-item="beforeCloseItem"
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
          v-if="isActiveAnnotationToolbarItem(toolbarItem)"
          id="workbench-annotation-mode-status"
          class="app-workbench__annotation-mode-status"
          :aria-label="t('workbench.artifactAnnotation.selectElement')"
          :title="t('workbench.artifactAnnotation.selectElement')"
          aria-atomic="true"
          aria-live="polite"
          data-testid="workbench-annotation-mode-status"
          role="status"
        >
          <span class="app-workbench__annotation-mode-status-full" aria-hidden="true">
            {{ t('workbench.artifactAnnotation.selectElement') }}
          </span>
        </span>
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
        <select
          v-else-if="toolbarItem.kind === 'select'"
          class="app-workbench__switcher app-workbench__mode-select"
          :value="toolbarItem.value"
          :aria-label="toolbarItem.label"
          :title="toolbarItem.label"
          @change="performPanelSelection(item, toolbarItem, $event)"
        >
          <option
            v-for="option in toolbarItem.options"
            :key="option.value"
            :value="option.value"
            :disabled="option.disabled"
          >
            {{ option.label }}
          </option>
          <optgroup
            v-if="toolbarItem.actionOptions?.length"
            :label="toolbarItem.actionGroupLabel"
          >
            <option
              v-for="option in toolbarItem.actionOptions"
              :key="option.value"
              :value="option.value"
              :disabled="option.disabled"
            >
              {{ option.label }}
            </option>
          </optgroup>
        </select>
        <button
          v-else
          type="button"
          class="app-workbench__action"
          :class="{ 'is-active': toolbarItem.pressed }"
          :aria-label="toolbarItem.label"
          :aria-describedby="isActiveAnnotationToolbarItem(toolbarItem)
            ? 'workbench-annotation-mode-status'
            : undefined"
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
import { useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import { useArtifactPromptAnnotationsStore } from '@/stores/artifactPromptAnnotations'
import {
  flushActiveWorkbenchDocumentContext,
  useWorkbenchDocumentContextStore,
  type ActiveWorkbenchDocumentContext,
  type WorkbenchDocumentContextPrepareRequest,
  type WorkbenchDocumentPrepareRequest,
} from '@/stores/workbenchDocumentContext'
import { useWorkbenchResourcesStore } from '@/stores/workbenchResources'
import { useRpcStore } from '@/stores/rpc'
import type {
  ArtifactPayload,
  ArtifactStateEventPayload,
  ChatDocumentContext,
} from '@/types/rpc'
import type {
  WorkbenchPreviewDescriptor,
  WorkbenchResource,
} from '@/types/workbenchResources'
import { workbenchResourceRefId } from '@/types/workbenchResources'
import { workbenchPanelRegistry } from '@/workbench/registry'
import {
  artifactWorkbenchItemId,
  artifactFromWorkbenchItem,
  createArtifactPreviewWorkbenchItem,
  navigationArtifactsFromWorkbenchItem,
  previewableNavigationArtifactsFromWorkbenchItem,
  requestInitialSectionForWorkbenchItem,
  sessionKeyFromWorkbenchItem,
} from '@/workbench/artifactItems'
import {
  BROWSER_WORKBENCH_OPEN_EVENT,
  createBrowserWorkbenchItem,
  normalizeBrowserUrl,
  type BrowserWorkbenchOpenEventDetail,
} from '@/workbench/browserItems'
import {
  artifactCategory,
  artifactFileTitle,
} from '@/utils/chat/artifacts'
import { artifactProductClientError } from '@/utils/artifactProductErrors'
import {
  readPreviewPreferences,
  savePreviewPreferences,
} from '@/utils/workbench/previewPreferences'
import { artifactWorkbenchPreviewKind } from '@/utils/workbench/artifactPreview'
import {
  attachWorkbenchRuntime,
  WorkbenchRuntimeManager,
} from '@/workbench/runtime'
import { createRpcArtifactDocumentProvider } from '@/workbench/artifactDocumentProvider'
import { artifactPayloadFromRevision } from '@/workbench/artifactDocumentProvider'
import { createRpcWorkbenchResourceProvider } from '@/workbench/workbenchResourceProvider'
import {
  workbenchResourceActionReasonCode,
} from '@/workbench/resourceCapabilityPresentation'
import {
  artifactPayloadFromWorkbenchResource,
  createResourceCollectionWorkbenchItem,
  resourceFromPreparedPreview,
  resourceUsesNativeHtmlPreview,
  resourceCollectionWorkbenchItemId,
  workbenchResourceKey,
} from '@/workbench/workbenchResourceItems'
import {
  ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT,
  ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT,
  ARTIFACT_PROMPT_ANNOTATIONS_ACCEPTED_EVENT,
  type ArtifactPromptAnnotationFocusDetail,
  type ArtifactPromptAnnotationReuseDetail,
  type ArtifactPromptAnnotationsAcceptedDetail,
} from '@/workbench/promptAnnotations'
import { useWorkbenchStore } from '@/workbench/store'
import type {
  NativeSurfaceRect,
  WorkbenchBeforeCloseOptions,
  WorkbenchComponentEvent,
  WorkbenchItem,
  WorkbenchPanelHeader,
  WorkbenchPanelRenderState,
  WorkbenchToolbarSelectOption,
  WorkbenchToolbarItem,
} from '@/workbench/types'
import { createArtifactWorkbenchDefinitions } from './artifactWorkbenchProvider'
import { createBrowserWorkbenchDefinition } from './browserWorkbenchProvider'
import { createWorkbenchResourceCollectionDefinition } from './workbenchResourceCollectionProvider'
import WorkbenchHost from './WorkbenchHost.vue'
import { fetchArtifactBlob } from '@/utils/chat/artifactAccess'
import { downloadBlob } from '@/utils/browser'

const props = withDefaults(defineProps<{
  enabled?: boolean
  modalBlocked?: boolean
  workbenchResourcesEnabled?: boolean
  promptAnnotationsEnabled?: boolean
  routeActive?: boolean
  sessionId?: string
}>(), {
  enabled: true,
  modalBlocked: false,
  workbenchResourcesEnabled: false,
  promptAnnotationsEnabled: false,
  routeActive: false,
  sessionId: '',
})

const { t } = useI18n()
const { confirm } = useConfirm()
const { pushToast } = useToasts()
const platform = usePlatform()
const store = useWorkbenchStore()
const rpc = useRpcStore()
const artifactDocuments = useArtifactDocumentsStore()
const artifactPromptAnnotations = useArtifactPromptAnnotationsStore()
const workbenchDocumentContext = useWorkbenchDocumentContextStore()
const workbenchResources = useWorkbenchResourcesStore()
const artifactDocumentProvider = createRpcArtifactDocumentProvider(rpc)
artifactDocuments.setProvider(artifactDocumentProvider)
const workbenchResourceProvider = createRpcWorkbenchResourceProvider(rpc)
workbenchResources.setProvider(props.workbenchResourcesEnabled ? workbenchResourceProvider : null)
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
let stopArtifactEvents: (() => void) | null = null
let stopDocumentEvents: (() => void) | null = null
let detachRuntime: (() => Promise<void>) | null = null
const artifactEventSequences = new Map<string, number>()
let scopeChangeGeneration = 0
const documentContextController = workbenchDocumentContext.attachController(
  prepareActiveDocumentContext,
  prepareWorkbenchDocument,
)

function artifactPreviewItemForExplicitOpen(
  options: Parameters<typeof createArtifactPreviewWorkbenchItem>[0],
): WorkbenchItem {
  const item = createArtifactPreviewWorkbenchItem(options)
  return requestInitialSectionForWorkbenchItem(
    item,
    store.items.find(candidate => candidate.id === item.id) || null,
  )
}

function readAuthToken(): string {
  if (typeof sessionStorage === 'undefined') return ''
  try {
    return sessionStorage.getItem('opensquilla.wsToken') || ''
  } catch {
    return ''
  }
}

function confirmWorkbenchPermission(request: {
  permission: string
  requestingOrigin: string
}): Promise<boolean> {
  return confirm({
    title: t('workbench.artifactPreview.permissionTitle'),
    body: t('workbench.artifactPreview.permissionBody', {
      origin: request.requestingOrigin || t('workbench.artifactPreview.unknownOrigin'),
      permission: request.permission,
    }),
    primaryLabel: t('workbench.artifactPreview.permissionAllow'),
    primaryClass: 'btn--primary',
  })
}

function openExternalUrl(value: string) {
  const url = normalizeBrowserUrl(value)
  if (!url || typeof window === 'undefined') return
  const opened = window.open(url, '_blank', 'noopener,noreferrer')
  if (opened) opened.opener = null
}

function openBrowserUrl(value: string) {
  const sessionId = store.activeSessionId || props.sessionId
  if (!nativeApi || !sessionId) {
    openExternalUrl(value)
    return
  }
  const item = createBrowserWorkbenchItem({ scopeId: sessionId, url: value })
  if (!item) return
  if (!store.openItem(item)) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
  }
}

function onBrowserWorkbenchOpen(event: Event) {
  const detail = (event as CustomEvent<BrowserWorkbenchOpenEventDetail>).detail
  if (detail && typeof detail.url === 'string') openBrowserUrl(detail.url)
}

for (const definition of createArtifactWorkbenchDefinitions({
  artifactDocuments,
  promptAnnotations: props.promptAnnotationsEnabled ? {
    create: request => artifactPromptAnnotations.create(request),
    update: (annotationId, body) => artifactPromptAnnotations.update(annotationId, body),
    discard: annotationId => artifactPromptAnnotations.discard(annotationId),
    beginOverlayEdit: (annotationId, sessionKey) => {
      artifactPromptAnnotations.beginOverlayEdit(annotationId, sessionKey)
    },
    completeOverlayEdit: annotationId => {
      artifactPromptAnnotations.completeOverlayEdit(annotationId)
    },
    releaseOverlayEdit: annotationId => {
      artifactPromptAnnotations.releaseOverlayEdit(annotationId)
    },
    setActiveDocument: (sessionKey, documentId) => {
      artifactPromptAnnotations.setActiveDocument(sessionKey, documentId)
    },
  } : undefined,
  authToken: readAuthToken,
  baseOrigin,
  confirmPermission: confirmWorkbenchPermission,
  confirmRemoteResources: () => confirm({
    title: t('workbench.artifactPreview.remoteResourcesConfirmTitle'),
    body: t('workbench.artifactPreview.remoteResourcesConfirmBody'),
    primaryLabel: t('workbench.artifactPreview.remoteResourcesConfirmAction'),
    primaryClass: 'btn--primary',
  }),
  currentSessionId: () => store.activeSessionId || props.sessionId,
  getPreviewPreferences: () => readPreviewPreferences(platform),
  openArtifact: (artifact, sessionKey, navigationArtifacts) => {
    if (artifactCategory(artifact) === 'visual') {
      artifactImageLightbox.open({
        artifact,
        navigationArtifacts,
        sessionKey,
      })
      return
    }
    const opened = store.openItem(artifactPreviewItemForExplicitOpen({
      artifact,
      navigationArtifacts,
      nativeHtml: Boolean(
        platform.capabilities.hasNativeWorkbenchSurfaces
        && platform.workbench.native,
      ),
      sessionKey,
    }))
    if (!opened) {
      pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
    }
  },
  publishDocument: async request => {
    await workbenchResources.publishDocument(
      request.sessionKey,
      request.documentId,
      request.revisionId,
      request.name,
    )
    refreshResourceCollectionItem(request.sessionKey)
    pushToast(t('workbench.resources.published', { name: request.name }), {
      tone: 'ok',
    })
  },
  platform,
  previewLeasesEnabled: true,
  pushToast: (message, options) => pushToast(message, options),
  savePreviewPreferences: preferences => savePreviewPreferences(platform, preferences),
  showFullPreviewNotice: () => pushToast(
    t('workbench.artifactPreview.fullModeNotice'),
    { tone: 'info', duration: 9000 },
  ),
  t: (key, params) => String(t(key, params || {})),
})) {
  workbenchPanelRegistry.register(definition, { replace: true })
}
workbenchPanelRegistry.register(createWorkbenchResourceCollectionDefinition({
  download: downloadWorkbenchResource,
  open: openWorkbenchResource,
  publish: publishWorkbenchResource,
  pushError: message => pushToast(message, { tone: 'danger', duration: 9000 }),
  t: (key, params) => String(t(key, params || {})),
}), { replace: true })
workbenchPanelRegistry.register(createBrowserWorkbenchDefinition({
  confirmPermission: confirmWorkbenchPermission,
  openExternal: openExternalUrl,
  platform,
  t: (key, params) => String(t(key, params || {})),
}), { replace: true })
detachRuntime = attachWorkbenchRuntime(store, runtimeManager)

function resourceSessionKey(item: WorkbenchItem): string {
  return item.scope.type === 'session' ? item.scope.id : ''
}

function openResourceArtifact(
  resource: WorkbenchResource,
  artifact: ArtifactPayload,
  sessionKey: string,
  preparedPreview?: WorkbenchPreviewDescriptor,
  initialSection: 'preview' | 'source' = 'preview',
) {
  const nativeArtifact = resourceUsesNativeHtmlPreview(resource)
  const opened = store.openItem(artifactPreviewItemForExplicitOpen({
    artifact,
    initialSection,
    nativeHtml: Boolean(
      nativeArtifact
      && platform.capabilities.hasNativeWorkbenchSurfaces
      && platform.workbench.native,
    ),
    ...(preparedPreview ? { preparedPreview } : {}),
    previewLeaseEligible: nativeArtifact,
    resourceIdentity: workbenchResourceKey(resource.resource),
    sessionKey,
  }))
  if (!opened) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
  }
}

async function openWorkbenchResource(resource: WorkbenchResource, item: WorkbenchItem) {
  const sessionKey = resourceSessionKey(item)
  if (!sessionKey) return
  const current = await workbenchResources.openCurrent(sessionKey, resource)
  if (current?.disposition === 'document') {
    refreshResourceCollectionItem(sessionKey)
    const artifact = artifactPayloadFromRevision(current.revision)
    artifact.documentId = current.document.documentId
    artifact.revisionId = current.revision.revisionId
    openResourceArtifact(current.resource, artifact, sessionKey)
    return
  }
  if (!current && resource.resource.type === 'document') {
    openResourceArtifact(
      resource,
      artifactPayloadFromWorkbenchResource(resource),
      sessionKey,
    )
    return
  }
  if (
    !current
    && resource.resource.type !== 'document'
    && resource.capabilities.manualEdit
    && resource.sha256
  ) {
    await importWorkbenchResourceForSession(resource, sessionKey)
    return
  }
  const readonlyResource = current?.resource || resource
  refreshResourceCollectionItem(sessionKey)
  if (!readonlyResource.capabilities.preview) {
    const previewKind = artifactWorkbenchPreviewKind(
      artifactPayloadFromWorkbenchResource(readonlyResource),
    )
    if (previewKind !== 'html' && readonlyResource.capabilities.download) {
      await downloadWorkbenchResource(readonlyResource, item)
      return
    }
    throw artifactProductClientError('RESOURCE_UNSUPPORTED', {
      reasonCode: current?.reasonCode || workbenchResourceActionReasonCode(
        readonlyResource.capabilities,
        'preview',
      ) || undefined,
    })
  }
  if (
    resourceUsesNativeHtmlPreview(readonlyResource)
    && platform.capabilities.hasNativeWorkbenchSurfaces
    && platform.workbench.native
  ) {
    openResourceArtifact(
      readonlyResource,
      artifactPayloadFromWorkbenchResource(readonlyResource),
      sessionKey,
    )
    return
  }
  const preview = await workbenchResources.preview(sessionKey, readonlyResource.resource)
  const resolved = preview ? resourceFromPreparedPreview(preview) : readonlyResource
  openResourceArtifact(
    resolved,
    artifactPayloadFromWorkbenchResource(resolved),
    sessionKey,
    resolved.resource.type === 'document' ? undefined : preview?.preview,
  )
}

async function importWorkbenchResourceForSession(
  resource: WorkbenchResource,
  sessionKey: string,
) {
  const imported = await workbenchResources.importDocument(sessionKey, resource)
  refreshResourceCollectionItem(sessionKey)
  const artifact = artifactPayloadFromRevision(imported.revision)
  artifact.documentId = imported.document.documentId
  artifact.revisionId = imported.revision.revisionId
  openResourceArtifact({
    ...resource,
    resource: {
      type: 'document',
      documentId: imported.document.documentId,
      id: imported.document.documentId,
    },
    downloadUrl: imported.revision.downloadUrl || imported.document.latestDownloadUrl,
    capabilities: {
      preview: imported.document.capabilities.preview,
      download: imported.document.capabilities.download,
      selectionContext: imported.document.capabilities.selectionContext,
      manualEdit: imported.document.capabilities.manualEdit,
      agentEdit: imported.document.capabilities.agentEdit,
      edit: imported.document.capabilities.edit,
      publish: imported.document.capabilities.publish,
      reasonCode: imported.document.capabilities.reason,
    },
    relations: {
      documentId: imported.document.documentId,
      headRevisionId: imported.revision.revisionId,
      headArtifactId: imported.revision.artifactId,
      source: resource.resource,
    },
  }, artifact, sessionKey)
}

async function publishWorkbenchResource(
  resource: WorkbenchResource,
  item: WorkbenchItem,
) {
  const sessionKey = resourceSessionKey(item)
  const documentId = resource.relations.documentId || (
    resource.resource.type === 'document' ? workbenchResourceRefId(resource.resource) : ''
  )
  const revisionId = resource.relations.headRevisionId || ''
  if (!sessionKey || !documentId || !revisionId) {
    throw new Error(t('workbench.resources.publishUnavailable'))
  }
  await workbenchResources.publishDocument(
    sessionKey,
    documentId,
    revisionId,
    resource.name,
  )
  refreshResourceCollectionItem(sessionKey)
  pushToast(t('workbench.resources.published', { name: resource.name }), {
    tone: 'ok',
  })
}

async function downloadWorkbenchResource(
  resource: WorkbenchResource,
  item: WorkbenchItem,
) {
  const sessionKey = resourceSessionKey(item)
  const resolved = sessionKey
    ? await workbenchResources.resolve(sessionKey, resource.resource) || resource
    : resource
  if (
    resolved.resource.type === 'attachment'
    && typeof resolved.downloadUrl === 'string'
    && resolved.downloadUrl.startsWith('data:')
  ) {
    const response = await fetch(resolved.downloadUrl, { credentials: 'omit' })
    if (!response.ok) throw new Error(t('workbench.resources.actionFailed'))
    const blob = await response.blob()
    if (typeof resolved.size === 'number' && blob.size !== resolved.size) {
      throw new Error(t('workbench.resources.actionFailed'))
    }
    if (resolved.sha256) {
      const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
      const actual = [...new Uint8Array(digest)]
        .map(value => value.toString(16).padStart(2, '0'))
        .join('')
      if (actual !== resolved.sha256.toLowerCase()) {
        throw new Error(t('workbench.resources.actionFailed'))
      }
    }
    downloadBlob(blob, resolved.name)
    return
  }
  const artifact = artifactPayloadFromWorkbenchResource(resolved)
  const result = await fetchArtifactBlob(artifact, {
    authToken: readAuthToken(),
    baseOrigin,
    sessionKey,
  })
  if (!result.ok) throw new Error(result.message)
  downloadBlob(result.blob, resolved.name)
}

function refreshResourceCollectionItem(sessionKey: string) {
  const itemId = resourceCollectionWorkbenchItemId(sessionKey)
  if (!store.items.some(item => item.id === itemId)) return
  store.updateItem(createResourceCollectionWorkbenchItem({
    resources: workbenchResources.navigationResources(sessionKey),
    sessionKey,
    title: t('workbench.resources.title'),
  }))
}

async function refreshArtifactDocumentItem(
  item: WorkbenchItem,
) {
  const artifact = artifactFromWorkbenchItem(item)
  if (!artifact) return
  const sessionKey = sessionKeyFromWorkbenchItem(item)
  if (!sessionKey) return
  const previousRevisionId = artifactDocuments.snapshot(
    artifact,
    sessionKey,
  ).workspace?.document.headRevisionId
  const workspace = await artifactDocuments.refresh(artifact, sessionKey)
  const current = store.items.find(candidate => candidate.id === item.id)
  if (!current) return
  // The document snapshot lives in a separate Pinia store. Refresh the
  // descriptor identity as well so an already-mounted panel recomputes its
  // Versions/Changes props immediately, even when the state event arrived
  // during a WebSocket reconnect boundary.
  const updated = {
    ...current,
    payload: { ...current.payload },
  }
  store.updateItem(updated)
  if (
    workspace.source === 'document-api'
    && previousRevisionId
    && previousRevisionId !== workspace.document.headRevisionId
  ) {
    // Route the head change with the descriptor that updateItem just made
    // authoritative. RuntimeManager deliberately rejects the stale descriptor
    // captured before the asynchronous metadata refresh.
    runtimeManager.handleComponentEvent(updated, {
      type: 'artifact-head-changed',
      payload: { revisionId: workspace.document.headRevisionId },
    })
  }
}

function refreshOpenArtifactDocuments(sessionKey: string) {
  for (const item of store.items) {
    const artifact = artifactFromWorkbenchItem(item)
    if (!artifact || sessionKeyFromWorkbenchItem(item) !== sessionKey) continue
    const workspace = artifactDocuments.snapshot(artifact, sessionKey).workspace
    if (workspace?.source !== 'document-api') continue
    void refreshArtifactDocumentItem(item).catch(() => undefined)
  }
}

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
  const opened = store.openItem(artifactPreviewItemForExplicitOpen({
    artifact,
    navigationArtifacts,
    nativeHtml: Boolean(
      platform.capabilities.hasNativeWorkbenchSurfaces
      && platform.workbench.native,
    ),
    sessionKey,
  }))
  if (!opened) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
  }
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

function isActiveAnnotationToolbarItem(toolbarItem: WorkbenchToolbarItem): boolean {
  return toolbarItem.kind === 'action'
    && toolbarItem.id === 'toggle-annotation-mode'
    && toolbarItem.pressed === true
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

function performPanelSelection(
  item: WorkbenchItem | null,
  toolbarItem: Extract<WorkbenchToolbarItem, { kind: 'select' }>,
  event: Event,
) {
  const select = event.currentTarget as HTMLSelectElement
  const selectedValue = select.value
  const selected = [
    ...toolbarItem.options,
    ...(toolbarItem.actionOptions || []),
  ].find((option: WorkbenchToolbarSelectOption) => option.value === selectedValue)

  // Keep the control on the effective mode while the async lease replacement
  // runs. The render state selects the new value on success.
  select.value = toolbarItem.value
  if (!item || !selected || selectedValue === toolbarItem.value) return
  runtimeManager.performAction(item, selected.actionId)
}

function restoreWorkbenchFocus() {
  void nextTick(() => {
    const candidates = document.querySelectorAll<HTMLElement>([
      '[data-testid="chat-session-action-workbench"]',
      '[data-testid="chat-header-primary-action"][data-action="workbench"]',
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

function onArtifactState(event: ArtifactStateEventPayload) {
  const documentId = typeof event.documentId === 'string' ? event.documentId : ''
  const sequence = Number(event.artifactEventSeq)
  if (!documentId || !Number.isSafeInteger(sequence) || sequence < 1) return
  if ((artifactEventSequences.get(documentId) || 0) >= sequence) return
  artifactEventSequences.set(documentId, sequence)
  const activeSessionKey = store.activeSessionId || props.sessionId
  if (props.workbenchResourcesEnabled && activeSessionKey) {
    void workbenchResources.load(activeSessionKey, true).then(() => {
      refreshResourceCollectionItem(activeSessionKey)
    }).catch(() => undefined)
  }
  for (const item of store.items) {
    const artifact = artifactFromWorkbenchItem(item)
    if (!artifact) continue
    const itemSessionKey = sessionKeyFromWorkbenchItem(item)
    const snapshot = artifactDocuments.snapshot(artifact, itemSessionKey)
    if (snapshot.workspace?.document.documentId !== documentId) continue
    void refreshArtifactDocumentItem(item).catch(() => undefined)
  }
}

function promptAnnotationItem(
  detail: { documentId: string; sessionKey: string },
) {
  if (!detail.documentId || !detail.sessionKey) return null
  const item = store.items.find((candidate) => {
    const artifact = artifactFromWorkbenchItem(candidate)
    if (!artifact || sessionKeyFromWorkbenchItem(candidate) !== detail.sessionKey) return false
    return artifactDocuments.snapshot(artifact, detail.sessionKey)
      .workspace?.document.documentId === detail.documentId
  })
  return item || null
}

async function activatePromptAnnotationItem(
  detail: ArtifactPromptAnnotationFocusDetail | ArtifactPromptAnnotationReuseDetail,
) {
  detail.acknowledge?.()
  const item = promptAnnotationItem(detail)
  if (!item) return null
  artifactPromptAnnotations.setActiveDocument(detail.sessionKey, detail.documentId)
  store.activateItem(item.id)
  store.setExpanded(true)
  await nextTick()
  await runtimeManager.flush(item.id)
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const state = runtimeManager.getRenderState(item.id)
    if (
      state.annotationAvailable === true
      && state.nativeSurfaceState === 'ready'
    ) return item
    if (
      state.nativeSurfaceState === 'error'
      || state.nativeSurfaceState === 'crashed'
      || state.previewBlocked === true
    ) return null
    await new Promise(resolve => window.setTimeout(resolve, 50))
  }
  return null
}

async function onPromptAnnotationFocus(event: Event) {
  const detail = (event as CustomEvent<ArtifactPromptAnnotationFocusDetail>).detail
  if (!detail?.annotationId || !detail.documentId || !detail.sessionKey) {
    detail?.complete?.(false)
    return
  }
  const item = await activatePromptAnnotationItem(detail)
  if (!item) {
    detail.complete?.(false)
    return
  }
  detail.complete?.(true)
}

async function onPromptAnnotationReuse(event: Event) {
  const detail = (event as CustomEvent<ArtifactPromptAnnotationReuseDetail>).detail
  if (
    !detail?.documentId
    || !detail.sessionKey
    || !detail.body.trim()
    || detail.body.length > 16 * 1024
  ) {
    detail?.complete?.(false)
    return
  }
  const item = await activatePromptAnnotationItem(detail)
  if (!item) {
    detail.complete?.(false)
    return
  }
  runtimeManager.handleComponentEvent(item, {
    type: 'artifact-prompt-annotation-reuse',
    payload: { body: detail.body },
  })
  await runtimeManager.flush(item.id)
  detail.complete?.(true)
}

async function onPromptAnnotationsAccepted(event: Event) {
  const detail = (event as CustomEvent<ArtifactPromptAnnotationsAcceptedDetail>).detail
  if (!detail?.sessionKey || detail.acceptedIds.length === 0) return
  // Chat acceptance can update the resource descriptor in the same render
  // tick. Resolve the authoritative item after that update; RuntimeManager
  // intentionally drops events carrying a stale descriptor identity.
  await nextTick()
  const pendingFlushes: Promise<void>[] = []
  for (const item of store.items) {
    if (
      item.kind !== 'artifact-preview'
      || sessionKeyFromWorkbenchItem(item) !== detail.sessionKey
    ) continue
    runtimeManager.handleComponentEvent(item, {
      type: 'artifact-prompt-annotations-accepted',
      payload: { acceptedIds: [...detail.acceptedIds] },
    })
    pendingFlushes.push(runtimeManager.flush(item.id))
  }
  await Promise.all(pendingFlushes)
}

async function beforeCloseItem(
  item: WorkbenchItem,
  options?: WorkbenchBeforeCloseOptions,
): Promise<boolean> {
  const accepted = await runtimeManager.beforeClose(item, options)
  if (!accepted) {
    pushToast(t('workbench.artifactDocument.sourceUnavailable'), {
      tone: 'danger',
      duration: 9000,
    })
  }
  return accepted
}

function activeWorkbenchDocumentContext(): ActiveWorkbenchDocumentContext | null {
  if (!props.enabled || !props.routeActive || !store.expanded) return null
  const sessionKey = props.sessionId.trim()
  if (!sessionKey || store.activeSessionId !== sessionKey) return null
  const item = store.activeItem
  if (
    !item
    || item.kind !== 'artifact-preview'
    || item.scope.type !== 'session'
    || item.scope.id !== sessionKey
  ) return null
  const artifact = artifactFromWorkbenchItem(item)
  if (!artifact) return null
  const snapshot = artifactDocuments.snapshot(artifact, sessionKey)
  const workspace = snapshot.workspace
  if (
    snapshot.stale
    || workspace?.source !== 'document-api'
    || !workspace.document.documentId
    || !workspace.document.headRevisionId
  ) return null
  return {
    activeItemId: item.id,
    documentId: workspace.document.documentId,
    headRevisionId: workspace.document.headRevisionId,
    sessionKey,
  }
}

function activeDocumentRequestIsCurrent(
  request: WorkbenchDocumentContextPrepareRequest,
): boolean {
  if (request.isCurrent?.() === false) return false
  const current = activeWorkbenchDocumentContext()
  return current?.sessionKey === request.sessionKey
    && current.activeItemId === request.activeItemId
    && current.documentId === request.documentId
}

async function waitForLatestArtifactDocument(
  artifact: ArtifactPayload,
  sessionKey: string,
) {
  if (artifactDocuments.snapshot(artifact, sessionKey).loading) {
    await new Promise<void>((resolve) => {
      const stop = watch(
        () => artifactDocuments.snapshot(artifact, sessionKey).loading,
        loading => {
          if (loading) return
          stop()
          resolve()
        },
      )
    })
  }
  return artifactDocuments.snapshot(artifact, sessionKey)
}

async function prepareActiveDocumentContext(
  request: WorkbenchDocumentContextPrepareRequest,
) {
  const item = store.activeItem
  const artifact = item ? artifactFromWorkbenchItem(item) : null
  if (!item || !artifact || !activeDocumentRequestIsCurrent(request)) return false

  return flushActiveWorkbenchDocumentContext({
    beforeClose: () => beforeCloseItem(item, { preserveRuntime: true }),
    isCurrent: () => activeDocumentRequestIsCurrent(request),
    readLatest: async () => {
      const latest = await waitForLatestArtifactDocument(artifact, request.sessionKey)
      if (!activeDocumentRequestIsCurrent(request)) return null
      const workspace = latest.workspace
      if (
        latest.loading
        || latest.stale
        || workspace?.source !== 'document-api'
        || workspace.document.documentId !== request.documentId
        || !workspace.document.headRevisionId
      ) return null
      return {
        documentId: workspace.document.documentId,
        headRevisionId: workspace.document.headRevisionId,
      }
    },
  })
}

function workbenchDocumentItem(request: WorkbenchDocumentPrepareRequest): WorkbenchItem | null {
  if (
    request.isCurrent?.() === false
    || props.sessionId.trim() !== request.sessionKey
    || store.activeSessionId !== request.sessionKey
  ) return null
  const matches = store.items.filter((item) => {
    if (
      item.kind !== 'artifact-preview'
      || item.scope.type !== 'session'
      || item.scope.id !== request.sessionKey
    ) return false
    const artifact = artifactFromWorkbenchItem(item)
    if (!artifact) return false
    return artifactDocuments.snapshot(artifact, request.sessionKey)
      .workspace?.document.documentId === request.documentId
  })
  return matches.length === 1 ? matches[0]! : null
}

async function prepareWorkbenchDocument(
  request: WorkbenchDocumentPrepareRequest,
): Promise<ChatDocumentContext | null | false> {
  const item = workbenchDocumentItem(request)
  if (!item) return null
  const artifact = artifactFromWorkbenchItem(item)
  if (!artifact) return null
  const isCurrent = () => (
    request.isCurrent?.() !== false
    && workbenchDocumentItem(request)?.id === item.id
  )
  if (!isCurrent()) return false
  try {
    if (!await beforeCloseItem(item, { preserveRuntime: true }) || !isCurrent()) return false
    const latest = await waitForLatestArtifactDocument(artifact, request.sessionKey)
    const workspace = latest.workspace
    if (
      !isCurrent()
      || latest.loading
      || latest.stale
      || workspace?.source !== 'document-api'
      || workspace.document.documentId !== request.documentId
      || !workspace.document.headRevisionId
    ) return false
    return {
      documentId: workspace.document.documentId,
      headRevisionId: workspace.document.headRevisionId,
    }
  } catch {
    return false
  }
}

watch(
  activeWorkbenchDocumentContext,
  context => documentContextController.setActive(context),
  { flush: 'sync', immediate: true },
)

async function setSessionScopeSafely(sessionId: string | null) {
  const generation = ++scopeChangeGeneration
  const previousSessionId = store.activeSessionId
  if (previousSessionId === sessionId) return
  const staleItems = store.items.filter(item =>
    item.scope.type === 'session' && item.scope.id !== sessionId)
  for (const item of staleItems) {
    if (!await beforeCloseItem(item) || generation !== scopeChangeGeneration) return
  }
  if (generation !== scopeChangeGeneration) return
  store.setSessionScope(sessionId)
  if (previousSessionId) {
    artifactDocuments.clearSession(previousSessionId)
    workbenchResources.clearSession(previousSessionId)
  }
  if (props.workbenchResourcesEnabled && sessionId) {
    void workbenchResources.load(sessionId, true).then(() => {
      refreshResourceCollectionItem(sessionId)
    }).catch(() => undefined)
  }
}

watch(
  () => [props.routeActive, props.sessionId] as const,
  ([routeActive, sessionId]) => {
    if (!routeActive) return
    void setSessionScopeSafely(sessionId || null)
  },
  { immediate: true },
)

watch(
  () => props.enabled,
  enabled => {
    if (!enabled) store.reset()
  },
)

watch(
  () => props.workbenchResourcesEnabled,
  enabled => {
    workbenchResources.setProvider(enabled ? workbenchResourceProvider : null)
    const sessionKey = store.activeSessionId || props.sessionId
    if (!enabled) {
      workbenchResources.reset()
      return
    }
    if (rpc.state === 'connected' && props.routeActive && sessionKey) {
      void workbenchResources.load(sessionKey, true).then(() => {
        refreshResourceCollectionItem(sessionKey)
      }).catch(() => undefined)
    }
  },
)

watch(
  () => rpc.state,
  state => {
    const sessionKey = store.activeSessionId || props.sessionId
    if (
      state !== 'connected'
      || !props.routeActive
      || !sessionKey
    ) return
    if (props.workbenchResourcesEnabled) {
      void workbenchResources.load(sessionKey, true).then(() => {
        refreshResourceCollectionItem(sessionKey)
      }).catch(() => undefined)
    }
    refreshOpenArtifactDocuments(sessionKey)
  },
)

onMounted(() => {
  if (nativeApi) stopSurfaceEvents = nativeApi.onSurfaceEvent(onNativeSurfaceEvent)
  stopArtifactEvents = rpc.on('session.event.artifact_state', onArtifactState)
  stopDocumentEvents = rpc.on('document.state_changed', onArtifactState)
  window.addEventListener(BROWSER_WORKBENCH_OPEN_EVENT, onBrowserWorkbenchOpen)
  window.addEventListener(ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT, onPromptAnnotationFocus)
  window.addEventListener(ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT, onPromptAnnotationReuse)
  window.addEventListener(
    ARTIFACT_PROMPT_ANNOTATIONS_ACCEPTED_EVENT,
    onPromptAnnotationsAccepted,
  )
})

onBeforeUnmount(() => {
  documentContextController.detach()
  window.removeEventListener(BROWSER_WORKBENCH_OPEN_EVENT, onBrowserWorkbenchOpen)
  window.removeEventListener(ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT, onPromptAnnotationFocus)
  window.removeEventListener(ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT, onPromptAnnotationReuse)
  window.removeEventListener(
    ARTIFACT_PROMPT_ANNOTATIONS_ACCEPTED_EVENT,
    onPromptAnnotationsAccepted,
  )
  stopSurfaceEvents?.()
  stopSurfaceEvents = null
  stopArtifactEvents?.()
  stopArtifactEvents = null
  stopDocumentEvents?.()
  stopDocumentEvents = null
  artifactEventSequences.clear()
  if (detachRuntime) void detachRuntime()
  detachRuntime = null
  artifactDocuments.setProvider(null)
  workbenchResources.setProvider(null)
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
  flex: 0 0 30px;
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

.app-workbench__mode-select {
  width: min(174px, 30vw);
  border-color: var(--border);
  background-color: var(--bg-surface);
  color: var(--text);
  font-weight: 600;
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

.app-workbench__annotation-mode-status {
  display: inline-flex;
  min-width: 0;
  max-width: min(220px, 30vw);
  height: 28px;
  flex: 0 1 220px;
  align-items: center;
  padding: 0 var(--sp-2);
  border: 1px solid color-mix(in srgb, var(--accent) 42%, var(--border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
  color: var(--accent);
  font-size: var(--fs-xs);
  font-weight: 600;
}

.app-workbench__annotation-mode-status-full {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

@container (max-width: 520px) {
  .app-workbench__switcher {
    width: min(128px, 35cqw);
  }

  .app-workbench__warning span {
    display: none;
  }

  .app-workbench__annotation-mode-status {
    display: none;
  }
}
</style>
