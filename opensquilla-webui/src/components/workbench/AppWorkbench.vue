<template>
  <WorkbenchHost
    ref="hostHandle"
    :tab-info="tabInfo"
    :show-toolbar="Boolean(store.activeItem && (showArtifactNavigation(store.activeItem) || visiblePanelToolbarItems(store.activeItem).length))"
    @tab-menu="openTabMenu"
    :enabled="enabled"
    :available-width="availableWidth"
    :layout-mode="layoutMode"
    :motion-enabled="motionEnabled"
    :animate-layout="animateLayout"
    :return-label="t('workbench.returnToChat')"
    :allow-empty="newPanelMenuVisible"
    :route-active="routeActive && store.activeSessionId === (sessionId || null)"
    :modal-blocked="modalBlocked"
    :native-blocked="surfaceBlocked"
    :native-snapshot-url="nativeSnapshotUrl"
    :aria-label="t('workbench.title')"
    :empty-label="t('workbench.empty')"
    :open-items-label="t('workbench.openItems')"
    :collapse-label="t('workbench.collapse')"
    :maximize-label="t('workbench.maximize')"
    :restore-label="t('workbench.restore')"
    :close-item-label="t('workbench.closeItem')"
    :historical-label="t('workbench.artifactDocument.historicalVersion')"
    :resize-label="t('workbench.resize')"
    :pixels-label="t('workbench.pixels')"
    :before-close-item="beforeCloseItem"
    @collapsed="restoreWorkbenchFocus"
    @return-chat="emit('return-chat')"
    @emptied="restoreWorkbenchFocus"
    @focus-return="restoreWorkbenchFocus"
    @surface-rect="onSurfaceRect"
  >
    <template v-if="$slots['collapse-control']" #collapse-control>
      <slot name="collapse-control" />
    </template>
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

    <template #tab-actions>
      <div
        v-if="newPanelMenuVisible"
        ref="newPanelMenuRoot"
        class="app-workbench__new-panel-menu"
      >
        <button
          ref="newPanelMenuTrigger"
          type="button"
          class="app-workbench__action"
          :aria-label="t('workbench.newPanel.title')"
          :title="t('workbench.newPanel.title')"
          aria-haspopup="menu"
          :aria-expanded="newPanelMenuOpen"
          aria-controls="workbench-new-panel-menu"
          data-testid="workbench-new-panel-trigger"
          @click.stop="toggleNewPanelMenu"
          @keydown.down.prevent="openNewPanelMenu('first')"
          @keydown.up.prevent="openNewPanelMenu('last')"
          @keydown.enter.prevent="toggleNewPanelMenu"
          @keydown.space.prevent="toggleNewPanelMenu"
        >
          <Icon name="plus" :size="16" aria-hidden="true" />
        </button>
        <Teleport to="body">
        <div
          v-if="newPanelMenuOpen"
          id="workbench-new-panel-menu"
          ref="newPanelMenu"
          class="app-workbench__new-panel-popover"
          role="menu"
          tabindex="-1"
          :style="newPanelMenuStyle"
          :aria-label="t('workbench.newPanel.title')"
          data-testid="workbench-new-panel-menu-list"
          @keydown="onNewPanelMenuKeydown"
        >
          <button
            v-for="option in newPanelOptions"
            :key="option.kind"
            type="button"
            role="menuitem"
            class="app-workbench__new-panel-option"
            :class="{ 'is-disabled': option.disabled }"
            :data-panel-kind="option.kind"
            :disabled="option.disabled"
            :title="option.disabledReason || option.label"
            @click="selectNewPanel(option)"
          >
            <Icon :name="option.icon" :size="16" aria-hidden="true" />
            <span class="app-workbench__new-panel-option-copy">
              <span>{{ option.label }}</span>
              <small v-if="option.disabledReason">{{ option.disabledReason }}</small>
            </span>
          </button>
          <button
            v-if="canReopenBrowser"
            type="button"
            role="menuitem"
            class="app-workbench__new-panel-option"
            data-testid="workbench-reopen-browser"
            @click="reopenBrowserFromMenu"
          >
            <Icon name="refresh" :size="16" aria-hidden="true" />
            <span class="app-workbench__new-panel-option-copy">
              <span>{{ t('workbench.browser.reopen') }}</span>
            </span>
          </button>
        </div>
        </Teleport>
      </div>
    </template>
    <template #actions="{ item }">
      <button v-if="item" type="button" class="app-workbench__action"
        :aria-label="t('resourceActions.more')" :title="t('resourceActions.more')" aria-haspopup="menu"
        data-testid="workbench-tab-actions" @click="openTabMenu(item, $event)"
        @keydown="onMenuTriggerKey(item, $event)">
        <Icon name="moreHorizontal" :size="16" aria-hidden="true" />
      </button>
    </template>

    <template #toolbar="{ item }">
      <select
        v-if="showArtifactNavigation(item)"
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
        v-for="toolbarItem in visiblePanelToolbarItems(item)"
        :key="toolbarItem.id"
      >
        <span
          v-if="isActiveAnnotationToolbarItem(toolbarItem)"
          id="workbench-annotation-mode-status"
          class="app-workbench__annotation-mode-status"
          :aria-label="t('workbench.annotationUnified.selectTarget')"
          :title="t('workbench.annotationUnified.selectTarget')"
          aria-atomic="true"
          aria-live="polite"
          data-testid="workbench-annotation-mode-status"
          role="status"
        >
          <span class="app-workbench__annotation-mode-status-full" aria-hidden="true">
            {{ t('workbench.annotationUnified.selectTarget') }}
          </span>
        </span>
        <span
          v-if="toolbarItem.kind === 'status'"
          class="app-workbench__warning"
          :class="{ 'app-workbench__warning--neutral': toolbarItem.tone === 'neutral' }"
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
          :class="{
            'is-active': toolbarItem.pressed,
            'app-workbench__action--annotation': ['toggle-annotation-mode', 'prepare-annotation', 'annotate'].includes(toolbarItem.id),
          }"
          :aria-label="toolbarItem.label"
          :aria-describedby="isActiveAnnotationToolbarItem(toolbarItem)
            ? 'workbench-annotation-mode-status'
            : undefined"
          :aria-pressed="toolbarItem.pressed"
          :disabled="toolbarItem.disabled"
          :title="toolbarItem.tooltip || toolbarItem.label"
          @click="performPanelAction(item, toolbarItem.id)"
        >
          <Icon :name="toolbarItem.icon" :size="15" aria-hidden="true" />
          <span
            v-if="['toggle-annotation-mode', 'prepare-annotation', 'annotate'].includes(toolbarItem.id)"
            class="app-workbench__action-label"
            aria-hidden="true"
          >
            {{ toolbarItem.label }}
          </span>
        </button>
      </template>
    </template>

    <template #empty>
      <section class="app-workbench__start" data-testid="workbench-start-panel" :aria-busy="panelContextLoading">
        <h2>{{ t('workbench.newPanel.title') }}</h2>
        <div class="app-workbench__start-options">
          <button
            v-for="option in newPanelOptions"
            :key="option.kind"
            type="button"
            class="app-workbench__new-panel-option app-workbench__start-option"
            :data-panel-kind="option.kind"
            :disabled="option.disabled"
            :title="option.disabledReason || option.label"
            @click="selectNewPanel(option)"
          >
            <Icon :name="option.icon" :size="20" aria-hidden="true" />
            <span class="app-workbench__new-panel-option-copy">
              <span>{{ option.label }}</span>
              <small v-if="option.disabledReason">{{ option.disabledReason }}</small>
            </span>
          </button>
        </div>
      </section>
    </template>

    <template #panel="{ item, active }">
      <component
        :is="panelComponent(item)"
        v-if="panelComponent(item)"
        :ref="panelRefSetter(item)"
        v-bind="panelProps(item, active, false)"
        v-on="{ workbenchEvent: panelEventHandler(item) }"
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
        v-on="{ workbenchEvent: panelEventHandler(item) }"
      />
    </template>
  </WorkbenchHost>
  <ResourceActionsMenu v-if="menuItem" ref="tabMenu" :artifact="resourceActionArtifact(menuItem)"
    :session-key="sessionKeyFromWorkbenchItem(menuItem)" :previewable="false" compact
    :download-action="menuDownloadAction"
    :extra-actions="tabMenuActions" @action="performTabMenuAction" />
</template>

<script setup lang="ts">
import {
  computed,
  inject,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  watch,
} from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useSideChatLauncher, type SideChatLaunchHandle, type SideChatLaunchRequest } from '@/modules/sideChatLauncher'
import type { IconName } from '@/utils/icons'
import { useConfirm } from '@/composables/useConfirm'
import { useDialogLayer, useNativeSurfaceOcclusionState } from '@/composables/useDialogA11y'
import { useToasts } from '@/composables/useToasts'
import { usePlatform } from '@/platform'
import type { NativeWorkbenchSurfaceEvent } from '@/platform/types'
import { artifactDocumentWorkspaceKey, useArtifactDocumentsStore } from '@/stores/artifactDocuments'
import ResourceActionsMenu from '@/components/ResourceActionsMenu.vue'
import { useArtifactPromptAnnotationsStore } from '@/stores/artifactPromptAnnotations'
import { useWorkbenchResourcesStore } from '@/stores/workbenchResources'
import type { ArtifactPayload } from '@/types/artifacts'
import {
  ARTIFACT_WORKBENCH_KEY,
  type ArtifactDocumentChange,
} from '@/modules/artifactWorkbench'
import type {
  WorkbenchPreviewDescriptor,
  WorkbenchResource,
} from '@/types/workbenchResources'
import { createWorkbenchResourceRef, workbenchResourceRefId } from '@/types/workbenchResources'
import { workbenchPanelRegistry } from '@/workbench/registry'
import {
  artifactWorkbenchItemId,
  artifactFromWorkbenchItem,
  artifactResourceRefFromWorkbenchItem,
  isFixedArtifactPreview,
  fileActionArtifactFromWorkbenchItem,
  createArtifactPreviewWorkbenchItem,
  navigationArtifactsFromWorkbenchItem,
  preparedPreviewFromWorkbenchItem,
  previewableNavigationArtifactsFromWorkbenchItem,
  requestInitialSectionForWorkbenchItem,
  sessionKeyFromWorkbenchItem,
} from '@/workbench/artifactItems'
import { workspaceFilePayload, workspaceFileReference, workspaceFileSessionKey } from '@/workbench/workspaceFileItems'
import { resolveWorkspaceDocumentFile } from '@/workbench/workspaceDocumentOpening'
import {
  BROWSER_WORKBENCH_OPEN_EVENT,
  createBrowserWorkbenchItem,
  normalizeBrowserUrl,
  type BrowserWorkbenchOpenEventDetail,
} from '@/workbench/browserItems'
import { artifactFileTitle } from '@/utils/chat/artifacts'
import { artifactProductClientError, classifyArtifactProductError } from '@/utils/artifactProductErrors'
import {
  readPreviewPreferences,
  savePreviewPreferences,
} from '@/utils/workbench/previewPreferences'
import { artifactWorkbenchPreviewKind, isOfficePreviewFormat } from '@/utils/workbench/artifactPreview'
import { previewPagePathFromUrl } from '@/utils/workbench/previewPagePath'
import {
  attachWorkbenchRuntime,
  WorkbenchRuntimeManager,
} from '@/workbench/runtime'
import { artifactPayloadFromRevision } from '@/workbench/artifactDocumentProvider'
import { prepareCurrentDocumentPreview, resolveCurrentDocumentResource } from '@/workbench/currentDocumentOpening'
import type { WorkspaceFile } from '@/modules/workspaceFiles'
import {
  workbenchResourceActionReasonCode,
} from '@/workbench/resourceCapabilityPresentation'
import {
  artifactPayloadFromWorkbenchResource,
  refreshResourceCollectionWorkbenchItem,
  resourceFromPreparedPreview,
  resourceUsesNativeHtmlPreview,
  resourceCollectionWorkbenchItemId,
  workbenchResourceKey,
} from '@/workbench/workbenchResourceItems'
import {
  ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT,
  ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT,
  PAGE_ANNOTATIONS_SENT_EVENT,
  type ArtifactPromptAnnotationFocusDetail,
  type ArtifactPromptAnnotationReuseDetail,
  type PageAnnotationsSentDetail,
} from '@/workbench/promptAnnotations'
import { PageAnnotationSendQueue } from '@/workbench/pageAnnotationSendQueue'
import { useWorkbenchStore } from '@/workbench/store'
import { createWorkspaceFilesWorkbenchItem } from '@/workbench/workspaceFileItems'
import { agentIdFromSessionKey } from '@/utils/chat/sessionKeys'
import { createTerminalWorkbenchItem } from '@/workbench/terminalItems'
import { createAssistantChatWorkbenchItem } from '@/workbench/assistantChatItems'
import { closeAssistantChatOwner } from '@/modules/assistantChat'
import { createClientRequestId } from '@/utils/chat/messageIdentity'
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
import { createArtifactWorkbenchDefinitions, type ArtifactWorkbenchProviderOptions } from './artifactWorkbenchProvider'
import { createBrowserWorkbenchDefinition } from './browserWorkbenchProvider'
import { requestNativeBrowserClose } from './browserCloseGuard'
import { createWorkbenchResourceCollectionDefinition } from './workbenchResourceCollectionProvider'
import { WORKSPACE_REFERENCES_KEY } from '@/modules/workspaceReferences'
import { WORKSPACE_FILES_KEY } from '@/modules/workspaceFiles'
import { TERMINAL_ACCESS_KEY } from '@/modules/terminal'
import { useGitAccess } from '@/modules/git'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'
import { SESSION_LIFECYCLE_KEY } from '@/modules/sessionLifecycle'
import { SESSION_READ_LIFECYCLE_FACTORY_KEY } from '@/modules/sessionReadLifecycle'
import { TURN_COMMANDS_KEY } from '@/modules/turnCommands'
import { CONVERSATION_EVENTS_KEY } from '@/modules/conversationEvents'
import { createWorkspaceFileDefinition } from './workspaceFileProvider'
import { createWorkspaceFilesWorkbenchDefinition } from './workspaceFilesWorkbenchProvider'
import { createTerminalWorkbenchDefinition } from './terminalWorkbenchProvider'
import { createGitChangesWorkbenchDefinition } from './gitChangesWorkbenchProvider'
import { createAssistantChatWorkbenchDefinition } from './assistantChatWorkbenchProvider'
import { createToolHistoryFileDefinition } from './toolHistoryFileProvider'
import WorkbenchHost from './WorkbenchHost.vue'
import type { WorkbenchLayoutMode } from '@/workbench/layout'
import { copyTextWithFallback, downloadBlob } from '@/utils/browser'
import { createTabThumbnailCache } from '@/workbench/tabThumbnailCache'
import { createNativeTabSnapshots } from './nativeTabSnapshots'

const emit = defineEmits<{ 'return-chat': [] }>()
const props = withDefaults(defineProps<{
  enabled?: boolean
  availableWidth?: number
  layoutMode?: WorkbenchLayoutMode
  motionEnabled?: boolean
  animateLayout?: boolean
  modalBlocked?: boolean
  workbenchResourcesEnabled?: boolean
  promptAnnotationsEnabled?: boolean
  officePromptAnnotationsEnabled?: boolean
  routeActive?: boolean
  sessionId?: string
  conversationStarted?: boolean
  sessionDurable?: boolean
  sessionLoading?: boolean
  sessionRefreshing?: boolean
  sessionContext?: string
  sessionPreparable?: boolean
  prepareSession?: (expectedSessionKey: string) => Promise<string | null>
  workspaceId?: string | null
  workspaceReady?: boolean
  workspaceResolved?: boolean
}>(), {
  enabled: true,
  modalBlocked: false,
  workbenchResourcesEnabled: false,
  promptAnnotationsEnabled: false,
  officePromptAnnotationsEnabled: false,
  routeActive: false,
  sessionId: '',
  conversationStarted: false,
  sessionDurable: false,
  sessionLoading: false,
  sessionRefreshing: false,
  sessionContext: '',
  sessionPreparable: false,
  workspaceId: null,
  workspaceReady: false,
  workspaceResolved: false,
})

const { t } = useI18n()
const { confirm } = useConfirm()
const { pushToast } = useToasts()
const platform = usePlatform()
const nativeApi = platform.workbench.native
const store = useWorkbenchStore()
// Browser producers share the same admission boundary even when they do not
// open the tab through this component.
const stopAdmissionNotifications = store.onAdmissionRejected(rejection => {
  pushToast(t('workbench.browser.capacityReached', { limit: rejection.limit }),
    { tone: 'warn', duration: 9000 })
})
const sideChatLauncher = useSideChatLauncher()
const sideChatHandles = new Map<string, SideChatLaunchHandle>()
const sideChatLaunchItems = new Map<string, { itemId: string; isCurrent(): boolean }>()
const releaseSideChatLauncher = sideChatLauncher?.register(launchSideChatCommand)
const workspaceReferences = inject(WORKSPACE_REFERENCES_KEY, null)
const workspaceFiles = inject(WORKSPACE_FILES_KEY, null)
const terminal = inject(TERMINAL_ACCESS_KEY, null)
// Git is a read-only owner published by main.ts; the module helper returns null
// outside the app shell (tests, isolated stories) instead of throwing.
const gitAccess = useGitAccess()
const referenceGateway = inject(GATEWAY_ACCESS_KEY, null)
const gateway = referenceGateway
const assistantSessionLifecycle = inject(SESSION_LIFECYCLE_KEY, null)
const assistantSessionReadFactory = inject(SESSION_READ_LIFECYCLE_FACTORY_KEY, null)
const assistantTurnCommands = inject(TURN_COMMANDS_KEY, null)
const assistantConversationEvents = inject(CONVERSATION_EVENTS_KEY, null)
const assistantPortsAvailable = Boolean(assistantSessionLifecycle && assistantSessionReadFactory
  && assistantTurnCommands && assistantConversationEvents)
type NewPanelKind = 'browser' | 'workspace-files' | 'terminal' | 'assistant-chat'
interface NewPanelOption {
  kind: NewPanelKind
  label: string
  icon: IconName
  disabled: boolean
  disabledReason?: string
}
const newPanelMenuOpen = ref(false)
const newPanelMenuTopmost = useDialogLayer(newPanelMenuOpen)
const newPanelMenuRoot = ref<HTMLElement | null>(null)
const newPanelMenu = ref<HTMLElement | null>(null)
const newPanelMenuTrigger = ref<HTMLButtonElement | null>(null)
const newPanelMenuStyle = ref({ left: '0px', top: '0px' })
const sessionReady = computed(() => Boolean(props.sessionId && store.activeSessionId === props.sessionId))
const preparingPanel = ref(false)
let panelOpeningGeneration = 0
let cancelPanelRefreshWait: (() => void) | null = null
const canPrepareSession = computed(() => sessionReady.value
  && (props.sessionDurable || props.sessionPreparable && Boolean(props.prepareSession)))
const gatewayAvailable = computed(() => Boolean(gateway?.availability === 'available'
  && gateway.isAvailable && gateway.connectionHealth === 'healthy' && !gateway.isResuming
  && (!gateway.connectionPhase || gateway.connectionPhase === 'healthy')))
const gatewayCanExecuteHost = computed(() => gateway?.canExecuteHost === true)
const canOpenBrowser = computed(() => Boolean(nativeApi && sessionReady.value))
function workspacePanelCapabilities(ready: boolean, resolved: boolean) {
  return {
    files: Boolean(workspaceFiles && props.workspaceId && ready
      && canPrepareSession.value && gatewayAvailable.value && gateway?.isLocalOwner),
    terminal: Boolean(terminal && sessionReady.value && resolved && (!props.workspaceId || ready)
      && gatewayAvailable.value && gatewayCanExecuteHost.value),
    assistant: Boolean(sessionReady.value && props.sessionDurable && props.conversationStarted && gatewayAvailable.value
      && gateway?.deliveryIdentity && assistantPortsAvailable && resolved
      && (!assistantSessionLifecycle?.supportsSidechat || assistantSessionLifecycle.supportsSidechat())
      && (!props.workspaceId || ready)),
  }
}
const currentPanelCapabilities = computed(() => workspacePanelCapabilities(props.workspaceReady, props.workspaceResolved))
const canOpenWorkspaceFiles = computed(() => !props.sessionRefreshing && currentPanelCapabilities.value.files)
const canOpenTerminal = computed(() => !props.sessionRefreshing && currentPanelCapabilities.value.terminal)
const canOpenAssistantChat = computed(() => !props.sessionRefreshing && currentPanelCapabilities.value.assistant)
const newPanelMenuVisible = computed(() => Boolean(nativeApi || workspaceFiles || terminal || referenceGateway))

const panelContextLoading = computed(() => !sessionReady.value || props.sessionLoading)
const panelContext = computed(() => JSON.stringify([
  props.sessionId, props.sessionContext, store.activeSessionId,
  gateway?.deliveryIdentity, gateway?.subscriptionEpoch,
]))
const settledWorkspacePresentation = ref<{
  context: string; workspaceId: string | null; ready: boolean; resolved: boolean
} | null>(null)
watch(() => [panelContext.value, props.workspaceId, props.workspaceReady, props.workspaceResolved,
  panelContextLoading.value, props.sessionRefreshing] as const, () => {
  if (panelContextLoading.value || props.sessionRefreshing) return
  settledWorkspacePresentation.value = {
    context: panelContext.value, workspaceId: props.workspaceId,
    ready: props.workspaceReady, resolved: props.workspaceResolved,
  }
}, { immediate: true })
const presentedWorkspace = computed(() => {
  const settled = settledWorkspacePresentation.value
  return props.sessionRefreshing && settled?.context === panelContext.value
    && settled.workspaceId === props.workspaceId
    ? settled : { ready: props.workspaceReady, resolved: props.workspaceResolved }
})
const canReopenBrowser = computed(() => canOpenBrowser.value && !panelContextLoading.value && !preparingPanel.value
  && store.closedBrowserItems.some(item => item.scope.type === 'session' && item.scope.id === props.sessionId))
// A pending session has unknown capabilities, not fewer capabilities. Preserve
// the last settled entry positions while resolving the next session; these are
// presentation keys only and never grant access to the previous session.
const settledPanelKinds = ref<NewPanelKind[] | null>(null)
const visiblePanelKinds = computed<NewPanelKind[]>(() => [
  'browser',
  ...(props.workspaceId ? ['workspace-files' as const] : []),
  'terminal',
  ...(props.sessionDurable && props.conversationStarted ? ['assistant-chat' as const] : []),
])
watch([panelContextLoading, visiblePanelKinds], ([loading, kinds]) => {
  if (!loading) settledPanelKinds.value = kinds
}, { immediate: true })

function sessionUnavailableReason(): string {
  return t('workbench.newPanel.requiresSession')
}

function gatewayUnavailableReason(): string {
  return t('workbench.newPanel.requiresConnection')
}

function buildPanelOptions(workspace: { ready: boolean; resolved: boolean }): NewPanelOption[] {
  const sessionReason = sessionReady.value ? undefined : sessionUnavailableReason()
  const capabilities = workspacePanelCapabilities(workspace.ready, workspace.resolved)
  const options: NewPanelOption[] = [
    {
      kind: 'browser', label: t('workbench.newPanel.browser'), icon: 'languages',
      disabled: !canOpenBrowser.value,
      disabledReason: canOpenBrowser.value ? undefined
        : sessionReason || t('workbench.newPanel.browserUnavailable'),
    },
    {
      kind: 'workspace-files', label: t('workbench.newPanel.workspaceFiles'), icon: 'folder',
      disabled: !capabilities.files,
      disabledReason: capabilities.files ? undefined : sessionReason || (!gatewayAvailable.value
        ? gatewayUnavailableReason() : t('workbench.newPanel.workspaceUnavailable')),
    },
    {
      kind: 'terminal', label: t('workbench.newPanel.terminal'), icon: 'terminal',
      disabled: !capabilities.terminal,
      disabledReason: capabilities.terminal ? undefined : sessionReason
        || (!gatewayAvailable.value || gateway?.hostExecuteUnavailableReason === 'disconnected'
          ? gatewayUnavailableReason() : gateway?.hostExecuteUnavailableReason === 'permission'
          ? t('workbench.newPanel.requiresHostExecute') : !workspace.resolved || props.workspaceId && !workspace.ready
            ? t('workbench.newPanel.workspaceNotReady') : t('workbench.newPanel.terminalUnavailable')),
    },
    {
      kind: 'assistant-chat', label: t('workbench.newPanel.assistantChat'), icon: 'chat',
      disabled: !capabilities.assistant,
      disabledReason: capabilities.assistant ? undefined : sessionReason
        || (!gatewayAvailable.value ? gatewayUnavailableReason()
          : !workspace.resolved || props.workspaceId && !workspace.ready
            ? t('workbench.newPanel.workspaceNotReady')
            : t('workbench.newPanel.assistantUnavailable')),
    },
  ]
  const kinds = panelContextLoading.value && settledPanelKinds.value
    ? settledPanelKinds.value : visiblePanelKinds.value
  return options.filter(option => kinds.includes(option.kind))
    .map(option => preparingPanel.value || panelContextLoading.value ? { ...option, disabled: true,
      disabledReason: t('workbench.newPanel.preparing') } : option)
}
const newPanelOptions = computed(() => buildPanelOptions(presentedWorkspace.value))
const actionablePanelOptions = computed(() => buildPanelOptions({
  ready: props.workspaceReady, resolved: props.workspaceResolved,
}))
watch(() => [props.sessionId, props.sessionContext, props.workspaceId, props.routeActive, props.enabled, props.modalBlocked, store.expanded,
  gateway?.subscriptionEpoch, gateway?.deliveryIdentity, gatewayAvailable.value], () => {
  panelOpeningGeneration += 1
  cancelPanelRefreshWait?.()
  preparingPanel.value = false
}, { flush: 'sync' })
watch(() => [referenceGateway?.subscriptionEpoch, referenceGateway?.availability,
  referenceGateway?.isLocalOwner, referenceGateway?.deliveryIdentity], (next, previous) => {
  // A filesystem snapshot is valid only for the connection that authorized it.
  for (const item of [...store.items]) {
    if (item.kind === 'file' || item.kind === 'workspace-files' || item.kind === 'terminal'
      || item.kind === 'tool-file-history'
      || item.kind === 'assistant-chat' && previous && next[3] !== previous[3]) store.closeItem(item.id, 'scope-changed')
  }
})
// Durability gates creation, not the lifetime of an existing side chat.
// ChatView changes its draft flag before publishing the next session key, and
// the route-header bridge also defaults to false while an owner is loading.
// Scope switching hides retained side chats; only explicit close retires them.
const resolvedWorkspaceBindings = new Map<string, string | null>()
watch(() => [props.sessionId, props.workspaceId, props.workspaceResolved] as const,
  ([sessionId, workspaceId, resolved]) => {
    if (!sessionId || !resolved) return
    const binding = workspaceId || null
    const hadBinding = resolvedWorkspaceBindings.has(sessionId)
    const previousBinding = resolvedWorkspaceBindings.get(sessionId)
    resolvedWorkspaceBindings.set(sessionId, binding)
    if (!hadBinding || previousBinding === binding) return
    // Only a resolved rebinding of this owner invalidates its tools. Visiting
    // another session (including its unresolved loading phase) is not rebinding.
    for (const item of [...store.items]) {
      if (item.scope.type === 'session' && item.scope.id === sessionId
        && ['workspace-files', 'terminal'].includes(item.kind)) {
        store.closeItem(item.id, 'scope-changed')
      }
    }
  }, { immediate: true })

const injectedArtifactWorkbench = inject(ARTIFACT_WORKBENCH_KEY)
if (!injectedArtifactWorkbench) throw new Error('ArtifactWorkbench was not provided')
const artifactWorkbench = injectedArtifactWorkbench
const artifactDocuments = useArtifactDocumentsStore()
const artifactPromptAnnotations = useArtifactPromptAnnotationsStore()
const workbenchResources = useWorkbenchResourcesStore()
// Metadata is reconstructible; open document owners and displayed inventories
// are not cache entries. Keep their heads while hidden edits/actions are alive.
const metadataOwners = new Map<string, () => void>()
watch(() => [store.items, store.activeSessionId] as const, () => {
  const wanted = new Set<string>()
  for (const item of store.items) {
    const artifact = artifactFromWorkbenchItem(item)
    if (!artifact || isFixedArtifactPreview(item)) continue
    const sessionKey = sessionKeyFromWorkbenchItem(item)
    const key = `document:${artifactDocumentWorkspaceKey(artifact, sessionKey)}`
    wanted.add(key)
    if (!metadataOwners.has(key)) metadataOwners.set(key, artifactDocuments.retain(artifact, sessionKey))
  }
  const sessionKey = store.activeSessionId || ''
  if (sessionKey) {
    const key = `inventory:${sessionKey}`
    wanted.add(key)
    if (!metadataOwners.has(key)) metadataOwners.set(key, workbenchResources.retain(sessionKey))
  }
  for (const [key, release] of metadataOwners) {
    if (wanted.has(key)) continue
    metadataOwners.delete(key)
    release()
  }
}, { immediate: true, flush: 'sync' })
onBeforeUnmount(() => { for (const release of metadataOwners.values()) release(); metadataOwners.clear() })
const artifactDocumentProvider = artifactWorkbench.documents
artifactDocuments.setProvider(artifactDocumentProvider)
const workbenchResourceProvider = artifactWorkbench.resources
workbenchResources.setProvider(props.workbenchResourcesEnabled ? workbenchResourceProvider : null)
const nativeSurfaceOccluded = useNativeSurfaceOcclusionState()
const surfaceBlocked = computed(() => props.modalBlocked || nativeSurfaceOccluded.value)
const baseOrigin = (() => {
  if (typeof window === 'undefined') return 'http://localhost'
  if (
    window.location.protocol === 'opensquilla-app:'
    && window.location.hostname === 'desktop'
  ) return 'opensquilla-app://desktop'
  return window.location.origin
})()
const runtimeManager = new WorkbenchRuntimeManager(workbenchPanelRegistry, {
  nativeWorkbenchApi: nativeApi,
  setExpanded: expanded => {
    store.setExpanded(expanded)
    if (!expanded) restoreWorkbenchFocus()
  },
})
const hostHandle = ref<{ closeItem(id: string): Promise<void>; closeOtherItems(id: string): Promise<void>; hideHover(): void } | null>(null)
const tabMenu = ref<{ show(event: MouseEvent | KeyboardEvent, artifact?: ArtifactPayload): Promise<void>; cancel(): void } | null>(null)
const menuItemId = ref<string | null>(null)
const menuItem = computed(() => store.visibleItems.find(item => item.id === menuItemId.value) ?? null)
const menuDownloadAction = computed(() => panelToolbarItems(menuItem.value)
  .some(action => action.kind === 'action' && action.id === 'download') ? 'panel:download' : undefined)
const thumbnailCache = createTabThumbnailCache()
const thumbnailVersion = ref(0)
const nativeArtifactSnapshots = createNativeTabSnapshots(nativeApi, () => { thumbnailVersion.value++ })
const nativeSnapshotUrl = computed(() => {
  void thumbnailVersion.value
  const item = store.activeItem
  if (!item) return undefined
  const state = runtimeManager.getRenderState(item.id)
  if (state.errorMessage || ['error', 'crashed', 'offline', 'unsupported'].includes(String(state.nativeSurfaceState))) return undefined
  if (typeof state.snapshotUrl === 'string') return state.snapshotUrl
  return nativeArtifactSnapshots.snapshot(item.id, thumbnailFingerprint(item)) || tabInfo(item).thumbnailUrl
})
function thumbnailFingerprint(item: WorkbenchItem) {
  const artifact = artifactFromWorkbenchItem(item)
  // Mutable documents render the store's head without replacing their tab item.
  // Prepared attachment/deliverable previews remain pinned to their own bytes.
  const workspace = artifact && !isFixedArtifactPreview(item)
    ? artifactDocuments.snapshot(artifact, sessionKeyFromWorkbenchItem(item)).workspace
    : null
  const head = workspace?.source === 'document-api' ? workspace.headArtifact : artifact
  return [head?.revisionId, head?.sha256, head?.download_url, head?.revision_download_url, artifact?.previewPagePath].join(':')
}
function tabInfo(item: WorkbenchItem) {
  void thumbnailVersion.value
  const state = runtimeManager.getRenderState(item.id)
  return {
    subtitle: panelHeader(item).subtitle,
    loading: item.kind === 'browser' && state.loading === true,
    thumbnailUrl: typeof state.thumbnailUrl === 'string' ? state.thumbnailUrl
      : nativeArtifactSnapshots.get(item.id, thumbnailFingerprint(item))
        || thumbnailCache.get(item.id, thumbnailFingerprint(item)),
  }
}
const tabMenuActions = computed(() => {
  const item = menuItem.value
  if (!item) return []
  const actions: { id: string; label: string; disabled?: boolean; separator?: boolean }[] = []
  if (item.kind === 'browser') {
    const state = runtimeManager.getRenderState(item.id)
    const loading = state.loading === true
    const url = normalizeBrowserUrl(String(state.currentUrl || item.payload.initialUrl || ''))
    actions.push(
      { id: loading ? 'stop' : 'reload', label: t(loading ? 'workbench.browser.stop' : 'workbench.refresh') },
      { id: 'copy-link', label: t('workbench.tabs.copyLink'), disabled: !url },
      { id: 'open-external', label: t('workbench.openExternal'), disabled: !url || url === 'about:blank' },
    )
  } else {
    const refresh = panelToolbarItems(item).find(action => action.kind === 'action' && action.id === 'refresh')
    if (refresh?.kind === 'action') actions.push({ id: 'panel:refresh', label: refresh.label, disabled: refresh.disabled })
    const external = panelToolbarItems(item).find(action => action.kind === 'action' && action.id === 'open-external')
    if (external?.kind === 'action') actions.push({ id: 'panel:open-external', label: external.label, disabled: external.disabled })
    if (nativeApi) actions.push({ id: 'open-web', label: t('workbench.tabs.openWeb') })
  }
  actions.push({ id: 'close', label: t('workbench.closeItem'), separator: Boolean(actions.length || resourceActionArtifact(item)) })
  if (store.visibleItems.length > 1) actions.push({ id: 'close-others', label: t('workbench.tabs.closeOthers') })
  return actions
})
async function openTabMenu(item: WorkbenchItem, event: MouseEvent | KeyboardEvent) {
  event.preventDefault()
  event.stopPropagation()
  hostHandle.value?.hideHover()
  menuItemId.value = item.id
  await nextTick()
  if (menuItem.value?.id === item.id) await tabMenu.value?.show(event, resourceActionArtifact(item))
}
function onMenuTriggerKey(item: WorkbenchItem, event: KeyboardEvent) {
  if (event.key === 'ContextMenu' || event.shiftKey && event.key === 'F10') void openTabMenu(item, event)
}
async function performTabMenuAction(action: string) {
  const item = menuItem.value
  if (!item) return
  if (action === 'close') { await hostHandle.value?.closeItem(item.id); return }
  if (action === 'close-others') { await hostHandle.value?.closeOtherItems(item.id); return }
  if (action === 'open-web') { openBrowserStart(); return }
  if (['panel:open-external', 'panel:download', 'panel:refresh'].includes(action)) {
    runtimeManager.performAction(item, action.slice('panel:'.length)); return
  }
  if (item.kind !== 'browser') return
  if (action === 'copy-link') {
    const state = runtimeManager.getRenderState(item.id)
    const url = normalizeBrowserUrl(String(state.currentUrl || item.payload.initialUrl || ''))
    if (!url) return
    try { await copyTextWithFallback(url) }
    catch { pushToast(t('resourceActions.failed'), { tone: 'danger' }) }
    return
  }
  runtimeManager.performAction(item, action)
}
watch(() => store.items.map(item => item.id), ids => {
  thumbnailCache.prune(new Set(ids))
  nativeArtifactSnapshots.prune(new Set(ids))
  thumbnailVersion.value++
})
watch(() => store.activeSessionId, () => {
  tabMenu.value?.cancel()
  menuItemId.value = null
  closeNewPanelMenu()
})
watch(() => [props.enabled, props.routeActive, props.modalBlocked, store.expanded], () => {
  if (!props.enabled || !props.routeActive || props.modalBlocked || !store.expanded) closeNewPanelMenu()
})
let stopSurfaceEvents: (() => void) | null = null
let stopArtifactEvents: (() => void) | null = null
let detachRuntime: (() => Promise<void>) | null = null
let scopeChangeGeneration = 0
let artifactNavigationGeneration = 0
watch(() => store.activeItemId, () => { artifactNavigationGeneration += 1 }, { flush: 'sync' })
function artifactPreviewItemForExplicitOpen(
  options: Parameters<typeof createArtifactPreviewWorkbenchItem>[0],
): WorkbenchItem {
  const item = createArtifactPreviewWorkbenchItem(options)
  return requestInitialSectionForWorkbenchItem(
    item,
    store.items.find(candidate => candidate.id === item.id) || null,
  )
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
  if (!url || url === 'about:blank' || typeof window === 'undefined') return
  const opened = window.open(url, '_blank', 'noopener,noreferrer')
  if (opened) opened.opener = null
}

function newPanelMenuButtons(): HTMLButtonElement[] {
  return [...(newPanelMenu.value?.querySelectorAll<HTMLButtonElement>(
    'button:not(:disabled)',
  ) || [])]
}

function focusNewPanelOption(position: 'first' | 'last' = 'first') {
  const buttons = newPanelMenuButtons()
  const target = buttons[position === 'last' ? buttons.length - 1 : 0] || newPanelMenu.value
  target?.focus({ preventScroll: true })
}

function positionNewPanelMenu() {
  const anchor = newPanelMenuTrigger.value?.getBoundingClientRect()
  const menu = newPanelMenu.value
  if (!anchor || !menu) return
  const width = menu.getBoundingClientRect().width || 280
  const height = menu.getBoundingClientRect().height
  newPanelMenuStyle.value = {
    left: `${Math.max(8, Math.min(anchor.right - width, window.innerWidth - width - 8))}px`,
    top: `${Math.max(8, Math.min(anchor.bottom + 6, window.innerHeight - height - 8))}px`,
  }
}

function openNewPanelMenu(position: 'first' | 'last' = 'first') {
  if (!props.enabled || !props.routeActive || props.modalBlocked) return
  tabMenu.value?.cancel()
  hostHandle.value?.hideHover()
  newPanelMenuOpen.value = true
  void nextTick(() => {
    if (!newPanelMenuOpen.value || !newPanelMenuTopmost.value) return
    positionNewPanelMenu()
    focusNewPanelOption(position)
  })
}

function closeNewPanelMenu(restoreFocus = false) {
  if (!newPanelMenuOpen.value) return
  newPanelMenuOpen.value = false
  if (restoreFocus) void nextTick(() => newPanelMenuTrigger.value?.focus())
}

function toggleNewPanelMenu() {
  if (newPanelMenuOpen.value) closeNewPanelMenu()
  else openNewPanelMenu()
}

function onNewPanelMenuKeydown(event: KeyboardEvent) {
  if (!newPanelMenuTopmost.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closeNewPanelMenu(true)
    return
  }
  if (event.key === 'Tab') {
    event.preventDefault()
    closeNewPanelMenu(true)
    return
  }
  const buttons = newPanelMenuButtons()
  if (!buttons.length || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length
  buttons[next]?.focus()
}

function onNewPanelOutside(event: PointerEvent) {
  if (!newPanelMenuOpen.value) return
  const target = event.target
  if (target instanceof Node && (newPanelMenuRoot.value?.contains(target)
    || newPanelMenu.value?.contains(target))) return
  closeNewPanelMenu()
}

function onNewPanelViewportChange(event: Event) {
  if (!newPanelMenuOpen.value) return
  const anchor = newPanelMenuTrigger.value
  if (!anchor?.isConnected) {
    closeNewPanelMenu()
    return
  }
  // Capture also sees transcript/editor/tab-list scrolling. Those independent
  // surfaces do not move this menu's anchor. Even the pane's opening animation
  // can scroll an ancestor after the + click; keep the menu and track its anchor.
  if (event.type === 'scroll' && event.target instanceof Node && !event.target.contains(anchor)) return
  const rect = anchor.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) {
    closeNewPanelMenu()
    return
  }
  positionNewPanelMenu()
}

async function selectNewPanel(option: NewPanelOption) {
  if (!props.routeActive || !props.enabled || props.modalBlocked || !store.expanded) return
  if (option.disabled || newPanelOptions.value.find(current => current.kind === option.kind)?.disabled !== false) return
  closeNewPanelMenu()
  if (props.sessionRefreshing && option.kind !== 'browser') {
    // Retained labels are presentation only. An explicit click can wait for the
    // current read, but may never create a panel from the old workspace binding.
    const generation = ++panelOpeningGeneration
    preparingPanel.value = true
    const refreshed = await new Promise<boolean>(resolve => {
      const stop = watch(() => props.sessionRefreshing, refreshing => {
        if (!refreshing) finish(true)
      }, { flush: 'post' })
      const cancel = () => finish(false)
      function finish(value: boolean) {
        stop()
        if (cancelPanelRefreshWait === cancel) cancelPanelRefreshWait = null
        resolve(value)
      }
      cancelPanelRefreshWait = cancel
    })
    if (generation !== panelOpeningGeneration) return
    preparingPanel.value = false
    if (!refreshed) return
    const current = actionablePanelOptions.value.find(candidate => candidate.kind === option.kind)
    if (current) await selectNewPanel(current)
    return
  }
  if (actionablePanelOptions.value.find(current => current.kind === option.kind)?.disabled !== false) return
  if (option.kind === 'browser') { openBrowserStart(); return }
  // A PTY or an empty side chat is not a durable main-chat session.
  if (option.kind === 'terminal') { openTerminal(); return }
  if (option.kind === 'assistant-chat') { openAssistantChat(); return }
  const sourceSession = props.sessionId
  const sourceWorkspace = props.workspaceId
  const sourceEpoch = gateway?.subscriptionEpoch
  const sourceIdentity = gateway?.deliveryIdentity
  const generation = ++panelOpeningGeneration
  const isCurrent = () => generation === panelOpeningGeneration && props.routeActive && props.enabled && !props.modalBlocked
    && props.sessionId === sourceSession && props.workspaceId === sourceWorkspace
    && gateway?.subscriptionEpoch === sourceEpoch && gateway?.deliveryIdentity === sourceIdentity
    && gatewayAvailable.value && store.expanded
  preparingPanel.value = true
  try {
    const key = props.sessionDurable ? sourceSession : await props.prepareSession?.(sourceSession)
    await nextTick()
    if (!key || key !== sourceSession || !isCurrent() || !props.sessionDurable) return
    closeNewPanelMenu()
    if (option.kind === 'workspace-files') openWorkspaceFiles()
  } catch (error) {
    if (isCurrent()) pushToast(error instanceof Error ? error.message : t('workbench.newPanel.prepareFailed'),
      { tone: 'danger', duration: 6000 })
  } finally {
    if (generation === panelOpeningGeneration) preparingPanel.value = false
  }
}

function openBrowserUrl(value: string, newTab = false) {
  const sessionId = props.sessionId
  if (!sessionId || !nativeApi) {
    openExternalUrl(value)
    return
  }
  let item = createBrowserWorkbenchItem({
    scopeId: sessionId,
    url: value,
    instanceId: createClientRequestId(),
  })
  if (!item) return
  if (item.payload.initialUrl === 'about:blank') item.title = t('workbench.browser.newTab')
  const source = store.findMostRecentItem(candidate => candidate.kind === 'browser'
    && candidate.scope.type === 'session' && candidate.scope.id === sessionId
    && typeof candidate.payload.targetRef === 'string')
  if (source) item = { ...item, payload: { ...item.payload, contextTargetRef: source.payload.targetRef } }
  const retained = store.findMostRecentItem(candidate =>
    candidate.kind === 'browser'
    && candidate.scope.type === 'session'
    && candidate.scope.id === sessionId
    && normalizeBrowserUrl(String(candidate.payload.initialUrl || '')) === item.payload.initialUrl,
  )
  if (retained && !newTab) {
    store.activateItem(retained.id)
    store.setExpanded(true)
    store.requestReveal()
    return
  }
  if (!store.openItem(item)) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
  } else {
    store.requestReveal()
  }
}

function openBrowserStart() {
  if (!canOpenBrowser.value) return
  closeNewPanelMenu()
  openBrowserUrl('about:blank', true)
}

function openWorkspaceFiles() {
  const sessionId = props.sessionId
  if (!canOpenWorkspaceFiles.value) return
  const item = { ...createWorkspaceFilesWorkbenchItem(sessionId), title: t('workbench.newPanel.workspaceFiles') }
  if (!store.openItem(item)) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
  } else {
    store.requestReveal()
  }
}

function openTerminal() {
  const sessionId = props.sessionId
  if (!canOpenTerminal.value) return
  const item = { ...createTerminalWorkbenchItem(sessionId, props.workspaceId
    ? { kind: 'project', workspaceId: props.workspaceId }
    : { kind: 'default-agent', agentId: agentIdFromSessionKey(sessionId) }), title: t('workbench.newPanel.terminal') }
  if (!store.openItem(item)) {
    pushToast(t('workbench.terminal.capacityReached'), { tone: 'warn', duration: 6000 })
  } else {
    store.requestReveal()
  }
}

function openAssistantChat(): WorkbenchItem | null {
  if (!canOpenAssistantChat.value) return null
  const item = createAssistantChatWorkbenchItem(props.sessionId,
    props.workspaceReady ? props.workspaceId || undefined : undefined,
    t('workbench.newPanel.assistantChat'))
  if (!store.openItem(item)) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
    return null
  } else {
    store.requestReveal()
  }
  return item
}

async function launchSideChatCommand(request: SideChatLaunchRequest): Promise<boolean> {
  const workspaceId = props.workspaceId
  const current = () => request.isCurrent() && props.routeActive && props.enabled && !props.modalBlocked
    && request.parentKey === props.sessionId && props.workspaceId === workspaceId
    && sessionReady.value && !panelContextLoading.value && canOpenAssistantChat.value
  if (!props.sessionDurable || !props.conversationStarted) throw new Error(t('chat.slashCommands.side.requiresStarted'))
  if (!current()) throw new Error(t('chat.slashCommands.side.unavailable'))
  // The parent retains only its current hand-off. Discard superseded drafts,
  // while keeping the current closed-tab tombstone so uncertain delivery can
  // never silently become a second child send.
  for (const [id, launch] of sideChatLaunchItems) {
    if (!launch.isCurrent()) sideChatLaunchItems.delete(id)
  }
  let itemId = sideChatLaunchItems.get(request.requestId)?.itemId
  if (itemId && !store.items.some(item => item.id === itemId)) throw new Error(t('chat.slashCommands.side.closed'))
  if (!itemId) {
    const item = openAssistantChat()
    if (!item) return false
    itemId = item.id
    sideChatLaunchItems.set(request.requestId, { itemId, isCurrent: request.isCurrent })
  } else {
    store.activateItem(itemId)
    store.setExpanded(true)
    store.requestReveal()
  }
  closeNewPanelMenu()
  await nextTick()
  if (!current()) return false
  const accepted = await sideChatHandles.get(itemId)?.launchSideChat({ ...request, isCurrent: current }) ?? false
  return accepted
}

function reopenBrowserFromMenu() {
  if (!newPanelMenuOpen.value || !props.routeActive || !props.enabled || props.modalBlocked
    || !store.expanded || !canReopenBrowser.value) return
  closeNewPanelMenu()
  reopenBrowser()
}

function reopenBrowser() {
  const closed = store.closedBrowserItems.find(item => item.scope.type === 'session'
    && item.scope.id === props.sessionId)
  if (!closed) return
  const { contextTargetRef: _context, targetRef: _target, adoptedNativeSurface: _adopted, ...payload } = closed.payload
  const source = store.findMostRecentItem(item => item.kind === 'browser'
    && item.scope.type === 'session' && item.scope.id === props.sessionId
    && typeof item.payload.targetRef === 'string')
  const reopened = { ...closed, payload: { ...payload,
    ...(source ? { contextTargetRef: source.payload.targetRef } : {}) } }
  if (!store.openItem(reopened)) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
  } else {
    store.requestReveal()
  }
}

function onBrowserWorkbenchOpen(event: Event) {
  const detail = (event as CustomEvent<BrowserWorkbenchOpenEventDetail>).detail
  if (!detail || typeof detail !== 'object') return
  if ('url' in detail) {
    if (typeof detail.url === 'string') openBrowserUrl(detail.url)
    return
  }
  if (detail.action !== 'reveal' || !nativeApi || detail.sessionId !== props.sessionId
    || store.activeSessionId !== props.sessionId
    || !props.enabled || !props.routeActive || props.modalBlocked) return
  const retained = store.findMostRecentItem(item => item.kind === 'browser'
    && item.scope.type === 'session' && item.scope.id === props.sessionId)
  if (retained) {
    store.activateItem(retained.id)
    store.setExpanded(true)
    store.requestReveal()
    void nextTick(() => {
      document.querySelector<HTMLInputElement>(
        `[data-workbench-item-id="${CSS.escape(retained.id)}"] .browser-preview__address`,
      )?.focus({ preventScroll: true })
    })
  } else {
    openBrowserStart()
  }
}

const promptAnnotationActions: NonNullable<ArtifactWorkbenchProviderOptions['promptAnnotations']> = {
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
    setActivePage: (sessionKey, pagePath) => artifactPromptAnnotations.setActivePage(sessionKey, pagePath),
    list: (sessionKey, documentId) => artifactPromptAnnotations.draftsForSession(sessionKey).filter(item => item.documentId === documentId),
    get: annotationId => artifactPromptAnnotations.annotations[annotationId],
    isQueued: annotationId => artifactPromptAnnotations.isQueued(annotationId),
    replaceSelection: (annotationId, request) => artifactPromptAnnotations.replaceSelection(annotationId, request),
    restoreReplacement: (original, replacementId) => artifactPromptAnnotations.restoreReplacement(original, replacementId),
}

for (const definition of createArtifactWorkbenchDefinitions({
  artifactContent: artifactWorkbench.content,
  artifactPreviews: artifactWorkbench.previews,
  artifactDocuments,
  promptAnnotations: props.promptAnnotationsEnabled ? promptAnnotationActions : undefined,
  officePromptAnnotations: props.officePromptAnnotationsEnabled ? promptAnnotationActions : undefined,
  baseOrigin,
  confirmPermission: confirmWorkbenchPermission,
  confirmRemoteResources: () => confirm({
    title: t('workbench.artifactPreview.remoteResourcesConfirmTitle'),
    body: t('workbench.artifactPreview.remoteResourcesConfirmBody'),
    primaryLabel: t('workbench.artifactPreview.remoteResourcesConfirmAction'),
    primaryClass: 'btn--primary',
  }),
  currentSessionId: () => store.activeSessionId || props.sessionId,
  prepareAnnotation: prepareWorkbenchAnnotation,
  viewRevision: openHistoricalRevision,
  openLatest: async (item, isCurrent) => {
    const artifact = artifactFromWorkbenchItem(item)
    const ref = typeof artifact?.documentId === 'string'
      ? createWorkbenchResourceRef('document', artifact.documentId)
      : artifactResourceRefFromWorkbenchItem(item)
    if (ref) await openLatestResource(ref, item, isCurrent)
  },
  editCurrentResource: async (ref, item, requestIsCurrent) => {
    const generation = scopeChangeGeneration
    const navigationGeneration = artifactNavigationGeneration
    const identity = gateway?.deliveryIdentity
    const epoch = gateway?.subscriptionEpoch
    const isCurrent = () => requestIsCurrent() && generation === scopeChangeGeneration
      && navigationGeneration === artifactNavigationGeneration && store.activeItemId === item.id
      && props.routeActive && props.enabled && !props.modalBlocked
      && gateway?.deliveryIdentity === identity && gateway?.subscriptionEpoch === epoch
      && store.items.some(candidate => candidate.id === item.id)
    const sessionKey = resourceSessionKey(item)
    if (!sessionKey || !isCurrent()) return
    try {
      const resource = await workbenchResources.resolve(sessionKey, ref)
      if (!isCurrent()) return
      if (!resource) throw artifactProductClientError('RESOURCE_UNSUPPORTED')
      await openWorkbenchResource(resource, item, isCurrent)
    } catch (error) {
      if (isCurrent()) throw error
    }
  },
  getPreviewPreferences: () => readPreviewPreferences(platform),
  publishDocument: async request => {
    await workbenchResources.publishDocument(
      request.sessionKey,
      request.documentId,
      request.revisionId,
      request.name,
    )
    refreshResourceCollectionItem(request.sessionKey)
  },
  platform,
  previewLeasesEnabled: true,
  pushToast: (message, options) => pushToast(message, options),
  savePreviewPreferences: preferences => savePreviewPreferences(platform, preferences),
  t: (key, params) => String(t(key, params || {})),
})) {
  workbenchPanelRegistry.register(definition, { replace: true })
}
workbenchPanelRegistry.register(createWorkbenchResourceCollectionDefinition({
  resources: item => workbenchResources.navigationResources(resourceSessionKey(item)),
  load: async item => {
    const snapshot = await workbenchResources.load(resourceSessionKey(item))
    if (snapshot.error) throw new Error(snapshot.error)
  },
  retain: item => workbenchResources.retain(resourceSessionKey(item)),
  download: downloadWorkbenchResource,
  open: openCollectionResource,
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
if (workspaceReferences || workspaceFiles) {
  workbenchPanelRegistry.register(createWorkspaceFileDefinition(
    workspaceReferences,
    workspaceFiles,
    key => String(t(key)),
    {
      openCurrentDocument: openWorkspaceDocument,
      onAnnotate: prepareWorkbenchAnnotation,
      annotationAvailable: item => {
        const path = workspaceFilePayload(item)?.path || workspaceFileReference(item)?.locator.relativePath || ''
        return /\.html?$/i.test(path)
          ? props.promptAnnotationsEnabled && platform.capabilities.hasNativeWorkbenchSurfaces
          : props.officePromptAnnotationsEnabled
      },
    },
  ), { replace: true })
}
if (workspaceFiles) {
  workbenchPanelRegistry.register(createWorkspaceFilesWorkbenchDefinition(
    workspaceFiles,
    key => String(t(key)),
    openResolvedWorkspaceDocument,
  ), { replace: true })
}
if (terminal) {
  workbenchPanelRegistry.register(createTerminalWorkbenchDefinition(
    terminal,
    key => String(t(key)),
  ), { replace: true })
}
workbenchPanelRegistry.register(createGitChangesWorkbenchDefinition(
  gitAccess,
  key => String(t(key)),
), { replace: true })
if (assistantPortsAvailable) {
  workbenchPanelRegistry.register(createAssistantChatWorkbenchDefinition(
    key => String(t(key)),
  ), { replace: true })
}
workbenchPanelRegistry.register(createToolHistoryFileDefinition(key => String(t(key)), artifactWorkbench.content), { replace: true })
detachRuntime = attachWorkbenchRuntime(store, runtimeManager)
const pageAnnotationSendQueue = new PageAnnotationSendQueue()
let pageAnnotationSendFlush: Promise<void> | null = null
let pageAnnotationSendFlushRequested = false
let pageAnnotationSendRetryTimer: ReturnType<typeof setTimeout> | null = null
let pageAnnotationSendRetryDelay = 250
const stopPromptAnnotationLifecycle = store.onLifecycle(event => {
  // The acceptance response can arrive before the resource/session descriptor
  // is mounted. Open and update are the authoritative handoff boundaries;
  // activation/resume also cover a tab that was already retained but hidden.
  if (
    event.type === 'open'
    || event.type === 'update'
    || event.type === 'activate'
    || event.type === 'resume'
  ) schedulePageAnnotationSendFlush()
})

function resourceSessionKey(item: WorkbenchItem): string {
  return sessionKeyFromWorkbenchItem(item)
}

function openResourceArtifact(
  resource: WorkbenchResource,
  artifact: ArtifactPayload,
  sessionKey: string,
  preparedPreview?: WorkbenchPreviewDescriptor,
  initialSection: 'preview' | 'source' = 'preview',
  ownerItem?: WorkbenchItem,
) {
  const nativeArtifact = resourceUsesNativeHtmlPreview(resource)
  const previewItem = artifactPreviewItemForExplicitOpen({
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
    ...(ownerItem ? { navigationArtifacts: navigationArtifactsFromWorkbenchItem(ownerItem) } : {}),
  })
  // Side resources are authorized by the child, but every tab opened from
  // their preview stays visible in the owning parent's Workbench.
  const opened = store.openItem(ownerItem ? { ...previewItem, scope: ownerItem.scope } : previewItem)
  if (opened && store.activeItemId === previewItem.id) store.requestReveal()
  return opened ? store.items.find(item => item.id === previewItem.id) : undefined
}

function openingIsCurrent(item: WorkbenchItem, requestIsCurrent: () => boolean) {
  const generation = scopeChangeGeneration
  const navigationGeneration = artifactNavigationGeneration
  const identity = gateway?.deliveryIdentity
  const epoch = gateway?.subscriptionEpoch
  return () => requestIsCurrent() && generation === scopeChangeGeneration
    && navigationGeneration === artifactNavigationGeneration
    && gateway?.deliveryIdentity === identity && gateway?.subscriptionEpoch === epoch
    && props.routeActive && props.enabled && !props.modalBlocked
    && store.activeItemId === item.id && store.items.some(candidate => candidate.id === item.id)
}

async function openLatestResource(
  ref: import('@/types/workbenchResources').WorkbenchResourceRef,
  item: WorkbenchItem,
  requestIsCurrent: () => boolean = () => true,
  pagePath?: string,
) {
  const isCurrent = openingIsCurrent(item, requestIsCurrent)
  const sessionKey = item.kind === 'file' ? workspaceFileSessionKey(item) : resourceSessionKey(item)
  if (!sessionKey || !isCurrent()) return false
  const resource = await resolveCurrentDocumentResource(
    (session, resource) => workbenchResources.resolveFresh(session, resource, isCurrent), sessionKey, ref, isCurrent)
  if (!resource || !isCurrent()) return false
  const artifact = await prepareCurrentDocumentPreview(resource, sessionKey, artifactDocuments, isCurrent)
  if (!artifact || !isCurrent()) return false
  if (!resource.capabilities.preview) {
    if (artifactWorkbenchPreviewKind(artifact) !== 'html' && resource.capabilities.download) {
      await downloadWorkbenchResource(resource, item)
      return true
    }
    throw artifactProductClientError('RESOURCE_UNSUPPORTED', {
      reasonCode: workbenchResourceActionReasonCode(resource.capabilities, 'preview') || undefined,
    })
  }
  if (pagePath) artifact.previewPagePath = pagePath
  const opened = openResourceArtifact(resource, artifact, sessionKey, undefined, 'preview', item)
  if (opened && item.kind === 'file' && opened.id !== item.id) store.closeItem(item.id)
  return Boolean(opened)
}

async function openResolvedWorkspaceDocument(file: WorkspaceFile, item: WorkbenchItem, isCurrent: () => boolean) {
  if (!props.workbenchResourcesEnabled || !file.documentTarget
    || !/\.(?:html?|docx|pptx|xlsx)$/i.test(file.path)) return false
  return openLatestResource(createWorkbenchResourceRef('document', file.documentTarget.documentId),
    item, isCurrent, file.documentTarget.pagePath)
}

async function openWorkspaceDocument(item: WorkbenchItem, requestIsCurrent: () => boolean) {
  if (!workspaceFiles || !props.workbenchResourcesEnabled) return false
  const isCurrent = openingIsCurrent(item, requestIsCurrent)
  const file = await resolveWorkspaceDocumentFile(workspaceFiles, item, isCurrent)
  return file ? openResolvedWorkspaceDocument(file, item, isCurrent) : false
}

function openHistoricalRevision(artifact: ArtifactPayload, item: WorkbenchItem) {
  if (!store.items.some(candidate => candidate.id === item.id) || store.activeItemId !== item.id) return
  const document = artifactFromWorkbenchItem(item)
  if (!artifact.revisionId || !artifact.id || !document?.documentId) return
  const historicalArtifact = { ...artifact, documentId: document.documentId,
    sourcePath: document.sourcePath, workbenchOpenIntent: 'history' }
  // A revision can be private. Fetch its authorized revision URL directly;
  // do not resolve its ArtifactStore ID as a public deliverable.
  const preview = artifactPreviewItemForExplicitOpen({
    artifact: historicalArtifact, historical: true,
    nativeHtml: Boolean(platform.capabilities.hasNativeWorkbenchSurfaces && platform.workbench.native),
    resourceIdentity: `revision:${document.documentId}:${artifact.revisionId}`,
    sessionKey: resourceSessionKey(item),
  })
  if (store.openItem({ ...preview, scope: item.scope })) store.requestReveal()
}

async function prepareWorkbenchAnnotation(
  item: WorkbenchItem,
  requestIsCurrent: () => boolean = () => true,
  reselectAnnotationId?: string,
) {
  const generation = scopeChangeGeneration
  const navigationGeneration = artifactNavigationGeneration
  const identity = gateway?.deliveryIdentity
  const isCurrent = () => requestIsCurrent() && generation === scopeChangeGeneration
    && navigationGeneration === artifactNavigationGeneration
    && gateway?.deliveryIdentity === identity && props.routeActive && props.enabled && !props.modalBlocked
    && store.activeItemId === item.id
  const sessionKey = item.kind === 'file' ? workspaceFileSessionKey(item) : resourceSessionKey(item)
  if (!sessionKey || !isCurrent()) return
  let file = workspaceFilePayload(item)
  if (item.kind === 'file' && !file) {
    if (!workspaceFiles) throw artifactProductClientError('RESOURCE_UNSUPPORTED')
    file = await resolveWorkspaceDocumentFile(workspaceFiles, item, isCurrent)
  }
  if (!isCurrent()) return
  const resourceRef = artifactResourceRefFromWorkbenchItem(item)
    || (artifactFromWorkbenchItem(item)?.documentId
      ? createWorkbenchResourceRef('document', String(artifactFromWorkbenchItem(item)?.documentId)) : null)
  if (!file && !resourceRef) throw artifactProductClientError('RESOURCE_UNSUPPORTED')
  const prepared = await workbenchResources.prepareAnnotation(sessionKey, file
    ? { workspaceFile: { path: file.path, workspaceBinding: file.workspaceBinding, expectedRevision: file.revision || '' } }
    : { resourceRef: resourceRef! }).catch(error => {
      if (isCurrent()) throw error
      return null
    })
  if (!isCurrent()) return
  if (!prepared || prepared.disposition !== 'document') throw artifactProductClientError('RESOURCE_UNSUPPORTED')
  const artifact = artifactPayloadFromRevision(prepared.revision)
  artifact.documentId = prepared.document.documentId
  artifact.revisionId = prepared.revision.revisionId
  artifact.sourcePath = prepared.resource.sourcePath
  if (prepared.pageContext?.pagePath) artifact.previewPagePath = prepared.pageContext.pagePath
  const reselectDraft = reselectAnnotationId ? artifactPromptAnnotations.annotations[reselectAnnotationId] : undefined
  if (reselectDraft?.pagePath) artifact.previewPagePath = reselectDraft.pagePath
  await artifactDocuments.load(artifact, sessionKey, { force: true })
  if (!isCurrent()) return
  const loaded = artifactDocuments.snapshot(artifact, sessionKey)
  const workspace = loaded.workspace
  if (!loaded.loaded || loaded.loading || loaded.stale || loaded.error
    || workspace?.source !== 'document-api'
    || workspace.document.documentId !== prepared.document.documentId) {
    throw artifactProductClientError('DOCUMENT_UNAVAILABLE')
  }
  // A concurrent edit can advance the head after preparation. Validate drafts
  // against the preview we actually loaded, and require a fresh preparation
  // before opening selection against a different baseline.
  artifactPromptAnnotations.validateDocumentVersion(sessionKey, workspace.document.documentId,
    workspace.document.headRevisionId, String(workspace.headArtifact.sha256 || ''))
  if (workspace.document.headRevisionId !== prepared.revision.revisionId
    || String(workspace.headArtifact.sha256 || '') !== String(artifact.sha256 || '')) {
    throw artifactProductClientError('DOCUMENT_CHANGED')
  }
  const currentItem = store.items.find(candidate => candidate.id === item.id)
  const currentArtifact = artifactFromWorkbenchItem(currentItem || null)
  const state = runtimeManager.getRenderState(item.id)
  const entrypoint = String(state.workingFileEntrypoint || '')
  const currentPage = previewPagePathFromUrl(String(state.currentUrl || ''), {
    launch_url: String(state.previewLaunchUrl || ''),
    entrypoint,
    page_path: String(state.workingFileInitialPage || ''),
  })
  // Preparation still authorizes the operation and validates the current head.
  // Re-entering selection on that same live page is not a Preview navigation:
  // a new section request would replace its surface and discard in-page state.
  const reuseCurrentPreview = !reselectAnnotationId
    && currentItem?.hostKind === 'native-webcontents'
    && !isFixedArtifactPreview(currentItem) && !preparedPreviewFromWorkbenchItem(currentItem)
    && currentItem.payload.resourceIdentity === workbenchResourceKey(prepared.resource.resource)
    && resourceSessionKey(currentItem) === sessionKey
    && artifactWorkbenchPreviewKind(artifact) === 'html'
    && currentArtifact?.documentId === artifact.documentId
    && currentArtifact?.id === artifact.id
    && currentArtifact?.revisionId === artifact.revisionId
    && !!artifact.sha256 && currentArtifact?.sha256 === artifact.sha256
    && !prepared.baselineUpdated && !prepared.materialized && prepared.resolution.status === 'current'
    && state.previewWorkingDocumentId === artifact.documentId
    && runtimeManager.isNativeSurfaceVisible(item.id)
    && state.nativeSurfaceState === 'ready' && state.annotationAvailable === true
    && state.workingFilePageUnknown !== true
    && !!currentPage && currentPage === (artifact.previewPagePath || entrypoint)
  const opened = reuseCurrentPreview
    ? currentItem
    : openResourceArtifact(prepared.resource, artifact, sessionKey, undefined, 'preview', item)
  if (!opened) return
  if (opened.id !== item.id && item.payload.historical !== true
    && (item.kind === 'file' || isFixedArtifactPreview(item))) store.closeItem(item.id)
  artifactPromptAnnotations.setActiveDocument(sessionKey, prepared.document.documentId)
  artifactPromptAnnotations.setActivePage(sessionKey, prepared.pageContext?.pagePath)
  if (prepared.baselineUpdated || (artifactFromWorkbenchItem(item)?.revisionId
    && artifactFromWorkbenchItem(item)?.revisionId !== artifact.revisionId)) {
    pushToast(t('workbench.annotationUnified.versionChanged'), { tone: 'info' })
  }
  // Do not await the same runtime queue from inside its prepare action.
  const openedNavigationGeneration = artifactNavigationGeneration
  void nextTick(async () => {
    const stillCurrent = () => generation === scopeChangeGeneration && gateway?.deliveryIdentity === identity
      && openedNavigationGeneration === artifactNavigationGeneration
      && props.routeActive && store.activeItemId === opened.id && store.items.some(candidate => candidate.id === opened.id)
    await runtimeManager.flush(opened.id)
    for (let attempt = 0; attempt < 60 && stillCurrent(); attempt++) {
      const state = runtimeManager.getRenderState(opened.id)
      if (artifactWorkbenchPreviewKind(artifact) !== 'html' || state.annotationAvailable === true) {
        if (reselectAnnotationId) runtimeManager.handleComponentEvent(opened, {
          type: 'artifact-annotation-summary', payload: { type: 'reselect', annotationId: reselectAnnotationId, prepared: true },
        })
        else runtimeManager.performAction(opened, 'begin-prepared-annotation')
        return
      }
      await new Promise(resolve => window.setTimeout(resolve, 50))
    }
    if (stillCurrent()) pushToast(t('workbench.annotationUnified.unavailable'), { tone: 'warn' })
  })
}

async function openCollectionResource(resource: WorkbenchResource, item: WorkbenchItem) {
  await openLatestResource(resource.resource, item)
}

async function openWorkbenchResource(
  resource: WorkbenchResource,
  item: WorkbenchItem,
  isCurrent: () => boolean = () => true,
) {
  const sessionKey = resourceSessionKey(item)
  if (!sessionKey || !isCurrent()) return
  const current = await workbenchResources.openCurrent(sessionKey, resource)
  if (!isCurrent()) return
  if (current?.disposition === 'document') {
    refreshResourceCollectionItem(sessionKey)
    const artifact = await prepareCurrentDocumentPreview(current.resource, sessionKey, artifactDocuments, isCurrent)
    if (!artifact || !isCurrent()) return
    artifact.documentId = current.document.documentId
    artifact.revisionId = current.revision.revisionId
    artifact.sourcePath = current.resource.sourcePath
    openResourceArtifact(current.resource, artifact, sessionKey, undefined, 'preview', item)
    return
  }
  if (!current && resource.resource.type === 'document') {
    openResourceArtifact(
      resource,
      artifactPayloadFromWorkbenchResource(resource),
      sessionKey,
      undefined,
      'preview',
      item,
    )
    return
  }
  if (
    !current
    && resource.resource.type !== 'document'
    && resource.capabilities.edit
    && resource.sha256
  ) {
    await importWorkbenchResourceForSession(resource, sessionKey, isCurrent, item)
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
      undefined,
      'preview',
      item,
    )
    return
  }
  const preview = await workbenchResources.preview(sessionKey, readonlyResource.resource)
  if (!isCurrent()) return
  const resolved = preview ? resourceFromPreparedPreview(preview) : readonlyResource
  openResourceArtifact(
    resolved,
    artifactPayloadFromWorkbenchResource(resolved),
    sessionKey,
    resolved.resource.type === 'document' ? undefined : preview?.preview,
    'preview',
    item,
  )
}

async function importWorkbenchResourceForSession(
  resource: WorkbenchResource,
  sessionKey: string,
  isCurrent: () => boolean = () => true,
  ownerItem?: WorkbenchItem,
) {
  if (!isCurrent()) return
  const imported = await workbenchResources.importDocument(sessionKey, resource)
  if (!isCurrent()) return
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
  }, artifact, sessionKey, undefined, 'preview', ownerItem)
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
}

async function downloadWorkbenchResource(
  resource: WorkbenchResource,
  item: WorkbenchItem,
) {
  const sessionKey = resourceSessionKey(item)
  const resolved = sessionKey
    ? await workbenchResources.resolve(sessionKey, resource.resource) || resource
    : resource
  const inlineAttachment = resolved.resource.type === 'attachment'
    && typeof resolved.downloadUrl === 'string'
    && resolved.downloadUrl.startsWith('data:')
  const artifact = artifactPayloadFromWorkbenchResource(resolved)
  const result = await artifactWorkbench.content.fetchArtifact(artifact, {
    sessionKey,
  })
  if (!result.ok) throw new Error(result.message)
  const blob = result.blob
  if (inlineAttachment) {
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
  }
  downloadBlob(blob, resolved.name)
}

function refreshResourceCollectionItem(sessionKey: string) {
  const itemId = resourceCollectionWorkbenchItemId(sessionKey)
  const existing = store.items.find(item => item.id === itemId)
  if (!existing) return
  store.updateItem(refreshResourceCollectionWorkbenchItem(
    existing,
    workbenchResources.navigationResources(sessionKey),
    t('workbench.resources.title'),
  ))
}

async function refreshArtifactDocumentItem(
  item: WorkbenchItem,
) {
  if (isFixedArtifactPreview(item)) return
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
  if (workspace.source === 'document-api') {
    artifactPromptAnnotations.validateDocumentVersion(sessionKey, workspace.document.documentId,
      workspace.document.headRevisionId, String(workspace.headArtifact.sha256 || ''))
  }
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
    if (!artifact || isFixedArtifactPreview(item) || sessionKeyFromWorkbenchItem(item) !== sessionKey) continue
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
    visible: active && store.isVisible,
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
  const resolved = workbenchPanelRegistry.resolve(item)?.getProps?.(
    item,
    panelRenderState(item, active, nativeSurface),
  ) || {}
  if (item.kind !== 'browser') return resolved
  return {
    ...resolved,
    active,
    canReopen: store.closedBrowserItems.some(closed => closed.scope.type === 'session'
      && item.scope.type === 'session' && closed.scope.id === item.scope.id),
  }
}

function artifactNavigationItems(
  item: WorkbenchItem | null,
): readonly ArtifactPayload[] {
  if (item?.payload.historical === true) return []
  const seen = new Set<string>()
  return previewableNavigationArtifactsFromWorkbenchItem(item)
    .filter(artifact => {
      const key = artifactNavigationId(item, artifact)
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
}

function artifactNavigationId(
  item: WorkbenchItem | null,
  artifact: ArtifactPayload,
): string {
  const known = artifact.id ? workbenchResources.find(sessionKeyFromWorkbenchItem(item),
    createWorkbenchResourceRef('deliverable', String(artifact.id))) : null
  const documentId = known?.relations.documentId || (artifact.source === 'workspace-preview'
    ? String(artifact.documentId || artifact.document_id || '') : '')
  const resourceIdentity = documentId ? `document:${documentId}`
    : artifact.id ? `deliverable:${artifact.id}` : undefined
  return artifactWorkbenchItemId(sessionKeyFromWorkbenchItem(item), artifact, resourceIdentity)
}

function showArtifactNavigation(item: WorkbenchItem | null): boolean {
  const artifacts = artifactNavigationItems(item)
  return Boolean(item && artifacts.length > 1
    && artifacts.some(artifact => artifactNavigationId(item, artifact) === item.id))
}

async function selectNavigationArtifact(
  item: WorkbenchItem | null,
  event: Event,
) {
  if (!item) return
  const select = event.currentTarget as HTMLSelectElement
  const artifact = artifactNavigationItems(item).find(
    candidate => artifactNavigationId(item, candidate) === select.value,
  )
  if (!artifact || select.value === item.id) return
  // The active item changes only after a successful open, including canonical
  // document resolution. Keep failed requests from looking like a selection.
  select.value = item.id
  const navigationArtifacts = navigationArtifactsFromWorkbenchItem(item)
  const sessionKey = sessionKeyFromWorkbenchItem(item)
  const visibleSessionKey = item.scope.type === 'session' ? item.scope.id : ''
  const navigationGeneration = ++artifactNavigationGeneration
  const scopeGeneration = scopeChangeGeneration
  const identity = gateway?.deliveryIdentity
  const epoch = gateway?.subscriptionEpoch
  const isCurrent = () => navigationGeneration === artifactNavigationGeneration
    && scopeGeneration === scopeChangeGeneration
    && gateway?.deliveryIdentity === identity && gateway?.subscriptionEpoch === epoch
    && props.routeActive && props.sessionId === visibleSessionKey
    && store.activeSessionId === visibleSessionKey && store.activeItemId === item.id
    && store.items.some(candidate => candidate.id === item.id)
  if (!isCurrent()) return
  const kind = artifactWorkbenchPreviewKind(artifact)
  const artifactId = typeof artifact.id === 'string' ? artifact.id.trim() : ''
  const documentId = typeof artifact.documentId === 'string'
    ? artifact.documentId.trim()
    : typeof artifact.document_id === 'string' ? artifact.document_id.trim() : ''
  if (props.workbenchResourcesEnabled && sessionKey && (artifactId || (artifact.source === 'workspace-preview' && documentId))
    && (kind === 'html' || isOfficePreviewFormat(kind))) {
    try {
      const workingFile = artifact.source === 'workspace-preview' && documentId
      const resource = await resolveCurrentDocumentResource(
        (session, resource) => workbenchResources.resolveFresh(session, resource, isCurrent), sessionKey, createWorkbenchResourceRef(
        workingFile ? 'document' : 'deliverable', workingFile || artifactId,
      ), isCurrent)
      if (!isCurrent()) return
      if (resource) {
        const previewArtifact = await prepareCurrentDocumentPreview(resource, sessionKey, artifactDocuments, isCurrent)
        if (!previewArtifact || !isCurrent()) return
        openResourceArtifact(resource, previewArtifact, sessionKey,
          undefined, 'preview', item)
        return
      }
      // A Gateway without the resource API retains the established legacy path.
    } catch (error) {
      if (!isCurrent()) return
      const classified = classifyArtifactProductError(error)
      const translated = t(classified.messageKey)
      pushToast(translated === classified.messageKey ? classified.fallbackMessage : translated,
        { tone: 'danger', duration: 9000 })
      return
    }
  }
  if (!isCurrent()) return
  const previewItem = artifactPreviewItemForExplicitOpen({
    artifact,
    navigationArtifacts,
    nativeHtml: Boolean(
      platform.capabilities.hasNativeWorkbenchSurfaces
      && platform.workbench.native,
    ),
    sessionKey,
    ...(artifactId ? { resourceIdentity: `deliverable:${artifactId}` } : {}),
  })
  if (store.openItem({ ...previewItem, scope: item.scope }) && store.activeItemId === previewItem.id) {
    store.requestReveal()
  }
}

function panelHeader(item: WorkbenchItem | null): WorkbenchPanelHeader {
  if (!item) return { title: t('workbench.newPanel.title') }
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

function visiblePanelToolbarItems(item: WorkbenchItem | null): readonly WorkbenchToolbarItem[] {
  const toolbar = panelToolbarItems(item)
  return item?.kind === 'artifact-preview'
    ? toolbar.filter(action => !['open-external', 'download', 'refresh'].includes(action.id))
    : toolbar
}

function resourceActionArtifact(item: WorkbenchItem): ArtifactPayload | undefined {
  return fileActionArtifactFromWorkbenchItem(item, runtimeManager.getRenderState(item.id))
}

function isActiveAnnotationToolbarItem(toolbarItem: WorkbenchToolbarItem): boolean {
  return toolbarItem.kind === 'action'
    && toolbarItem.id === 'toggle-annotation-mode'
    && toolbarItem.pressed === true
}

function setPanelHandle(item: WorkbenchItem, value: unknown) {
  if (item.kind === 'assistant-chat') {
    if (value && typeof (value as SideChatLaunchHandle).launchSideChat === 'function') {
      sideChatHandles.set(item.id, value as SideChatLaunchHandle)
    } else sideChatHandles.delete(item.id)
  }
  runtimeManager.setComponentHandle(item, value)
}

function panelRefSetter(item: WorkbenchItem): (value: unknown) => void {
  return value => setPanelHandle(item, value)
}

function panelEventHandler(item: WorkbenchItem) {
  const renderedFingerprint = thumbnailFingerprint(item)
  return (event: WorkbenchComponentEvent) => {
    // Store updates precede Vue's prop update. A pending old renderer can still
    // emit in that gap, so bind its image to the revision rendered by this slot.
    if (event.type === 'preview-thumbnail' && renderedFingerprint !== thumbnailFingerprint(item)) return
    handlePanelEvent(item, event)
  }
}

function handlePanelEvent(item: WorkbenchItem, event: WorkbenchComponentEvent) {
  if (event.type === 'browser-reopen') {
    if (item.kind === 'browser' && store.activeItemId === item.id
      && item.scope.type === 'session' && item.scope.id === props.sessionId) reopenBrowser()
    return
  }
  if (event.type === 'preview-thumbnail' && event.payload instanceof Blob) {
    // The Office navigator already rendered this image for the visible document.
    // Store a bounded independent URL so closing its renderer cannot break the tab card.
    if (store.items.some(candidate => candidate.id === item.id)) {
      thumbnailCache.save(item.id, thumbnailFingerprint(item), event.payload)
      thumbnailVersion.value++
    }
    return
  }
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
    // Query in preference order: a combined selector sorts by DOM position,
    // which lets an earlier Deliverables button steal the pane toggle's focus.
    const selectors = [
      '[data-testid="topbar-workbench-toggle"]',
      '[data-testid="chat-session-action-workbench"]',
      '[data-testid="chat-header-primary-action"][data-action="workbench"]',
      '[data-testid="chat-session-action-deliverables"]',
      '[data-testid="chat-header-primary-action"][data-action="deliverables"]',
      '.chat-textarea',
    ]
    for (const selector of selectors) {
      const target = [...document.querySelectorAll<HTMLElement>(selector)].find(element =>
        element.getClientRects().length > 0 && !element.closest('[inert], [aria-hidden="true"]')
        && !element.matches(':disabled'))
      if (!target) continue
      target.focus({ preventScroll: true })
      return
    }
  })
}

function onSurfaceRect(rect: NativeSurfaceRect) {
  runtimeManager.handleSurfaceRect(rect)
}

function onNativeSurfaceEvent(event: NativeWorkbenchSurfaceEvent) {
  if (event.type === 'browser-closed') {
    const item = store.items.find(candidate => candidate.id === event.surfaceId)
    if (item?.kind === 'browser' && item.scope.type === 'session'
      && item.scope.id === event.detail?.sessionKey) store.closeItem(item.id)
    return
  }
  if (event.type === 'browser-opened') {
    const detail = event.detail
    if (!detail?.url || !detail.sessionKey || !nativeApi) return
    const existing = store.items.find(candidate => candidate.id === event.surfaceId)
    if (existing && (existing.kind !== 'browser' || existing.scope.type !== 'session'
      || existing.scope.id !== detail.sessionKey)) return
    const item = createBrowserWorkbenchItem({ scopeId: detail.sessionKey, url: detail.url })
    if (!item) return
    item.id = event.surfaceId
    item.title = detail.url === 'about:blank' ? t('workbench.browser.newTab') : detail.title || item.title
    item.payload = { ...item.payload, adoptedNativeSurface: true, targetRef: detail.targetRef,
      ...(detail.navigationError ? { navigationError: detail.navigationError } : {}) }
    store.openItem(item, { activate: props.enabled && props.routeActive
      && detail.sessionKey === props.sessionId
      && detail.sessionKey === store.activeSessionId })
    return
  }
  const browserItem = store.items.find(candidate => candidate.id === event.surfaceId)
  if (browserItem?.kind === 'browser') {
    if (event.detail?.sessionKey && (browserItem.scope.type !== 'session'
      || browserItem.scope.id !== event.detail.sessionKey)) return
  }
  if (event.type === 'navigation-state' && event.detail?.url) {
    const item = store.items.find(candidate => candidate.id === event.surfaceId)
    const url = normalizeBrowserUrl(event.detail.url)
    if (item?.kind === 'browser' && url) {
      store.updateItem({
        ...item,
        title: url === 'about:blank' ? t('workbench.browser.newTab') : event.detail.title
          || (item.payload.initialUrl === 'about:blank' ? new URL(url).hostname : item.title),
        payload: { ...item.payload, initialUrl: url,
          ...(event.detail.targetRef ? { targetRef: event.detail.targetRef } : {}),
          ...(event.detail.navigationError !== undefined ? { navigationError: event.detail.navigationError } : {}) },
      })
    }
  }
  runtimeManager.handleNativeSurfaceEvent(event)
  if (browserItem?.kind === 'artifact-preview' && browserItem.hostKind === 'native-webcontents') {
    if (event.type === 'loading' || event.type === 'error' || event.type === 'crashed') {
      nativeArtifactSnapshots.invalidate(browserItem.id, event.type === 'loading')
    } else if (event.type === 'ready' && store.activeItemId === browserItem.id && store.isVisible) {
      void nativeArtifactSnapshots.ready(browserItem.id, thumbnailFingerprint(browserItem))
    }
  }
}

function onArtifactState(event: ArtifactDocumentChange) {
  const documentId = event.documentId
  const activeSessionKey = store.activeSessionId || props.sessionId
  if (props.workbenchResourcesEnabled && activeSessionKey) {
    void workbenchResources.load(activeSessionKey, true).then(() => {
      refreshResourceCollectionItem(activeSessionKey)
    }).catch(() => undefined)
  }
  for (const item of store.items) {
    const artifact = artifactFromWorkbenchItem(item)
    if (!artifact || isFixedArtifactPreview(item)) continue
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
    if (!artifact || isFixedArtifactPreview(candidate) || sessionKeyFromWorkbenchItem(candidate) !== detail.sessionKey) return false
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
  if (!item || (item.scope.type === 'session' && item.scope.id !== store.activeSessionId)) return null
  if (!store.activateItem(item.id) || store.activeItemId !== item.id) return null
  artifactPromptAnnotations.setActiveDocument(detail.sessionKey, detail.documentId)
  store.setExpanded(true)
  store.requestReveal()
  await nextTick()
  await runtimeManager.flush(item.id)
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const state = runtimeManager.getRenderState(item.id)
    if (
      state.annotationAvailable === true
      && state.nativeSurfaceState === 'ready'
    ) return item
    const artifact = artifactFromWorkbenchItem(item)
    if (props.officePromptAnnotationsEnabled && artifact && isOfficePreviewFormat(artifactWorkbenchPreviewKind(artifact))) return item
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
    if ((!props.promptAnnotationsEnabled && !props.officePromptAnnotationsEnabled) || !detail?.annotationId || !detail.documentId || !detail.sessionKey) {
    detail?.complete?.(false)
    return
  }
  const item = await activatePromptAnnotationItem(detail)
  if (!item) {
    detail.complete?.(false)
    return
  }
  const draft = artifactPromptAnnotations.annotations[detail.annotationId]
  if (draft?.freshness === 'stale') {
    runtimeManager.handleComponentEvent(item, {
      type: 'artifact-annotation-summary', payload: { type: 'edit', annotationId: draft.annotationId },
    })
    await runtimeManager.flush(item.id)
    detail.complete?.(true)
    return
  }
  if (draft?.selection && props.officePromptAnnotationsEnabled) {
    runtimeManager.handleComponentEvent(item, {
      type: 'artifact-annotation-summary', payload: { type: 'edit', annotationId: draft.annotationId },
    })
    await runtimeManager.flush(item.id)
    detail.complete?.(true)
    return
  }
  if (!draft?.targetRef || !draft.locatorHint || !nativeApi?.focusWorkbenchAnnotation) {
    detail.complete?.(false)
    return
  }
  try {
    const result = await nativeApi.focusWorkbenchAnnotation({
      surfaceId: item.id, targetRef: draft.targetRef, locatorHint: draft.locatorHint,
      ...(draft.pagePath ? { pagePath: draft.pagePath } : {}),
    })
    detail.complete?.(result.ok)
  } catch {
    detail.complete?.(false)
  }
}

async function onPromptAnnotationReuse(event: Event) {
  const detail = (event as CustomEvent<ArtifactPromptAnnotationReuseDetail>).detail
  if (
    !props.promptAnnotationsEnabled
    || !detail?.documentId
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

async function deliverPageAnnotationSend(
  item: WorkbenchItem,
  draftIds: readonly string[],
  documentId?: string,
): Promise<boolean> {
  // A descriptor may be replaced while the runtime queue is draining. Keep
  // the acknowledgement queued in that case; the store's update lifecycle
  // will retry against the authoritative descriptor.
  const current = store.items.find(candidate => candidate.id === item.id)
  if (current !== item || item.kind !== 'artifact-preview') return false
  runtimeManager.handleComponentEvent(item, {
    type: 'page-annotations-sent',
    payload: { draftIds: [...draftIds], ...(documentId ? { documentId } : {}) },
  })
  await runtimeManager.flush(item.id)
  if (store.items.find(candidate => candidate.id === item.id) !== item) return false
  // A stale descriptor can pass the identity check while its runtime is still
  // being disposed/recreated.  In that window handleComponentEvent is a
  // no-op; consuming the acknowledgement would leave the native picker active
  // forever because no later event would replay it.  Keep it queued until the
  // matching runtime exists and has processed the event.
  if (!runtimeManager.hasRuntime(item.id)) return false
  const state = runtimeManager.getRenderState(item.id)
  if (state.annotationAckDeferredToNewDraft === JSON.stringify(draftIds)) return true
  // A runtime instance may be present before its provider constructor has
  // published the initial render state. Treat that as an unprocessed event,
  // rather than consuming the acknowledgement from an empty state object.
  if (
    !Object.prototype.hasOwnProperty.call(state, 'annotationMode')
    || !Object.prototype.hasOwnProperty.call(state, 'annotationModeStopping')
  ) return false
  // `setArtifactAnnotationMode(false)` reports failures through render state
  // rather than throwing. Do not consume the acknowledgement until the native
  // picker is confirmed inactive; a later lifecycle/retry can then finish the
  // cleanup instead of leaving the orange picker pressed for the next turn.
  return state.annotationMode !== true && state.annotationModeStopping !== true
}

async function flushPageAnnotationSendQueue(): Promise<void> {
  await nextTick()
  // A session can also have a resource collection or browser tab open. Only
  // artifact previews can consume this event; passing other session-scoped
  // items to the queue would make a no-op look like a failed delivery and
  // retain an already-applied acknowledgement until TTL expiry.
  const artifactItems = store.items.filter(item => item.kind === 'artifact-preview')
  await pageAnnotationSendQueue.flush(
    artifactItems,
    sessionKeyFromWorkbenchItem,
    deliverPageAnnotationSend,
    Date.now(),
    item => {
      const artifact = artifactFromWorkbenchItem(item)
      return artifact ? artifactDocuments.snapshot(artifact, sessionKeyFromWorkbenchItem(item)).workspace?.document.documentId : undefined
    },
  )
  if (pageAnnotationSendQueue.size === 0) {
    pageAnnotationSendRetryDelay = 250
    return
  }
  if (pageAnnotationSendRetryTimer) return
  const delay = pageAnnotationSendRetryDelay
  pageAnnotationSendRetryDelay = Math.min(delay * 2, 4_000)
  pageAnnotationSendRetryTimer = setTimeout(() => {
    pageAnnotationSendRetryTimer = null
    schedulePageAnnotationSendFlush()
  }, delay)
}

function schedulePageAnnotationSendFlush(): void {
  if (pageAnnotationSendRetryTimer) {
    clearTimeout(pageAnnotationSendRetryTimer)
    pageAnnotationSendRetryTimer = null
  }
  if (pageAnnotationSendFlush) {
    pageAnnotationSendFlushRequested = true
    return
  }
  pageAnnotationSendFlush = flushPageAnnotationSendQueue()
    .catch(() => undefined)
    .finally(() => {
      pageAnnotationSendFlush = null
      if (!pageAnnotationSendFlushRequested) return
      pageAnnotationSendFlushRequested = false
      schedulePageAnnotationSendFlush()
    })
}

async function onPageAnnotationsSent(event: Event) {
  const detail = (event as CustomEvent<PageAnnotationsSentDetail>).detail
  if (!detail?.sessionKey || detail.draftIds.length === 0) return
  // Chat acceptance can update the resource descriptor after this event. Keep
  // the acknowledgement until an artifact item exists instead of relying on
  // a fixed number of render ticks.
  if (pageAnnotationSendQueue.enqueue(detail)) {
    schedulePageAnnotationSendFlush()
  }
}

async function beforeCloseItem(
  item: WorkbenchItem,
  options?: WorkbenchBeforeCloseOptions,
): Promise<boolean> {
  if (item.kind === 'assistant-chat' && item.scope.type === 'session') {
    if (!assistantSessionLifecycle || !gateway?.deliveryIdentity) return false
    try {
      await closeAssistantChatOwner({ identity: gateway.deliveryIdentity, parentKey: item.scope.id,
        ownerId: String(item.payload.ownerId || item.id), lifecycle: assistantSessionLifecycle })
      return true
    } catch (error) {
      pushToast(error instanceof Error ? error.message : t('workbench.assistantChat.unavailable'), { tone: 'danger', duration: 9000 })
      return false
    }
  }
  const nativeClose = await requestNativeBrowserClose(item, nativeApi)
  if (nativeClose !== null) return nativeClose
  const accepted = await runtimeManager.beforeClose(item, options)
  if (!accepted) {
    pushToast(t('workbench.artifactDocument.sourceUnavailable'), {
      tone: 'danger',
      duration: 9000,
    })
  }
  return accepted
}

function setSessionScopeSafely(sessionId: string | null) {
  if (store.activeSessionId === sessionId) return
  scopeChangeGeneration += 1
  // A scope switch is a presentation transaction, not a close transaction.
  // Retained items still own their document/resource snapshots and authorization.
  store.setSessionScope(sessionId)
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
    if (props.routeActive && sessionKey) {
      void workbenchResources.load(sessionKey, true).then(() => {
        refreshResourceCollectionItem(sessionKey)
      }).catch(() => undefined)
    }
  },
)

onMounted(() => {
  document.addEventListener('pointerdown', onNewPanelOutside, true)
  window.addEventListener('resize', onNewPanelViewportChange)
  window.addEventListener('scroll', onNewPanelViewportChange, true)
  if (nativeApi) stopSurfaceEvents = nativeApi.onSurfaceEvent(onNativeSurfaceEvent)
  const documentChanges = artifactWorkbench.subscribeDocumentChanges(onArtifactState)
  stopArtifactEvents = () => documentChanges.close()
  const sessionKey = store.activeSessionId || props.sessionId
  if (props.routeActive && sessionKey) {
    refreshOpenArtifactDocuments(sessionKey)
  }
  window.addEventListener(BROWSER_WORKBENCH_OPEN_EVENT, onBrowserWorkbenchOpen)
  window.addEventListener(ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT, onPromptAnnotationFocus)
  window.addEventListener(ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT, onPromptAnnotationReuse)
  window.addEventListener(
    PAGE_ANNOTATIONS_SENT_EVENT,
    onPageAnnotationsSent,
  )
})

onBeforeUnmount(() => {
  releaseSideChatLauncher?.()
  sideChatHandles.clear()
  sideChatLaunchItems.clear()
  panelOpeningGeneration += 1
  cancelPanelRefreshWait?.()
  document.removeEventListener('pointerdown', onNewPanelOutside, true)
  window.removeEventListener('resize', onNewPanelViewportChange)
  window.removeEventListener('scroll', onNewPanelViewportChange, true)
  closeNewPanelMenu()
  thumbnailCache.clear()
  nativeArtifactSnapshots.clear()
  artifactNavigationGeneration += 1
  stopPromptAnnotationLifecycle()
  stopAdmissionNotifications()
  pageAnnotationSendQueue.clear()
  if (pageAnnotationSendRetryTimer) clearTimeout(pageAnnotationSendRetryTimer)
  pageAnnotationSendRetryTimer = null
  pageAnnotationSendFlushRequested = false
  window.removeEventListener(BROWSER_WORKBENCH_OPEN_EVENT, onBrowserWorkbenchOpen)
  window.removeEventListener(ARTIFACT_PROMPT_ANNOTATION_FOCUS_EVENT, onPromptAnnotationFocus)
  window.removeEventListener(ARTIFACT_PROMPT_ANNOTATION_REUSE_EVENT, onPromptAnnotationReuse)
  window.removeEventListener(
    PAGE_ANNOTATIONS_SENT_EVENT,
    onPageAnnotationsSent,
  )
  stopSurfaceEvents?.()
  stopSurfaceEvents = null
  stopArtifactEvents?.()
  stopArtifactEvents = null
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
  position: relative;
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

.app-workbench__new-panel-menu {
  position: relative;
  flex: 0 0 auto;
}

.app-workbench__new-panel-popover {
  position: fixed;
  z-index: 10002;
  display: flex;
  box-sizing: border-box;
  width: min(280px, calc(100vw - 16px));
  max-height: calc(100dvh - 16px);
  overflow-y: auto;
  flex-direction: column;
  gap: 2px;
  padding: 5px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  box-shadow: var(--shadow-lg);
}

.app-workbench__new-panel-option {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: var(--sp-2);
  padding: 8px 10px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: start;
}

.app-workbench__new-panel-option:not(:disabled):hover,
.app-workbench__new-panel-option:focus-visible {
  background: var(--bg-hover);
  outline: 1px solid var(--accent);
}

.app-workbench__new-panel-option:disabled,
.app-workbench__new-panel-option.is-disabled {
  cursor: default;
  color: var(--text-dim);
}

.app-workbench__new-panel-option-copy {
  display: grid;
  min-width: 0;
  gap: 2px;
  font-size: var(--fs-sm);
}

.app-workbench__new-panel-option-copy small {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 400;
}

.app-workbench__start {
  display: flex;
  flex: 1;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  gap: var(--sp-4);
  padding: var(--sp-5);
  overflow-y: auto;
}

.app-workbench__start h2 {
  margin: 0;
  color: var(--text);
  font-size: var(--fs-lg);
}

.app-workbench__start-options {
  display: grid;
  width: 100%;
  max-width: 380px;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--sp-3);
}

.app-workbench__start-option {
  min-height: 68px;
  align-items: center;
  padding: var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}

.app-workbench__start-option .app-workbench__new-panel-option-copy {
  /* Fit the label and one-line pending hint inside the same 68px card. */
  line-height: 1.4;
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

.app-workbench__action--annotation {
  width: auto;
  flex: 0 0 auto;
  gap: var(--sp-1);
  padding: 0 var(--sp-2);
  border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  color: var(--accent);
  font: inherit;
  font-size: var(--fs-xs);
  font-weight: 600;
  white-space: nowrap;
}

.app-workbench__action--annotation:hover,
.app-workbench__action--annotation.is-active {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 15%, transparent);
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

.app-workbench__warning--neutral {
  color: var(--text-muted);
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
