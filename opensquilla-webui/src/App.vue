<template>
  <!-- Sidebar -->
  <nav
    ref="sidebarRef"
    class="sidebar"
    :class="{
      docked: effectiveSidebarOpen,
      'sidebar--drawer': isSidebarDrawer,
    }"
    :inert="!effectiveSidebarOpen"
    :aria-hidden="effectiveSidebarOpen ? undefined : 'true'"
    :aria-label="t('chrome.primaryNav')"
    id="sidebar-nav"
  >
    <!-- Brand -->
    <div class="sidebar-brand">
      <div class="sidebar-brand-lockup">
        <img class="sidebar-brand-mark" :src="brandMarkUrl" alt="" aria-hidden="true" />
        <span class="sidebar-brand-text">OpenSquilla</span>
      </div>
      <button
        ref="sidebarDockToggleRef"
        class="sidebar-dock-toggle"
        :aria-label="t('chrome.collapseSidebar')"
        aria-controls="sidebar-nav"
        :aria-expanded="effectiveSidebarOpen"
        :aria-keyshortcuts="sidebarToggleAriaShortcut"
        aria-describedby="sidebar-toggle-tip-expanded"
        data-testid="sidebar-toggle-expanded"
        @click="toggleDock('sidebar-button')"
      >
        <Icon name="sidebar-visible" :size="18" />
        <span
          id="sidebar-toggle-tip-expanded"
          class="sidebar-toggle-tip sidebar-toggle-tip--sidebar"
          role="tooltip"
        >
          <span>{{ t('chrome.toggleSidebar') }}</span>
          <kbd v-if="sidebarToggleHint">{{ sidebarToggleHint }}</kbd>
        </span>
      </button>
    </div>

    <!-- Always-visible flat nav index. Bounded and self-scrolling under a
         short viewport so it never squeezes Recents, which owns the elastic
         space below; every destination stays a labelled text row. -->
    <div class="sidebar-section sidebar-core" role="navigation" :aria-label="t('chrome.controlNav')">
      <!-- New task leads the index: it opens a draft instantly against the
           default agent (no picker to interrupt the flow) and reads as a row
           rather than a boxed button so the sidebar keeps one rhythm. -->
      <button
        class="sidebar-new-session"
        :title="newChatHint ? `${t('chrome.newTaskTitle')} (${newChatHint})` : t('chrome.newTaskTitle')"
        @click="startNewChatInstant"
      >
        <Icon name="plus" :size="17" />
        <span class="sidebar-new-session__label">{{ t('chrome.newTask') }}</span>
        <!-- Badge tracks the configured binding and hides when the shortcut is
             disabled (Settings → Keyboard), so it never advertises a dead key. -->
        <kbd v-if="newChatHint" class="sidebar-kbd" aria-hidden="true">{{ newChatHint }}</kbd>
      </button>
      <!-- Overview / Skills & Channels / Cron, single-sourced from route
           metadata so the rail, mobile drawer, and palette never drift. -->
      <router-link
        v-for="item in workNav"
        :key="item.path"
        :to="item.path"
        class="sidebar-fn-item"
        :class="{ 'is-active': isPrimaryNavActive(item.path) }"
        :aria-current="isPrimaryNavActive(item.path) ? 'page' : undefined"
        @click="handleNavClick"
      >
        <Icon :name="sidebarNavIcons[item.icon] ?? item.icon" :size="17" />
        <span class="sidebar-fn-label">{{ item.title }}</span>
      </router-link>
    </div>


    <!-- Recent conversations -->
    <SidebarConversations
      :sections="sidebarSections"
      :session-order="sidebarSessionOrder"
      :error="sessionListError"
      :loading="isLoading"
      :loading-more="isLoadingMore"
      :load-more-error="loadMoreError"
      :has-more="hasMore"
      :current-key="sidebarCurrentKey"
      :contract-debug-enabled="contractDebugEnabled"
      :search-hint="commandPaletteHint"
      :can-manage-projects="gatewayAccess.canManageProjectWorkspaces"
      :can-create-projects="gatewayAccess.canChooseProject"
      @select="switchToSession"
      @refresh="loadSidebarData"
      @load-more="loadMoreSessions"
      @rename="onRenameSession"
      @copy-session-link="onCopySidebarSessionLink"
      @delete="onDeleteSession"
      @bulk-delete="onBulkDeleteSessions"
      @reorder="onReorderSidebarSession"
      @session-pin="onPinSidebarSession"
      @new-chat="startNewChatInstant"
      @new-project="openProjectCreator"
      @new-project-task="startProjectTask"
      @project-pin="onProjectPin"
      @project-edit="openProjectEditor"
      @project-delete-history="onProjectDeleteHistory"
      @project-remove="onProjectRemove"
      @search="openCommandPalette"
    />

    <!-- Fixed footer: settings and the desktop update entry -->
    <div class="sidebar-foot">
      <SidebarDesktopUpdate @open="openDesktopUpdateSettings" />
      <button
        type="button"
        class="sidebar-fn-item"
        data-icon="settings"
        @click="openSettings"
      >
        <Icon name="settings-2" :size="17" />
        <span class="sidebar-fn-label">{{ t('chrome.settings') }}</span>
      </button>
    </div>
  </nav>

  <SidebarResizer
    v-if="effectiveSidebarOpen && isSidebarResizable"
    ref="sidebarResizerRef"
    :enabled="effectiveSidebarOpen && isSidebarResizable"
    :width="sidebarEffectiveWidth"
    :min="SIDEBAR_MIN_WIDTH"
    :max="sidebarDynamicMaximum"
    :preference="appStore.sidebarWidthPreference.width"
    :preference-source="appStore.sidebarWidthPreference.source"
    @resize-start="handleSidebarResizeStart"
    @preview="applySidebarPreview"
    @commit="commitSidebarWidth"
    @reset="resetSidebarWidth"
    @collapse="collapseSidebarFromResize"
    @cancel="applySidebarPreview"
    @resize-end="handleSidebarResizeEnd"
  />

  <!-- Drawer scrim is driven by the same runtime mode as JS focus/Escape logic. -->
  <div
    v-if="effectiveSidebarOpen && isSidebarDrawer"
    class="sidebar-scrim"
    role="presentation"
    aria-hidden="true"
    @click="closeSidebarDrawer"
  />

  <CommandPalette
    v-model:open="commandPaletteOpen"
    :recents="sidebarSections"
    @new-chat="onPaletteNewChat"
    @open-settings="onPaletteOpenSettings"
    @toggle-theme="onPaletteToggleTheme"
    @select-session="onPaletteSelectSession"
  />

  <!-- Main content -->
  <div
    id="app-main"
    class="main"
    :inert="effectiveSidebarOpen && isSidebarDrawer"
    :class="{
      docked: effectiveSidebarOpen,
      'main--sidebar-drawer': isSidebarDrawer,
      'main--sidebar-compact': sidebarLayoutMode === 'compact',
      'main--chat': isChatRoute,
      'main--chat-sidebar-collapsed': isChatRoute && !effectiveSidebarOpen,
      'main--tabbar-hidden': mobileKeyboardOpen,
    }"
  >
    <div ref="workspaceRef" class="app-workspace" :data-workbench-layout="effectiveWorkbenchLayout">
      <div
        v-show="!mainPaneHidden"
        class="app-chat-pane"
        :class="{ 'app-chat-pane--layout-motion': workbenchMotionEnabled }"
        :inert="workbenchSinglePane || undefined"
        :aria-hidden="workbenchSinglePane ? 'true' : undefined"
      >
    <header ref="topbarRef" class="topbar" :class="{ 'topbar--chat': isChatRoute }" :data-connection-state="effectiveConnectionState">
      <div class="topbar-left">
        <!-- Sidebar toggle — visible when sidebar is collapsed -->
        <button
          v-show="!effectiveSidebarOpen"
          ref="topbarSidebarToggleRef"
          class="sidebar-dock-toggle topbar-toggle"
          :aria-label="sidebarExpandLabel"
          aria-controls="sidebar-nav"
          :aria-expanded="effectiveSidebarOpen"
          :aria-keyshortcuts="sidebarToggleAriaShortcut"
          aria-describedby="sidebar-toggle-tip-collapsed"
          data-testid="sidebar-toggle-collapsed"
          @click="toggleDock('topbar-button')"
        >
          <Icon name="sidebar-hidden" :size="18" />
          <span
            v-if="desktopUpdate.visible.value"
            class="sidebar-update-dot"
            :data-state="desktopUpdateSeverity"
            data-testid="sidebar-update-dot"
            aria-hidden="true"
          ></span>
          <span id="sidebar-toggle-tip-collapsed" class="sidebar-toggle-tip" role="tooltip">
            <span>{{ t('chrome.toggleSidebar') }}</span>
            <kbd v-if="sidebarToggleHint">{{ sidebarToggleHint }}</kbd>
          </span>
        </button>
      </div>
      <!-- App owns the route header and its component tree. Chat only publishes
           reactive state and commands through the typed route-header bridge. -->
      <div
        id="app-route-header"
        class="topbar-route-header"
        data-testid="route-header-host"
      >
        <ChatHeaderActions
          v-if="isChatRoute"
          v-show="chatRouteHeaderVisible"
          ref="chatHeaderActionsRef"
          :title="chatRouteHeaderTitle"
          :copy-state="chatRouteHeaderCopyState"
          :copy-icon="chatRouteHeaderCopyIcon"
          :copy-live-text="chatRouteHeaderCopyLiveText"
          :deliverable-count="chatRouteHeaderDeliverableCount"
          :has-new-deliverable="chatRouteHeaderHasNewDeliverable"
          :share-mode="chatRouteHeaderShareMode"
          :shareable-message-count="chatRouteHeaderShareableMessageCount"
          :find-state="chatRouteHeaderFindState"
          @open-deliverables="chatRouteHeader.invoke('openDeliverables')"
          @start-share="chatRouteHeader.invoke('startShare')"
          @copy-session-link="chatRouteHeader.invoke('copySessionLink')"
          @open-find="chatRouteHeader.invoke('openFind')"
          @update-find-query="chatRouteHeader.invoke('updateFindQuery', $event)"
          @search-find="chatRouteHeader.invoke('searchFind', $event)"
          @navigate-find="chatRouteHeader.invoke('navigateFind', $event)"
          @close-find="chatRouteHeader.invoke('closeFind')"
        />
      </div>
      <div class="topbar-right">
        <ChatSystemStatus
          :layout="systemHeaderLayout"
          :connection-state="effectiveConnectionState"
          :connection-label="connectionStateLabel"
          :connection-notice-visible="connectionNoticeVisible"
          :approval-count="appStore.approvalCount"
          :can-manage-connection="webConfigEnabled"
          @open-connection="openConnectionSettings"
          @open-approval="openBlockedApprovalSession"
        />
        <WorkbenchToggle
          v-if="!workbenchToggleInPanel"
          :enabled="appStore.features.artifactWorkbench === true && isChatRoute"
          :presentation-visible="false"
          allow-empty
          :session-id="workbenchSessionKey"
          :blocked="workbenchModalBlocked"
        />
      </div>
    </header>
      <main
        v-show="!mainPaneHidden"
        :inert="workbenchSinglePane || undefined"
        :aria-hidden="workbenchSinglePane ? 'true' : undefined"
        class="content"
        :class="{ 'content--chat': isChatRoute }"
        id="content"
      >
        <ErrorBoundary @error-captured="clearChatRouteHeaderAfterError">
          <SettingsBackgroundRoute :route="settingsContentRoute">
          <router-view :route="settingsContentRoute" v-slot="{ Component, route }">
            <!-- out-in: one view in the DOM at a time, so pages never overlap (no
                 double-exposure, and never two composers/textareas mid-swap).
                 Console views are kept-alive, so the entering page is instant —
                 out-in no longer incurs the old remount/fetch "dead gap". -->
            <template v-if="route.meta.routeTransition === 'none'">
              <KeepAlive v-if="route.meta.keepAlive" :max="12">
                <component :is="Component" :key="route.meta.viewKey || route.name" />
              </KeepAlive>
              <component v-else :is="Component" :key="route.meta.viewKey || route.name" />
            </template>
            <Transition v-else name="route-fade" mode="out-in">
              <KeepAlive v-if="route.meta.keepAlive" :max="12">
                <component :is="Component" :key="route.meta.viewKey || route.name" />
              </KeepAlive>
              <component v-else :is="Component" :key="route.meta.viewKey || route.name" />
            </Transition>
          </router-view>
          </SettingsBackgroundRoute>
          <router-view v-if="settingsBackgroundRoute" />
        </ErrorBoundary>
      </main>
      </div>
      <AppWorkbench
        @return-chat="narrowWorkbenchRequested = false"
        :available-width="workspaceAvailableWidth"
        :layout-mode="effectiveWorkbenchLayout"
        :motion-enabled="workbenchMotionEnabled"
        :animate-layout="workbenchLayoutMotion"
        :enabled="appStore.features.artifactWorkbench === true"
        :workbench-resources-enabled="(
          appStore.features.documentWorkbenchResources === true
          || appStore.features.artifactPromptAnnotations === true
        )"
        :prompt-annotations-enabled="appStore.features.artifactPromptAnnotations === true"
        :office-prompt-annotations-enabled="appStore.features.officePromptAnnotations === true"
        :route-active="isChatRoute"
        :session-id="workbenchSessionKey"
        :session-loading="chatRouteHeader.model.sessionKey.value !== workbenchSessionKey
          || chatRouteHeader.model.sessionLoading.value"
        :session-durable="chatRouteHeader.model.sessionKey.value === workbenchSessionKey
          && chatRouteHeader.model.sessionDurable.value"
        :session-preparable="chatRouteHeader.model.sessionKey.value === workbenchSessionKey
          && chatRouteHeader.model.sessionPreparable.value"
        :conversation-started="chatRouteHeader.model.sessionKey.value === workbenchSessionKey
          && chatRouteHeader.model.sessionDurable.value && chatRouteHeader.model.visible.value
          && chatRouteHeader.model.shareableMessageCount.value > 0"
        :prepare-session="chatRouteHeader.prepareWorkbenchSession"
        :modal-blocked="workbenchModalBlocked"
        :workspace-id="chatRouteHeader.model.sessionKey.value === workbenchSessionKey
          ? chatRouteHeader.model.workspaceId.value : null"
        :workspace-ready="chatRouteHeader.model.sessionKey.value === workbenchSessionKey
          && chatRouteHeader.model.workspaceReady.value"
        :workspace-resolved="chatRouteHeader.model.sessionKey.value === workbenchSessionKey
          && chatRouteHeader.model.workspaceResolved.value"
      >
        <template #collapse-control>
          <WorkbenchToggle
            v-if="workbenchToggleInPanel"
            :enabled="appStore.features.artifactWorkbench === true && isChatRoute"
            :presentation-visible="true"
            allow-empty
            :session-id="workbenchSessionKey"
            :blocked="workbenchModalBlocked"
          />
        </template>
      </AppWorkbench>
      <ArtifactImageLightbox />
    </div>
  </div>

  <!-- Mobile bottom tab bar (<=768px only; hides while the keyboard is up):
       Chat, Usage, then More for the sidebar drawer with session history,
       navigation, and Settings. -->
  <nav
    class="mobile-tabbar"
    :class="{ 'is-keyboard-open': mobileKeyboardOpen }"
    :inert="effectiveSidebarOpen && isSidebarDrawer"
    :aria-label="t('chrome.primaryMobile')"
  >
    <router-link
      to="/chat"
      class="mobile-tab"
      :class="{ 'is-active': isNavActive('/chat') }"
      @click="handleNavClick"
    >
      <Icon name="chat" :size="20" />
      <span class="mobile-tab__label">{{ t('nav.chat') }}</span>
    </router-link>
    <router-link
      to="/usage"
      class="mobile-tab"
      :class="{ 'is-active': isNavActive('/usage') }"
      @click="handleNavClick"
    >
      <Icon name="usage" :size="20" />
      <span class="mobile-tab__label">{{ t('nav.usage') }}</span>
    </router-link>
    <button
      type="button"
      class="mobile-tab"
      :class="{ 'is-active': isMobileMoreActive }"
      @click="openSidebarDrawer"
    >
      <Icon name="menu" :size="20" />
      <span class="mobile-tab__label">{{ t('chrome.more') }}</span>
    </button>
  </nav>

  <Teleport to="body">
    <ToastHost />
  </Teleport>

  <ConfirmModal />

  <ProjectWorkspaceCreateDialog
    v-if="gatewayAccess.canChooseProject"
    :open="projectCreateOpen && !projectCreateConfirming && !projectSourcePickerOpen"
    :name="projectCreateName"
    :source-path="projectCreateSourcePath"
    :busy="projectCreateBusy"
    :source-picking="projectCreateSourcePicking"
    @update:name="projectCreateName = $event"
    @choose-source="chooseProjectSourceDirectory"
    @close="closeProjectCreator"
    @create="createProjectWorkspace"
  />

  <ProjectWorkspacePickerDialog
    v-if="gatewayAccess.canChooseProject"
    :open="projectCreateOpen && projectSourcePickerOpen"
    :enabled="gatewayAccess.canChooseProject"
    :session-key="currentSessionKey || 'agent:main:webchat:workspace-picker'"
    :initial-path="projectCreateSourcePath"
    @close="projectSourcePickerOpen = false"
    @choose="onProjectSourcePathChosen"
  />

  <ProjectWorkspaceEditDialog
    v-if="gatewayAccess.canManageProjectWorkspaces"
    :key="projectEditInstance"
    :open="Boolean(editingProject)"
    :saving="projectEditSaving"
    :initial-name="editingProject?.name || ''"
    :path="editingProject?.path || ''"
    @close="closeProjectEditor"
    @save="onProjectRename"
  />

  <UpdateBanner />

  <!-- Single app-wide announcer for the pending-approval count. The nav badge
       and topbar pill stay silent (no double-announce); this region carries the
       only spoken update when the count changes. -->
  <p class="app-approval-live" aria-live="polite" role="status">{{ approvalAnnouncement }}</p>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { SettingsBackgroundRoute, useSettingsRouteOverlay } from './router/settingsRouteOverlay'
import { useI18n } from 'vue-i18n'
import { routeTitle } from './router'
import { getPlatform } from '@/platform'
import { copyTextWithFallback } from '@/utils/browser'
import { currentSessionLink } from '@/utils/chat/sessionLinks'
import { useAppStore, type PendingApproval } from './stores/app'
import { GATEWAY_ACCESS_KEY } from './modules/gatewayAccess'
import { PRODUCT_ACTIVITY_KEY } from './modules/productActivity'
import { useProductActivity } from './composables/useProductActivity'
import { useReadinessConnectionSync } from './composables/setup/useReadinessConnectionSync'
import { SESSION_DIRECTORY_KEY } from './modules/sessionDirectory'
import { SESSION_DIRECTORY_CHANGES_KEY } from './modules/sessionDirectoryChanges'
import { SESSION_LIFECYCLE_KEY } from './modules/sessionLifecycle'
import { APPROVAL_CENTER_KEY, type ApprovalEvent, type ApprovalItem, type ApprovalSubscription } from './modules/approvalCenter'
import {
  arrangeSidebarSections,
  useSessions,
  type SessionItem,
  type SessionListLoadResult,
  type SidebarSection,
  type SidebarSectionRow,
} from './composables/useSessions'
import Icon from './components/Icon.vue'
import type { IconName } from './utils/icons'
import ErrorBoundary from './components/ErrorBoundary.vue'
import ToastHost from './components/ToastHost.vue'
import ConfirmModal from './components/ConfirmModal.vue'
import ProjectWorkspaceCreateDialog from './components/ProjectWorkspaceCreateDialog.vue'
import ProjectWorkspaceEditDialog from './components/ProjectWorkspaceEditDialog.vue'
import ProjectWorkspacePickerDialog from './components/ProjectWorkspacePickerDialog.vue'
import UpdateBanner from './components/UpdateBanner.vue'
import SidebarDesktopUpdate from './components/SidebarDesktopUpdate.vue'
import ChatSystemStatus from './components/chat/ChatSystemStatus.vue'
import ChatHeaderActions from './components/chat/ChatHeaderActions.vue'
import SidebarConversations from './components/SidebarConversations.vue'
import SidebarResizer from './components/SidebarResizer.vue'
import CommandPalette from './components/CommandPalette.vue'
import ArtifactImageLightbox from './components/chat/ArtifactImageLightbox.vue'
import AppWorkbench from './components/workbench/AppWorkbench.vue'
import WorkbenchToggle from './components/workbench/WorkbenchToggle.vue'
import { useWorkbenchStore } from './workbench/store'
import { WORKBENCH_SPLIT_MIN_WIDTH, workbenchLayoutMode, workbenchShouldYieldSidebar } from './workbench/layout'
import { useDesktopUpdate } from './composables/useDesktopUpdate'
import { useDesktopUpdatePresentation } from './composables/useDesktopUpdatePresentation'
import { useSidebarLayout } from './composables/useSidebarLayout'
import { useSystemHeaderLayout } from './composables/useSystemHeaderLayout'
import { CONNECTION_NOTICE_DELAY_MS, useDelayedNotice } from './composables/useDelayedNotice'
import { useDocumentEvent } from './composables/useDocumentEvent'
import { hasOpenDialogLayer } from './composables/useDialogA11y'
import {
  provideChatTopbarPopoverCoordinator,
} from './composables/useChatTopbarPopoverCoordinator'
import { provideArtifactImageLightbox, closeImageLightboxOutsideSession } from './composables/chat/useArtifactImageLightbox'
import {
  provideChatRouteHeaderBridge,
  type ChatRouteHeaderHostHandle,
} from './composables/chat/useChatRouteHeaderBridge'
import { useAgentOptions } from './composables/useAgentOptions'
import { useSessionTaskAttention } from './composables/useSessionTaskAttention'
import { useToasts } from './composables/useToasts'
import { useConfirm } from './composables/useConfirm'
import { useProjectWorkspaces } from './composables/useProjectWorkspaces'
import { useProjectWorkspaceEditor } from './composables/useProjectWorkspaceEditor'
import { useFreshTaskDraft } from './composables/useFreshTaskDraft'
import { useNavigation } from './app/useNavigation'
import { bindDesktopSessionDeepLinks } from './app/desktopSessionDeepLinks'
import { normalizeAgentId } from './utils/chat/sessionKeys'
import { effectiveChatConnectionState } from './utils/chat/chatConnectionState'
import { notifyCronRunFinished } from './utils/cron/notifications'
import { installSessionNavigationDiagConsole, recordSessionNavigationDiag } from './utils/chat/sessionNavigationDiag'
import { isMacPlatform } from './utils/browser'
import { useShortcutsStore } from './stores/shortcuts'
import { bindingMatches, formatBinding } from './utils/keychord'
import { SIDEBAR_MIN_WIDTH, type SidebarWidthPreference } from './utils/sidebarLayout'
import { sidebarSessionOrderKeys } from './utils/sidebarDisplayProjection'
import {
  dispatchLocalSessionsDeleted,
  localSessionsDeletedDetail,
  LOCAL_SESSIONS_DELETED_EVENT,
} from './utils/sessionSync'
import { activeTaskWasDeletedWithProjectHistory } from './utils/projectHistory'
import { createAppAutomaticRpc } from './utils/appAutomaticRpc'
import {
  optionalSessionRpcAllowed,
  optionalSessionReadOptions,
} from './composables/chat/sessionBootstrapAdmission'
import { AGENT_CATALOG_KEY } from './modules/agentCatalog'
import {
  CRON_SCHEDULER_KEY,
  type CronRunFinished,
  type CronSubscription,
} from './modules/cronScheduler'
import {
  buildChatSessionTitles,
  isSensibleChatTitle,
  provideChatSessionTitles,
  resolveOptimisticChatTitle,
} from './composables/chat/useChatSessionTitles'

const appStore = useAppStore()
const workbenchStore = useWorkbenchStore()
const platform = getPlatform()
const injectedGatewayAccess = inject(GATEWAY_ACCESS_KEY)
if (!injectedGatewayAccess) throw new Error('GatewayAccess was not provided')
const gatewayAccess = injectedGatewayAccess
useReadinessConnectionSync(gatewayAccess)
const productActivity = inject(PRODUCT_ACTIVITY_KEY)
if (productActivity) useProductActivity(gatewayAccess, productActivity)
const injectedSessionDirectory = inject(SESSION_DIRECTORY_KEY)
if (!injectedSessionDirectory) throw new Error('SessionDirectory was not provided')
const sessionDirectory = injectedSessionDirectory
const injectedSessionDirectoryChanges = inject(SESSION_DIRECTORY_CHANGES_KEY)
if (!injectedSessionDirectoryChanges) throw new Error('SessionDirectoryChanges was not provided')
const sessionDirectoryChanges = injectedSessionDirectoryChanges
const injectedSessionLifecycle = inject(SESSION_LIFECYCLE_KEY)
if (!injectedSessionLifecycle) throw new Error('SessionLifecycle was not provided')
const sessionLifecycle = injectedSessionLifecycle
const injectedApprovalCenter = inject(APPROVAL_CENTER_KEY)
if (!injectedApprovalCenter) throw new Error('ApprovalCenter was not provided')
const approvalCenter = injectedApprovalCenter
const injectedAgentCatalog = inject(AGENT_CATALOG_KEY)
if (!injectedAgentCatalog) throw new Error('AgentCatalog was not provided')
const agentCatalog = injectedAgentCatalog
const injectedCronScheduler = inject(CRON_SCHEDULER_KEY)
if (!injectedCronScheduler) throw new Error('CronScheduler was not provided')
const cronScheduler = injectedCronScheduler
const shortcutsStore = useShortcutsStore()
const artifactImageLightbox = provideArtifactImageLightbox()
const { t } = useI18n()
const $route = useRoute()
const router = useRouter()
const { backgroundRoute: settingsBackgroundRoute, contentRoute: settingsContentRoute } = useSettingsRouteOverlay(router)
// Every transient control in the global topbar shares one active owner. The
// controls render on chat and non-chat routes, so route-scoped coordination
// would allow sibling menus such as Language and Theme to overlap.
const isChatRoute = computed(() => settingsContentRoute.value.path === '/chat' || settingsContentRoute.value.path === '/chat/new')
const workbenchVisible = computed(() => isChatRoute.value
  && appStore.features.artifactWorkbench === true && workbenchStore.expanded)
const topbarPopoverCoordinationEnabled = ref(true)
provideChatTopbarPopoverCoordinator(
  topbarPopoverCoordinationEnabled,
)
const chatRouteHeader = provideChatRouteHeaderBridge()
const {
  sessionKey: chatRouteHeaderSessionKey,
  visible: chatRouteHeaderVisible,
  title: chatRouteHeaderTitle,
  optimisticTitle: chatRouteHeaderOptimisticTitle,
  copyState: chatRouteHeaderCopyState,
  copyIcon: chatRouteHeaderCopyIcon,
  copyLiveText: chatRouteHeaderCopyLiveText,
  deliverableCount: chatRouteHeaderDeliverableCount,
  hasNewDeliverable: chatRouteHeaderHasNewDeliverable,
  shareMode: chatRouteHeaderShareMode,
  shareableMessageCount: chatRouteHeaderShareableMessageCount,
  findState: chatRouteHeaderFindState,
} = chatRouteHeader.model
const chatHeaderActionsRef = ref<ChatRouteHeaderHostHandle | null>(null)
watch(chatHeaderActionsRef, host => chatRouteHeader.setHost(host), { flush: 'sync' })
watch(isChatRoute, active => {
  if (!active) chatRouteHeader.clear()
}, { flush: 'sync' })

function clearChatRouteHeaderAfterError() {
  chatRouteHeader.clear()
}
const sidebarRef = ref<HTMLElement | null>(null)
const sidebarDockToggleRef = ref<HTMLButtonElement | null>(null)
const topbarSidebarToggleRef = ref<HTMLButtonElement | null>(null)
const topbarRef = ref<HTMLElement | null>(null)
type SidebarResizerHandle = { cancel: () => boolean }
const sidebarResizerRef = ref<SidebarResizerHandle | null>(null)

const {
  viewportWidth: shellWidth,
  mode: sidebarLayoutMode,
  dynamicMax: sidebarDynamicMaximum,
  effectiveWidth: sidebarEffectiveWidth,
} = useSidebarLayout()
const isSidebarDrawer = computed(() => sidebarLayoutMode.value === 'drawer')
const isSidebarResizable = computed(() => sidebarLayoutMode.value === 'resizable')
const sidebarResizeActive = ref(false)
const workspaceRef = ref<HTMLElement | null>(null)
const measuredWorkspaceWidth = ref(0)
const sidebarRevealOverride = ref(false)
// Opening a phone drawer is temporary. Window resizing must not rewrite the
// user's desktop dock preference or unexpectedly open a drawer over the chat.
const sidebarDrawerOpen = ref(false)
const sidebarYieldsToWorkbench = computed(() => workbenchShouldYieldSidebar({
  shellWidth: shellWidth.value,
  sidebarWidth: sidebarEffectiveWidth.value + 1,
  workbenchExpanded: workbenchVisible.value,
  sidebarDrawer: isSidebarDrawer.value,
}))
const effectiveSidebarOpen = computed(() => isSidebarDrawer.value
  ? sidebarDrawerOpen.value
  : appStore.sidebarOpen && (!sidebarYieldsToWorkbench.value || sidebarRevealOverride.value))
const workspaceAvailableWidth = computed(() => measuredWorkspaceWidth.value > 0
  ? measuredWorkspaceWidth.value
  : Math.max(0, shellWidth.value - (effectiveSidebarOpen.value && !isSidebarDrawer.value ? sidebarEffectiveWidth.value : 0)))
// Responsive hiding is presentation only. Do not close a child or overwrite
// the user's expanded/width preference when the primary conversation needs space.
const narrowWorkbenchRequested = ref(false)
const workbenchConstrained = computed(() => workspaceAvailableWidth.value < WORKBENCH_SPLIT_MIN_WIDTH)
watch(workbenchConstrained, () => { narrowWorkbenchRequested.value = false }, { flush: 'sync' })
watch(() => workbenchStore.revealRequestId, () => {
  if (workbenchConstrained.value) narrowWorkbenchRequested.value = true
}, { flush: 'sync' })
watch(() => workbenchStore.activeSessionId, () => { narrowWorkbenchRequested.value = false })
watch(() => workbenchStore.expanded, expanded => {
  if (!expanded) narrowWorkbenchRequested.value = false
}, { flush: 'sync' })
const effectiveWorkbenchLayout = computed(() => workbenchLayoutMode({
  availableWidth: workspaceAvailableWidth.value,
  maximized: workbenchStore.maximized,
  explicitReveal: narrowWorkbenchRequested.value,
}))
const workbenchSinglePane = computed(() => workbenchVisible.value
  && effectiveWorkbenchLayout.value === 'single-pane')
// The pane owns its collapse affordance. When responsive layout yields back to
// the conversation, its expand affordance returns to the main header instead.
const workbenchToggleInPanel = computed(() => workbenchVisible.value
  && effectiveWorkbenchLayout.value !== 'main-only')
watch(workbenchToggleInPanel, () => {
  if (!(document.activeElement instanceof HTMLElement)
    || !document.activeElement.matches('[data-testid="topbar-workbench-toggle"]')) return
  void nextTick(() => workspaceRef.value?.querySelector<HTMLElement>(
    '[data-testid="topbar-workbench-toggle"]',
  )?.focus({ preventScroll: true }))
})
const workbenchMotionEnabled = ref(false)
const workbenchLayoutMotion = ref(false)
const mainPaneHidden = computed(() => workbenchSinglePane.value && !workbenchLayoutMotion.value)
let workbenchMotionTimer: ReturnType<typeof setTimeout> | undefined
let workspaceObserver: ResizeObserver | undefined
function measureWorkspace() {
  const width = workspaceRef.value?.getBoundingClientRect().width
  if (width && Number.isFinite(width)) measuredWorkspaceWidth.value = width
}
function stopWorkbenchMotion() {
  clearTimeout(workbenchMotionTimer)
  workbenchMotionEnabled.value = false
  workbenchLayoutMotion.value = false
}
function onWorkspaceViewportResize() {
  stopWorkbenchMotion()
  measureWorkspace()
}
watch(workbenchVisible, visible => {
  workbenchLayoutMotion.value = false
  if (!visible) sidebarRevealOverride.value = false
  if (sidebarResizeActive.value) return
  clearTimeout(workbenchMotionTimer)
  workbenchMotionEnabled.value = true
  workbenchMotionTimer = setTimeout(stopWorkbenchMotion, 240)
}, { flush: 'sync' })
watch(() => workbenchStore.maximized, () => {
  if (!workbenchVisible.value || sidebarResizeActive.value) return
  clearTimeout(workbenchMotionTimer)
  workbenchMotionEnabled.value = true
  workbenchLayoutMotion.value = true
  workbenchMotionTimer = setTimeout(stopWorkbenchMotion, 240)
}, { flush: 'sync' })
watch(effectiveSidebarOpen, (open, previous) => {
  if (open || !previous) return
  const focused = document.activeElement
  const focusWasInSidebar = Boolean(focused && sidebarRef.value?.contains(focused))
    || focused instanceof HTMLElement && focused.matches('.sidebar-resizer')
  if (!focusWasInSidebar) return
  void nextTick(() => {
    const target = workbenchSinglePane.value
      ? workspaceRef.value?.querySelector<HTMLElement>('[data-testid="workbench-return-chat"]')
      : topbarSidebarToggleRef.value
    target?.focus({ preventScroll: true })
  })
})
watch([effectiveSidebarOpen, sidebarEffectiveWidth], () => void nextTick(measureWorkspace))
onMounted(() => {
  measureWorkspace()
  if (typeof ResizeObserver !== 'undefined' && workspaceRef.value) {
    workspaceObserver = new ResizeObserver(measureWorkspace)
    workspaceObserver.observe(workspaceRef.value)
  }
  window.addEventListener('resize', onWorkspaceViewportResize, { passive: true })
})
onUnmounted(() => {
  workspaceObserver?.disconnect()
  window.removeEventListener('resize', onWorkspaceViewportResize)
  clearTimeout(workbenchMotionTimer)
})

function setSidebarCssWidth(width: number) {
  if (!Number.isFinite(width)) return
  document.getElementById('app')?.style.setProperty('--sidebar-width', `${Math.round(width)}px`)
}

// Persisted/pre-set changes are infrequent. Pointer previews bypass App's
// reactive tree and write the same root custom property directly once per rAF.
watch(sidebarEffectiveWidth, width => {
  if (!sidebarResizeActive.value) setSidebarCssWidth(width)
}, { immediate: true })

const APP_SESSION_SYNC_SOURCE = 'app-sidebar'

// Localized connection-state label for the topbar pill and its tooltip. The
// Semantic availability is projected into the existing presentation keys;
// CSS uppercases the result (a no-op for CJK scripts).
const gatewayConnectionPhase = computed(() => {
  if (gatewayAccess.availability === 'available') {
    return gatewayAccess.connectionPhase
      || (gatewayAccess.isResuming || gatewayAccess.connectionHealth === 'suspect' ? 'suspect' : 'healthy')
  }
  return gatewayAccess.availability === 'preparing' ? 'checking' : 'disconnected'
})
const connectionState = computed(() => {
  if (gatewayConnectionPhase.value === 'healthy') return 'connected'
  if (gatewayConnectionPhase.value === 'disconnected') return 'disconnected'
  return 'connecting'
})
const effectiveConnectionState = computed(() => effectiveChatConnectionState(
  connectionState.value,
  appStore.chatLivePhase,
  isChatRoute.value,
))
const connectionStateLabel = computed(() => getPlatform().id === 'web' && gatewayAccess.requiresCredential
  ? t('setup.connection.tokenRequired')
  : gatewayConnectionPhase.value === 'healthy' || gatewayConnectionPhase.value === 'disconnected'
  ? t(`chrome.connectionState.${effectiveConnectionState.value}`)
  : t(`chrome.connectionState.${gatewayConnectionPhase.value}`))
// Only the presentation waits. RPC readiness, sending and recovery keep using
// their real state. A rejected connection cannot recover silently, so show it now.
const connectionNoticeVisible = useDelayedNotice(
  computed(() => effectiveConnectionState.value !== 'connected'),
  computed(() => gatewayAccess.connectionBlocked === true),
  computed(() => gatewayAccess.isRuntimeStarting),
  CONNECTION_NOTICE_DELAY_MS,
)
// afterEach only fires on navigation, so a same-route language switch needs an
// explicit re-localize of the tab title.
watch(() => appStore.locale, () => {
  document.title = `${routeTitle($route)} — OpenSquilla`
})
const {
  allSessions,
  sessionListError,
  isLoading,
  isLoadingMore,
  loadMoreError,
  hasMore,
  loadSessions,
  loadMoreSessions,
  cancelPendingRequests,
  applyConfirmedTitle,
} = useSessions(sessionDirectory)
const { bottomRoutes, workNav } = useNavigation()
const sidebarNavIcons: Partial<Record<IconName, IconName>> = {
  skills: 'sparkles',
  cron: 'clock-3',
  usage: 'chart-no-axes-column',
}
const { pushToast } = useToasts()
const { confirm } = useConfirm()
const projectWorkspaces = useProjectWorkspaces()
const freshTaskDraft = useFreshTaskDraft()
const projectCreateOpen = ref(false)
const projectCreateName = ref('')
const projectCreateSourcePath = ref('')
const projectCreateBusy = ref(false)
const projectCreateSourcePicking = ref(false)
const projectCreateConfirming = ref(false)
const projectSourcePickerOpen = ref(false)
const {
  workspaceId: editingProjectId,
  saving: projectEditSaving,
  instance: projectEditInstance,
  open: openProjectEditor,
  close: closeProjectEditor,
  save: onProjectRename,
} = useProjectWorkspaceEditor({
  allowed: () => gatewayAccess.canManageProjectWorkspaces,
  rename: (workspaceId, name) => projectWorkspaces.renameWorkspace(workspaceId, name),
  onError: error => pushToast(t('workspaces.updateFailed', { error: errorMessage(error) }), { tone: 'danger' }),
})
const editingProject = computed(() =>
  editingProjectId.value
    ? projectWorkspaces.byId.value.get(editingProjectId.value) || null
    : null,
)
watch(
  () => gatewayAccess.canManageProjectWorkspaces,
  allowed => {
    if (allowed) {
      scheduleSessionRefresh()
      return
    }
    projectCreateOpen.value = false
    projectCreateName.value = ''
    projectCreateSourcePath.value = ''
    projectCreateBusy.value = false
    projectCreateSourcePicking.value = false
    projectCreateConfirming.value = false
    projectSourcePickerOpen.value = false
    closeProjectEditor()
  },
)
const desktopUpdate = useDesktopUpdate()
const { summary: desktopUpdateSummary, severity: desktopUpdateSeverity } = useDesktopUpdatePresentation(desktopUpdate)
const sidebarExpandLabel = computed(() => desktopUpdate.visible.value
  ? `${t('chrome.expandSidebar')}, ${desktopUpdateSummary.value}`
  : t('chrome.expandSidebar'))
const webConfigEnabled = getPlatform().capabilities.hasWebConfig

let cronFinishedSubscription: CronSubscription | null = null

function handleCronRunFinished(event: CronRunFinished) {
  notifyCronRunFinished(event, {
    t,
    pushToast,
    openJob: async jobId => {
      const failure = await router.push({ path: '/cron', query: jobId ? { job: jobId } : {} })
      return !failure
    },
    openSession: async sessionKey => {
      await switchToSession(sessionKey, 'cron.reminder_toast')
      return $route.path === '/chat' && $route.query.session === sessionKey
    },
  })
}

installSessionNavigationDiagConsole()

// Shared agents.list state + fetch (singleton) for sidebar session metadata.
const { agents, loadAgents } = useAgentOptions(agentCatalog, optionalSessionReadOptions)
const mobileKeyboardOpen = ref(false)
const commandPaletteOpen = ref(false)
const localChatSessions = ref<Record<string, { effectiveAgentId: string; title: string; updatedAt: number }>>({})
// Pending optimistic renames, keyed by session key; confirmed names enter the
// existing list before the temporary override is removed.
const renameOverrides = ref<Record<string, string>>({})

const chatSessionTitles = computed(() => (
  buildChatSessionTitles(allSessions.value, renameOverrides.value)
))
provideChatSessionTitles(chatSessionTitles)

const brandMarkUrl = computed(() => {
  if (import.meta.env.DEV) return '/opensquilla-mark.png'
  const base = document.getElementById('opensquilla-data')?.dataset.basePath || '/control'
  return `${base.replace(/\/$/, '')}/static/dist/opensquilla-mark.png`
})

// Display chords track the configurable bindings so the rail hint, the New chat
// badge, and the palette never drift from what the handler actually honours. A
// disabled shortcut yields an empty hint (the New chat badge then hides).
const isMac = isMacPlatform()
const commandPaletteHint = computed(() =>
  formatBinding(shortcutsStore.effectiveBinding('command-palette'), isMac))
const newChatHint = computed(() =>
  formatBinding(shortcutsStore.effectiveBinding('new-chat'), isMac))
const sidebarToggleBinding = computed(() => shortcutsStore.effectiveBinding('toggle-sidebar'))
const sidebarToggleHint = computed(() => formatBinding(sidebarToggleBinding.value, isMac))
const sidebarToggleAriaShortcut = computed(() => {
  const binding = sidebarToggleBinding.value
  if (!binding) return undefined
  const parts: string[] = []
  if (binding.primary) parts.push(isMac ? 'Meta' : 'Control')
  if (binding.alt) parts.push('Alt')
  if (binding.shift) parts.push('Shift')
  parts.push(binding.key.length === 1 ? binding.key.toUpperCase() : binding.key)
  return parts.join('+')
})

// Current session key from ChatView via URL
const currentSessionKey = computed(() => {
  return ($route.query.session as string) || ''
})
// The draft already has the key that its first turn will use. Keep manually
// opened browser pages on that identity before the URL gains a session query.
const workbenchSessionKey = computed(() =>
  currentSessionKey.value || chatRouteHeader.model.sessionKey.value,
)
const sessionTaskAttention = useSessionTaskAttention()

function currentSessionIsVisible(): boolean {
  return (
    $route.path === '/chat'
    && document.visibilityState === 'visible'
    && document.hasFocus()
  )
}

function markCurrentSessionReadIfVisible() {
  const sessionKey = currentSessionKey.value
  if (sessionKey && currentSessionIsVisible()) {
    sessionTaskAttention.markRead(sessionKey)
  }
}

watch(currentSessionKey, markCurrentSessionReadIfVisible, {
  flush: 'sync',
  immediate: true,
})

// Shared status controls use the actual topbar geometry on every route.
const systemHeaderPressureCount = computed(() => (
  Number(connectionNoticeVisible.value)
  + Number(appStore.approvalCount > 0)
))
const systemHeaderLayout = useSystemHeaderLayout({
  target: topbarRef,
  active: computed(() => true),
  pressureCount: systemHeaderPressureCount,
})
const activeProjectDraftId = computed(() =>
  $route.path === '/chat/new' ? String($route.query.project || '') : '',
)
const activeProjectDraftKey = computed(() => {
  const workspaceId = activeProjectDraftId.value
  if (!workspaceId) return ''
  const request = freshTaskDraft.request.value
  const requestId = request?.workspaceId === workspaceId ? request.id : 0
  return `draft:project:${workspaceId}:${requestId}`
})
const sidebarCurrentKey = computed(() =>
  currentSessionKey.value || activeProjectDraftKey.value,
)

watch(
  [
    currentSessionKey,
    isChatRoute,
    () => artifactImageLightbox.request.value?.ownerSessionKey || artifactImageLightbox.request.value?.sessionKey || '',
  ],
  ([sessionKey, chatRouteActive]) => {
    closeImageLightboxOutsideSession(artifactImageLightbox, sessionKey, chatRouteActive)
  },
  { flush: 'sync' },
)

// The Settings overlay (route-mounted dialog) is open on these routes. It owns
// its own Escape/focus, so App-level keyboard shortcuts defer to it. Both web
// and desktop mount the same overlay now (webConfigEnabled is true on both).
const settingsOverlayOpen = computed(() =>
  webConfigEnabled && ($route.name === 'settings' || $route.name === 'settings-section'))
const workbenchModalBlocked = computed(() =>
  commandPaletteOpen.value
  || settingsOverlayOpen.value
  || (effectiveSidebarOpen.value && isSidebarDrawer.value))

const contractDebugEnabled = computed(() => appStore.features.contractDebug === true)

function isNavActive(path: string): boolean {
  if (path === '/chat') return isChatRoute.value
  return $route.path === path
}

// Skills fronts the Skills/Channels hub; Usage is a standalone destination.
const SKILLS_CHANNELS_HUB_PATHS = new Set(['/skills', '/channels'])
const MOBILE_MORE_PATHS = new Set(['/skills', '/channels', '/cron'])
const isSkillsChannelsHubActive = computed(() => SKILLS_CHANNELS_HUB_PATHS.has($route.path))
const isMobileMoreActive = computed(() =>
  effectiveSidebarOpen.value || MOBILE_MORE_PATHS.has($route.path))

function isPrimaryNavActive(path: string): boolean {
  if (path === '/skills') return isSkillsChannelsHubActive.value
  return isNavActive(path)
}

function agentDisplayName(agentId: string): string {
  const agent = agents.value.find(a => a.id === agentId)
  return agent?.name || (agentId === 'main' ? 'Main Agent' : agentId)
}

// Raw session keys (agent:…:…) and bare UUIDs must never render in the sidebar.
function sidebarConversationTitle(item: SessionItem): string {
  for (const candidate of [item.title, item.subtitle, item.groupLabel]) {
    const text = String(candidate || '').trim()
    if (isSensibleChatTitle(text)) return text
  }
  return t('shared.sidebar.untitledTask')
}

// A draft / current-session row the backend list does not yet carry. The
// sidebar arranger reads only a handful of fields off the SessionItem, so a
// synthetic chat row carries canonical defaults for the remaining fields.
function syntheticChatSession(
  key: string,
  effectiveAgentId: string,
  title: string,
  updatedAt: number,
  project?: {
    id: string
    name: string
    path: string
    provisional?: boolean
  },
): SessionItem {
  return {
    key,
    title,
    subtitle: '',
    groupLabel: normalizeAgentId(effectiveAgentId),
    workspace: project?.path,
    workspaceId: project?.id,
    workspaceLabel: project?.name,
    workspaceDisplayPath: project?.path,
    effectiveAgentId,
    sessionKind: 'chat',
    surface: 'webchat',
    conversationKind: 'direct',
    status: 'idle',
    runStatus: 'idle',
    runLabel: 'Idle',
    messageCount: null,
    updatedAt,
    model: '',
    parent: null,
    provisional: project?.provisional,
    forkedFromParent: false,
    hasContractGaps: false,
  }
}

function optimisticProjectForSession(key: string) {
  const workspaceId = freshTaskDraft.materializedWorkspaceBySession.value[key]
  return workspaceId ? projectWorkspaces.byId.value.get(workspaceId) || null : null
}

function withOptimisticProjectBinding(item: SessionItem): SessionItem {
  if (item.workspaceId) return item
  const project = optimisticProjectForSession(item.key)
  if (!project) return item
  return {
    ...item,
    workspace: project.path,
    workspaceId: project.id,
    workspaceLabel: project.name,
    workspaceDisplayPath: project.path,
  }
}

// Sessions to arrange into the sidebar: the backend list plus the local draft
// and the current chat session when the list does not carry them yet (both
// injected as Chats so a brand-new conversation appears immediately).
const sidebarSessionItems = computed((): SessionItem[] => {
  const items: SessionItem[] = []
  const seen = new Set<string>()
  for (const item of allSessions.value) {
    if (!item.key || item.key === 'unknown') continue
    seen.add(item.key)
    items.push(withOptimisticProjectBinding(item))
  }
  for (const [key, local] of Object.entries(localChatSessions.value)) {
    if (seen.has(key)) continue
    seen.add(key)
    const project = optimisticProjectForSession(key) || undefined
    items.push(syntheticChatSession(
      key,
      local.effectiveAgentId,
      local.title || t('chrome.newChat'),
      local.updatedAt,
      project,
    ))
  }
  const draftWorkspaceId = activeProjectDraftId.value
  const draftKey = activeProjectDraftKey.value
  const draftProject = draftWorkspaceId
    ? projectWorkspaces.byId.value.get(draftWorkspaceId)
    : null
  if (draftKey && draftProject && !seen.has(draftKey)) {
    seen.add(draftKey)
    items.push(syntheticChatSession(
      draftKey,
      'main',
      t('chrome.newTask'),
      Date.now(),
      {
        id: draftProject.id,
        name: draftProject.name,
        path: draftProject.path,
        provisional: true,
      },
    ))
  }
  const current = currentSessionKey.value
  if (current && !seen.has(current)) {
    const currentAgentId = normalizeAgentId(current.split(':')[1] || 'main')
    const project = optimisticProjectForSession(current) || undefined
    items.push(syntheticChatSession(
      current,
      currentAgentId,
      t('shared.sidebar.currentTask'),
      Date.now(),
      project,
    ))
  }
  return items
})

// A just-materialized chat can reach the route before the next sessions.list
// snapshot. Keep an optimistic ledger entry for it so switching to another
// conversation does not make the new task disappear from the sidebar. When
// ChatView has the first user message, reuse its resolved header title so the
// sidebar shows the same text immediately. The key guard prevents a title from
// the previous ChatView instance leaking into the new session during navigation.
function optimisticCurrentSessionTitle(key: string, allowHeaderTitle = true): string {
  return resolveOptimisticChatTitle(
    key,
    chatRouteHeaderSessionKey.value,
    chatRouteHeaderOptimisticTitle.value,
    chatRouteHeaderTitle.value,
    allowHeaderTitle,
    {
      currentTask: t('shared.sidebar.currentTask'),
      newChat: t('chat.newChat'),
      chatWithSuffix: suffix => t('chat.chatWithSuffix', { suffix }),
    },
  )
}

watch(
  [
    currentSessionKey,
    chatRouteHeaderSessionKey,
    chatRouteHeaderTitle,
    chatRouteHeaderOptimisticTitle,
  ],
  ([key], oldValues) => {
    if (!key || allSessions.value.some(item => item.key === key)) return
    const existing = localChatSessions.value[key]
    const previousKey = oldValues?.[0] || ''
    const previousHeaderKey = oldValues?.[1] || ''
    const sessionChanged = Boolean(
      (previousKey && key && previousKey !== key)
      || (previousHeaderKey
        && chatRouteHeaderSessionKey.value
        && previousHeaderKey !== chatRouteHeaderSessionKey.value),
    )
    const title = optimisticCurrentSessionTitle(key, !sessionChanged)
    if (existing && existing.title === title) return
    const effectiveAgentId = normalizeAgentId(key.split(':')[1] || 'main')
    localChatSessions.value = {
      ...localChatSessions.value,
      [key]: {
        effectiveAgentId: existing?.effectiveAgentId || effectiveAgentId,
        title,
        updatedAt: existing?.updatedAt || Date.now(),
      },
    }
  },
  { flush: 'sync', immediate: true },
)

watch(allSessions, sessions => {
  // Only positive directory confirmation retires a provisional row; an
  // absent key may simply be outside the loaded page.
  removeLocalSessions(new Set(sessions.map(item => item.key)))
  for (const item of sessions) {
    if (!item.key || !item.workspaceId) continue
    freshTaskDraft.confirmMaterializedProjectTask(item.key, item.workspaceId)
  }
})

const SIDEBAR_SESSION_ORDER_KEY = 'opensquilla-sidebar-session-order-v1'
const SIDEBAR_PINNED_SESSIONS_KEY = 'opensquilla-sidebar-pinned-sessions-v1'

function readStoredSessionKeys(storageKey: string): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(storageKey) || '[]')
    if (!Array.isArray(parsed)) return []
    return [...new Set(parsed.filter((key): key is string => typeof key === 'string' && key.length > 0))]
      .slice(0, 1000)
  } catch {
    return []
  }
}

function writeStoredSessionKeys(storageKey: string, keys: readonly string[]) {
  try {
    localStorage.setItem(storageKey, JSON.stringify(keys))
  } catch {
    // Storage can be unavailable in restricted browser contexts.
  }
}

const sidebarSessionOrder = ref<string[]>(readStoredSessionKeys(SIDEBAR_SESSION_ORDER_KEY))
const sidebarPinnedSessionKeys = ref<string[]>(readStoredSessionKeys(SIDEBAR_PINNED_SESSIONS_KEY))

// Collapsible family sections (Chats / Channels / Automations). Row titles and
// agent names are resolved here so the raw-session-id scrub and the display-name
// lookup stay in App.vue; subagents indent under their parent via the helper.
const sidebarSections = computed((): SidebarSection[] => {
  const byKey = new Map(sidebarSessionItems.value.map(item => [item.key, item]))
  return arrangeSidebarSections(
    sidebarSessionItems.value,
    gatewayAccess.canManageProjectWorkspaces && projectWorkspaces.hasLoaded.value
      ? projectWorkspaces.workspaces.value
      : undefined,
    sidebarSessionOrder.value,
    sidebarPinnedSessionKeys.value,
  ).map(section => ({
    ...section,
    rows: section.rows.map((row): SidebarSectionRow => {
      if (row.rowKind !== 'session') return { ...row, agentName: '' }
      const source = byKey.get(row.key)
      const title = renameOverrides.value[row.key]
        || (source ? sidebarConversationTitle(source) : row.title)
      return {
        ...row,
        title,
        agentName: agentDisplayName(normalizeAgentId(row.effectiveAgentId)),
        taskAttention: sessionTaskAttention.attentionFor(row.key, row.runStatus),
      }
    }),
  }))
})

function onReorderSidebarSession(payload: {
  draggedKey: string
  targetKey: string
  position: 'before' | 'after'
}) {
  const orderedKeys = sidebarSessionOrderKeys(
    sidebarSections.value,
    sidebarSessionOrder.value,
  )
  const from = orderedKeys.indexOf(payload.draggedKey)
  if (from < 0 || !orderedKeys.includes(payload.targetKey) || payload.draggedKey === payload.targetKey) return

  orderedKeys.splice(from, 1)
  const target = orderedKeys.indexOf(payload.targetKey)
  orderedKeys.splice(payload.position === 'after' ? target + 1 : target, 0, payload.draggedKey)
  sidebarSessionOrder.value = orderedKeys
  writeStoredSessionKeys(SIDEBAR_SESSION_ORDER_KEY, orderedKeys)
}

function onPinSidebarSession(payload: { key: string; pinned: boolean }) {
  const pinned = new Set(sidebarPinnedSessionKeys.value)
  if (payload.pinned) pinned.add(payload.key)
  else pinned.delete(payload.key)
  sidebarPinnedSessionKeys.value = [...pinned]
  writeStoredSessionKeys(SIDEBAR_PINNED_SESSIONS_KEY, sidebarPinnedSessionKeys.value)

  if (!payload.pinned) return
  const currentOrder = sidebarSessionOrderKeys(
    sidebarSections.value,
    sidebarSessionOrder.value,
  )
    .filter(key => key !== payload.key)
  currentOrder.unshift(payload.key)
  sidebarSessionOrder.value = currentOrder
  writeStoredSessionKeys(SIDEBAR_SESSION_ORDER_KEY, currentOrder)
}

let appAutomaticRpcMounted = false

// Hide the bottom tab bar while the on-screen keyboard owns the bottom edge.
// A visual-viewport shrink well beyond browser-chrome changes (>140px) is the
// simplest cross-platform signal; per-input focus tracking was considered and
// dropped as fragile. When the heuristic misses, the bar just stays visible.
function syncMobileKeyboard() {
  const viewport = window.visualViewport
  if (!viewport) return
  mobileKeyboardOpen.value = window.innerWidth <= 768 && window.innerHeight - viewport.height > 140
}

type SidebarToggleSource = 'sidebar-button' | 'topbar-button' | 'shortcut'

function toggleDock(source: SidebarToggleSource) {
  sidebarResizerRef.value?.cancel()
  const wasOpen = effectiveSidebarOpen.value
  const focusWasInsideSidebar = Boolean(
    sidebarRef.value && document.activeElement && sidebarRef.value.contains(document.activeElement),
  )
  const focusWasOnResizer = document.activeElement instanceof HTMLElement
    && document.activeElement.matches('.sidebar-resizer')
  // An explicit reveal wins over the temporary space-saving presentation.
  // This override lasts only while the workbench is open; it is not a preference.
  if (isSidebarDrawer.value) {
    sidebarDrawerOpen.value = !wasOpen
  } else {
    sidebarRevealOverride.value = !wasOpen && sidebarYieldsToWorkbench.value
    appStore.setSidebarOpen(!wasOpen)
  }
  if (!wasOpen && (source === 'topbar-button' || isSidebarDrawer.value)) {
    void nextTick(() => sidebarDockToggleRef.value?.focus())
  } else if (wasOpen && (source === 'sidebar-button' || focusWasInsideSidebar || focusWasOnResizer)) {
    void nextTick(() => topbarSidebarToggleRef.value?.focus())
  }
}

function handleNavClick() {
  if (isSidebarDrawer.value && effectiveSidebarOpen.value) {
    closeSidebarDrawer()
  }
}

function openSidebarDrawer() {
  if (!isSidebarDrawer.value || effectiveSidebarOpen.value) return
  toggleDock('topbar-button')
}

function closeSidebarDrawer() {
  if (!effectiveSidebarOpen.value || !isSidebarDrawer.value) return
  sidebarResizerRef.value?.cancel()
  sidebarDrawerOpen.value = false
  void nextTick(() => topbarSidebarToggleRef.value?.focus())
}

function handleSidebarResizeStart() {
  stopWorkbenchMotion()
  sidebarResizeActive.value = true
}

function applySidebarPreview(width: number) {
  setSidebarCssWidth(width)
}

function commitSidebarWidth(width: number) {
  const preference: SidebarWidthPreference = {
    version: 1,
    width,
    source: 'custom',
  }
  appStore.setSidebarWidthPreference(preference)
}

function resetSidebarWidth() {
  appStore.resetSidebarWidthPreference()
}

function collapseSidebarFromResize() {
  appStore.setSidebarOpen(false)
  // The collapse gesture never overwrites the saved preference. Reset the root
  // variable now so the next explicit open restores that preference immediately.
  setSidebarCssWidth(sidebarEffectiveWidth.value)
  void nextTick(() => topbarSidebarToggleRef.value?.focus())
}

function handleSidebarResizeEnd() {
  sidebarResizeActive.value = false
  setSidebarCssWidth(sidebarEffectiveWidth.value)
}

// Layout mode is a single state machine shared with Settings. Each transition
// starts with a closed drawer; returning to desktop restores the dock preference.
// Compact mode keeps the current desktop preference as well.
watch(sidebarLayoutMode, (nextMode, previousMode) => {
  const focusWasOnResizer = document.activeElement instanceof HTMLElement
    && document.activeElement.matches('.sidebar-resizer')
  sidebarResizerRef.value?.cancel()
  sidebarResizeActive.value = false
  setSidebarCssWidth(sidebarEffectiveWidth.value)

  sidebarDrawerOpen.value = false
  if (nextMode === 'drawer') {
    const focusWasInsideSidebar = Boolean(
      sidebarRef.value && document.activeElement && sidebarRef.value.contains(document.activeElement),
    )
    if (focusWasOnResizer || focusWasInsideSidebar) {
      void nextTick(() => topbarSidebarToggleRef.value?.focus())
    }
  } else if (previousMode === 'resizable' && focusWasOnResizer) {
    void nextTick(() => sidebarDockToggleRef.value?.focus())
  }
}, { immediate: true })

watch(sidebarDynamicMaximum, () => {
  if (!sidebarResizeActive.value) return
  sidebarResizerRef.value?.cancel()
  sidebarResizeActive.value = false
  setSidebarCssWidth(sidebarEffectiveWidth.value)
})

// Primary new-chat path: ordinary tasks always start against the default Agent.
function openDefaultDraft() {
  freshTaskDraft.requestFreshTask('main')
  return router.push({ path: '/chat/new', query: { agent: 'main' } })
}

function startNewChatInstant() {
  handleNavClick()
  void openDefaultDraft()
}

function startProjectTask(workspaceId: string) {
  if (!workspaceId || !gatewayAccess.canManageProjectWorkspaces) return
  handleNavClick()
  freshTaskDraft.requestFreshTask('main', workspaceId)
  void router.push({
    path: '/chat/new',
    query: { agent: 'main', project: workspaceId },
  })
}

function projectNameFromPath(path: string): string {
  const normalized = path.trim().replace(/[\\/]+$/, '')
  return normalized.split(/[\\/]/).pop() || normalized
}

function resetProjectCreator() {
  projectCreateOpen.value = false
  projectCreateName.value = ''
  projectCreateSourcePath.value = ''
  projectCreateBusy.value = false
  projectCreateSourcePicking.value = false
  projectCreateConfirming.value = false
  projectSourcePickerOpen.value = false
}

function openProjectCreator() {
  if (!gatewayAccess.canChooseProject) return
  projectCreateName.value = ''
  projectCreateSourcePath.value = ''
  projectCreateBusy.value = false
  projectCreateSourcePicking.value = false
  projectCreateConfirming.value = false
  projectSourcePickerOpen.value = false
  projectCreateOpen.value = true
}

function closeProjectCreator() {
  if (projectCreateBusy.value) return
  resetProjectCreator()
}

function onProjectPathChosen(path: string) {
  if (!projectCreateOpen.value || !gatewayAccess.canChooseProject) return
  projectCreateSourcePath.value = path
  if (!projectCreateName.value.trim()) {
    projectCreateName.value = projectNameFromPath(path)
  }
}

function onProjectSourcePathChosen(path: string) {
  projectSourcePickerOpen.value = false
  onProjectPathChosen(path)
}

async function chooseProjectSourceDirectory() {
  if (
    !projectCreateOpen.value
    || !gatewayAccess.canChooseProject
    || projectCreateBusy.value
    || projectCreateSourcePicking.value
    || projectSourcePickerOpen.value
  ) return

  const nativePicker = getPlatform().files.chooseProjectDirectory
  if (typeof nativePicker !== 'function') {
    projectSourcePickerOpen.value = true
    return
  }

  projectCreateSourcePicking.value = true
  try {
    const choice = await nativePicker()
    const selected = String(choice?.path || '').trim()
    if (selected) onProjectPathChosen(selected)
  } catch (err) {
    pushToast(t('workspaces.directoryPickerFailed', { error: errorMessage(err) }), {
      tone: 'danger',
    })
  } finally {
    projectCreateSourcePicking.value = false
  }
}

async function createProjectWorkspace(payload: { name: string; path: string }) {
  if (
    !projectCreateOpen.value
    || !gatewayAccess.canChooseProject
    || projectCreateBusy.value
    || projectCreateSourcePicking.value
    || projectSourcePickerOpen.value
  ) return
  const name = payload.name.trim()
  const path = payload.path.trim()
  if (!name || !path) return
  projectCreateConfirming.value = true
  await nextTick()
  const trusted = await confirm({
    title: t('workspaces.trustTitle'),
    body: t('workspaces.trustBody', { path }),
    primaryLabel: t('workspaces.trustConfirm'),
    primaryClass: 'btn--primary',
  })
  if (!trusted) {
    projectCreateConfirming.value = false
    return
  }
  projectCreateBusy.value = true
  const existingWorkspaceIds = new Set(
    projectWorkspaces.workspaces.value.map(workspace => workspace.id),
  )
  try {
    const workspace = await projectWorkspaces.openWorkspace(path)
    if (!workspace) throw new Error('Gateway returned an empty project.')
    const alreadyExists = existingWorkspaceIds.has(workspace.id)
    const renamedExisting = alreadyExists && workspace.name !== name
    if (workspace.name !== name) {
      await projectWorkspaces.renameWorkspace(workspace.id, name)
    }
    resetProjectCreator()
    if (alreadyExists) {
      pushToast(t(
        renamedExisting
          ? 'workspaces.projectExistingRenamed'
          : 'workspaces.projectAlreadyExists',
        { name },
      ), { tone: 'info' })
    } else {
      pushToast(t('workspaces.projectCreated', { name }), { tone: 'ok' })
    }
  } catch (err) {
    projectCreateBusy.value = false
    projectCreateConfirming.value = false
    pushToast(t('workspaces.createProjectFailed', { error: errorMessage(err) }), { tone: 'danger' })
  }
}

async function onProjectPin(payload: { workspaceId: string; pinned: boolean }) {
  if (!gatewayAccess.canManageProjectWorkspaces) return
  try {
    await projectWorkspaces.setPinned(payload.workspaceId, payload.pinned)
  } catch (err) {
    pushToast(t('workspaces.updateFailed', { error: errorMessage(err) }), { tone: 'danger' })
  }
}

async function onProjectDeleteHistory(workspaceId: string) {
  if (!gatewayAccess.canManageProjectWorkspaces) return
  try {
    const result = await projectWorkspaces.deleteWorkspaceHistory(workspaceId)
    const leaveDeletedTask = activeTaskWasDeletedWithProjectHistory({
      workspaceId,
      currentSessionKey: currentSessionKey.value,
      sessions: allSessions.value,
      deletedSessionKeys: result.deletedSessionKeys,
    })
    const deleted = new Set(result.deletedSessionKeys)
    removeLocalSessions(deleted)
    sessionTaskAttention.removeMany(deleted)
    appStore.removePendingApprovalsForSessions(deleted)
    dispatchLocalSessionsDeleted(deleted, APP_SESSION_SYNC_SOURCE)
    await loadSessions()
    if (leaveDeletedTask) void openDefaultDraft()
    pushToast(t('workspaces.historyDeleted'), { tone: 'ok' })
  } catch (err) {
    pushToast(t('workspaces.deleteHistoryFailed', { error: errorMessage(err) }), { tone: 'danger' })
  }
}

async function onProjectRemove(workspaceId: string) {
  if (!gatewayAccess.canManageProjectWorkspaces) return
  const project = projectWorkspaces.byId.value.get(workspaceId)
  if (!project) return
  let affectedCronJobs = 0
  try {
    const jobs = await cronScheduler.listJobs()
    affectedCronJobs = (jobs || []).filter(
      job => job.workspaceId === workspaceId,
    ).length
  } catch {
    // The backend still enforces the pause atomically during removal.
  }
  const approved = await confirm({
    title: t('workspaces.removeTitle'),
    body: affectedCronJobs > 0
      ? t('workspaces.removeBodyWithCronJobs', {
          name: project.name,
          count: affectedCronJobs,
        })
      : t('workspaces.removeBody', { name: project.name }),
    primaryLabel: t('workspaces.removeConfirm'),
  })
  if (!approved) return
  try {
    await projectWorkspaces.removeWorkspace(workspaceId)
    if (editingProjectId.value === workspaceId) closeProjectEditor()
    if (
      $route.path === '/chat/new'
      && String($route.query.project || '') === workspaceId
    ) {
      freshTaskDraft.requestFreshTask('main')
      await router.replace({ path: '/chat/new', query: { agent: 'main' } })
    }
  } catch (err) {
    pushToast(t('workspaces.removeFailed', { error: errorMessage(err) }), { tone: 'danger' })
  }
}

// Command palette: ⌘K / Ctrl+K and the rail "Search / Go to…" row both open it.
// Its action commands route back through the existing handlers so behaviour stays
// single-sourced (new chat opens a draft, Settings reuses the footer path).
function openCommandPalette() {
  handleNavClick()
  commandPaletteOpen.value = true
}

function onPaletteNewChat() {
  startNewChatInstant()
}

function onPaletteOpenSettings() {
  openSettings()
}

function onPaletteToggleTheme() {
  // Cycle the appearance mode through the registry-driven order used by
  // Settings (every selectable value theme + system), so the palette
  // never resets a custom theme back to 'light'.
  appStore.cycleTheme()
}

function onPaletteSelectSession(key: string) {
  switchToSession(key, 'command_palette.select_session')
}

async function switchToSession(key: string, source = 'app.switchToSession') {
  if (!key) return
  sessionTaskAttention.markRead(key)
  recordSessionNavigationDiag(source, {
    from: currentSessionKey.value,
    to: key,
  })
  await router.push({ path: '/chat', query: { session: key } })
  if ($route.path === '/chat' && $route.query.session === key) closeSidebarDrawer()
}

let sidebarCopySequence = 0
let sidebarClipboardWrite: Promise<void> = Promise.resolve()
async function onCopySidebarSessionLink(key: string) {
  if (!key) return
  const sequence = ++sidebarCopySequence
  const epoch = gatewayAccess.subscriptionEpoch
  const isCurrent = () => sequence === sidebarCopySequence
    && gatewayAccess.subscriptionEpoch === epoch
  try {
    const link = await currentSessionLink(key, platform)
    if (!isCurrent()) return
    // Clipboard fallbacks can settle after a newer click. Serialize the writes,
    // then recheck ownership so an old fallback cannot overwrite the new link.
    const write = sidebarClipboardWrite.then(async () => {
      if (isCurrent()) await copyTextWithFallback(link)
    })
    sidebarClipboardWrite = write.catch(() => undefined)
    await write
    if (isCurrent()) pushToast(t('chat.copied'), { tone: 'ok' })
  } catch {
    if (isCurrent()) pushToast(t('chat.toast.copyFailed'), { tone: 'danger' })
  }
}

// Only the current rename may update local display state. Later authoritative
// list snapshots can still rename or remove the row normally.
let sessionRenameSequence = 0
const pendingSessionRenames = new Map<string, number>()
async function onRenameSession({ key, title }: { key: string; title: string }) {
  const next = title.trim()
  if (!key || !next) return
  const sequence = ++sessionRenameSequence
  pendingSessionRenames.set(key, sequence)
  renameOverrides.value = { ...renameOverrides.value, [key]: next }
  try {
    await sessionLifecycle.rename({ key, title: next })
    if (pendingSessionRenames.get(key) !== sequence) return
    applyConfirmedTitle(key, next)
    const local = localChatSessions.value[key]
    if (local) localChatSessions.value[key] = { ...local, title: next }
    pushToast('Session renamed', { tone: 'ok' })
  } catch (err: unknown) {
    if (pendingSessionRenames.get(key) !== sequence) return
    console.warn('[App] session rename error:', errorMessage(err))
    pushToast('Failed to rename session', { tone: 'danger' })
  } finally {
    if (pendingSessionRenames.get(key) === sequence) {
      pendingSessionRenames.delete(key)
      const { [key]: _dropped, ...rest } = renameOverrides.value
      renameOverrides.value = rest
      await loadSidebarData()
    }
  }
}

function removeLocalSessions(keys: Set<string>) {
  if (keys.size === 0) return
  let next = localChatSessions.value
  let changed = false
  for (const key of keys) {
    if (!next[key]) continue
    const { [key]: _dropped, ...rest } = next
    next = rest
    changed = true
  }
  if (changed) localChatSessions.value = next
}

function handleLocalSessionsDeleted(event: Event) {
  const detail = localSessionsDeletedDetail(event)
  if (!detail || detail.source === APP_SESSION_SYNC_SOURCE) return
  const deleted = new Set(detail.keys)
  removeLocalSessions(deleted)
  sessionTaskAttention.removeMany(deleted)
  appStore.removePendingApprovalsForSessions(deleted)
  scheduleSessionRefresh()
}

async function deleteSessions(keys: string[]) {
  const uniqueKeys = [...new Set(keys.map(key => key.trim()).filter(Boolean))]
  if (uniqueKeys.length === 0) return null
  try {
    return await sessionLifecycle.remove(uniqueKeys)
  } catch (err: unknown) {
    console.warn('[App] session deletion error:', errorMessage(err))
    return null
  }
}

// Delete sessions, then refresh the list. If the open session was deleted, drop
// into a fresh draft so the view does not linger on a session that no longer exists.
async function onBulkDeleteSessions(keys: string[]) {
  const uniqueKeys = [...new Set(keys.map(key => key.trim()).filter(Boolean))]
  if (uniqueKeys.length === 0) return
  const currentKey = currentSessionKey.value
  const wasCurrentDeleted = !!currentKey && uniqueKeys.includes(currentKey)
  const result = await deleteSessions(uniqueKeys)
  const deleted = new Set(result?.deleted || [])
  if (!result || deleted.size === 0) {
    console.warn('[App] session deletion reported failure:', result?.errors)
    pushToast(t('shared.sidebar.bulkDeleteFailed'), { tone: 'danger' })
    return
  }
  removeLocalSessions(deleted)
  sessionTaskAttention.removeMany(deleted)
  appStore.removePendingApprovalsForSessions(deleted)
  dispatchLocalSessionsDeleted(deleted, APP_SESSION_SYNC_SOURCE)
  const failedCount = Math.max(0, uniqueKeys.length - deleted.size)
  pushToast(t('shared.sidebar.bulkDeleteDone', { count: deleted.size }), { tone: 'ok' })
  if (failedCount > 0 || (result.errors?.length || 0) > 0) {
    console.warn('[App] session deletion partial failure:', result.errors)
    pushToast(t('shared.sidebar.bulkDeletePartial', { count: failedCount || result.errors?.length || 0 }), { tone: 'danger' })
  }
  await loadSessions()
  if (wasCurrentDeleted && deleted.has(currentKey)) {
    void openDefaultDraft()
  }
}

async function onDeleteSession(key: string) {
  if (!key) return
  const wasCurrent = key === currentSessionKey.value
  const result = await deleteSessions([key])
  if (!result?.deleted?.includes(key)) {
    console.warn('[App] session deletion reported failure:', result?.errors)
    pushToast('Failed to delete session', { tone: 'danger' })
    return
  }
  pushToast('Session deleted', { tone: 'ok' })
  const deleted = new Set([key])
  removeLocalSessions(deleted)
  sessionTaskAttention.removeMany(deleted)
  appStore.removePendingApprovalsForSessions(deleted)
  dispatchLocalSessionsDeleted(deleted, APP_SESSION_SYNC_SOURCE)
  await loadSessions()
  if (wasCurrent) {
    void openDefaultDraft()
  }
}

// Topbar approval pill: jump straight to the blocked session's chat so the
// in-thread card can be answered. The live `pendingApprovals` list (kept fresh
// by the push subscription + reconnect seed) is the source of truth — no
// re-fetch — and the oldest pending session is the deterministic target. With
// no routable session, fall back to Chat; the topbar retains the pending count.
function openBlockedApprovalSession() {
  const oldest = appStore.oldestPendingWithSession
  if (oldest?.sessionKey) {
    appStore.requestApprovalFocus(oldest)
    switchToSession(oldest.sessionKey, 'approval.openBlockedSession')
    return
  }
  // No session attached to the pending approval: return to chat.
  router.push('/chat')
}

// Footer settings row. Both platforms mount the same `/settings` overlay now, so
// a single push covers both. bottomRoutes is honored first to keep any future
// bottom-nav destination authoritative, falling back to the shared overlay.
function openSettings() {
  handleNavClick()
  router.push(bottomRoutes.value[0]?.path ?? '/settings')
}

// Topbar connection pill (web): jump straight to the Connection section so the
// gateway link can be inspected or re-pointed.
function openConnectionSettings() {
  router.push('/settings/gateway#connection')
}

// The sidebar entry hands off to the complete Desktop update workflow.
function openDesktopUpdateSettings() {
  handleNavClick()
  router.push('/settings/gateway#updates')
}

function scheduleSessionRefresh() {
  automaticAppRpc.schedule()
}

async function performSidebarLoad(): Promise<SessionListLoadResult> {
  const sessionRead = loadSessions()
  const requests: Promise<unknown>[] = [sessionRead]
  if (
    gatewayAccess.canManageProjectWorkspaces
    && optionalSessionRpcAllowed.value
  ) {
    requests.push(
      projectWorkspaces.loadWorkspaces(optionalSessionReadOptions),
    )
  }
  await Promise.allSettled(requests)
  return sessionRead
}

const automaticAppRpc = createAppAutomaticRpc({
  available: () => gatewayAccess.isAvailable,
  admitted: () => optionalSessionRpcAllowed.value,
  resumeDirectory: () => sessionDirectoryChanges.resume(),
  subscribeCron: subscribeCronEventsWhenAdmitted,
  loadAgents,
  loadSidebar: performSidebarLoad,
  cancelSidebar: cancelPendingRequests,
})

function loadSidebarData(): Promise<void> {
  return automaticAppRpc.load()
}

function handleAppForeground() {
  markCurrentSessionReadIfVisible()
  if (document.visibilityState === 'visible') automaticAppRpc.foreground()
}

const sessionDirectoryChangesSubscription = sessionDirectoryChanges.subscribe(change => {
  if (change.reason === 'deleted') removeLocalSessions(new Set([change.key]))
  scheduleSessionRefresh()
  sessionTaskAttention.handleSessionDirectoryChange(change, {
    currentSessionKey: currentSessionKey.value,
    currentSessionVisible: currentSessionIsVisible(),
  })
})

function subscribeCronEventsWhenAdmitted() {
  if (
    !appAutomaticRpcMounted
    || !optionalSessionRpcAllowed.value
    || !gatewayAccess.isAvailable
    || cronFinishedSubscription
  ) return
  // CronScheduler's Adapter owns its generation-aware remote event lease, but
  // App owns admission so the optional subscribe frame cannot enter the
  // Gateway's serial dispatcher ahead of critical session recovery.
  cronFinishedSubscription = cronScheduler.subscribe(handleCronRunFinished)
}

watch(optionalSessionRpcAllowed, () => {
  void automaticAppRpc.admissionChanged()
}, { flush: 'sync' })

watch(
  () => gatewayAccess.availability,
  () => {
    void automaticAppRpc.availabilityChanged()
  },
  { flush: 'sync' },
)

watch(
  () => gatewayAccess.connectionHealth,
  (health, previous) => {
    if (health === 'healthy' && previous === 'suspect') {
      automaticAppRpc.connectionHealthChanged(health)
    }
  },
  { flush: 'sync' },
)

function handleKeydown(e: KeyboardEvent) {
  // Scoped handlers (chat find, editors and IME input) own consumed keys even
  // when a user has rebound a global action to the same combination.
  if (e.defaultPrevented) return
  // Chord bindings carry the primary modifier as Cmd on Apple platforms and Ctrl
  // elsewhere — and require the other modifier to be absent — so we never match
  // macOS' Ctrl+K (emacs kill-to-end-of-line inside text fields). preventDefault
  // runs BEFORE any early return so the browser never sees the chord: on
  // Chrome/Edge/Firefox (Win/Linux) Ctrl+K focuses the omnibox/search, and in
  // Firefox-mac Cmd+K focuses the search bar. Swallowing it unconditionally also
  // lets the shortcut fire from inside the composer textarea, where the cursor
  // usually sits.
  //
  // Configurable chord shortcuts, consulted from the shortcuts store so the
  // Keyboard settings section is the single source of truth. effectiveBinding
  // returns null for a disabled shortcut, so bindingMatches skips it. New chat
  // is checked first because the palette's no-shift binding would otherwise also
  // match a Shift+K press under a looser guard. preventDefault still runs before
  // the settingsOverlay guard so the browser never sees the chord.
  const paletteBinding = shortcutsStore.effectiveBinding('command-palette')
  const newChatBinding = shortcutsStore.effectiveBinding('new-chat')
  const toggleSidebarBinding = shortcutsStore.effectiveBinding('toggle-sidebar')
  if (bindingMatches(e, toggleSidebarBinding, isMac)) {
    e.preventDefault()
    if (e.repeat || settingsOverlayOpen.value) return
    toggleDock('shortcut')
    return
  }
  if (bindingMatches(e, newChatBinding, isMac)) {
    e.preventDefault()
    if (settingsOverlayOpen.value) return
    startNewChatInstant()
    return
  }
  if (bindingMatches(e, paletteBinding, isMac)) {
    e.preventDefault()
    if (settingsOverlayOpen.value) return
    // Toggle so a second press closes it; the palette owns Escape/focus while open.
    commandPaletteOpen.value = !commandPaletteOpen.value
    return
  }

  // Skip App's fallbacks when a handler that runs BEFORE this one already
  // consumed the key: the composer textarea (@keydown, target phase) and any
  // earlier-registered document listener (e.g. ChatView). Overlays (drawers,
  // modals) attach their document listeners on open — AFTER this one — so they
  // run later and are covered separately by the shared dialog-layer guard.
  if (e.defaultPrevented) return

  // Child topbar controls and routed dialogs install their Escape handlers
  // after App's listener. Let the current dialog-stack owner handle the key
  // instead of pre-emptively collapsing the mobile sidebar beneath it.
  if (e.key === 'Escape' && hasOpenDialogLayer()) return
  // Escape dismisses the sidebar only as the mobile slide-over. On desktop the
  // sidebar is a persistent dock toggled by its own button, so it must never
  // collapse as a side effect of an Escape meant for an overlay opened on top of
  // it. The shared dialog-layer guard above handles that ordering collision;
  // this branch remains mobile-only because desktop uses a persistent dock.
  if (e.key === 'Escape' && effectiveSidebarOpen.value && !settingsOverlayOpen.value && isSidebarDrawer.value) {
    closeSidebarDrawer()
  }
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

// ---------------------------------------------------------------------------
// App-wide approval awareness
//
// A view-local snapshot is not enough; a tool that blocks a background/queued
// turn must surface the badge from any view. The
// gateway pushes `<namespace>.approval.requested|resolved` the moment a run
// blocks or a decision lands, so we keep `pendingApprovals`/`approvalCount`
// live here, seeded once on (re)connect to recover requests that predate the
// socket (e.g. a reload while one is already pending).
// ---------------------------------------------------------------------------

const approvalSubscriptions: ApprovalSubscription[] = []
let approvalSeedGeneration = 0

function approvalItemToPending(item: ApprovalItem): PendingApproval | null {
  const approvalId = item.id.trim()
  if (!approvalId) return null
  return {
    approvalId,
    sessionKey: item.sessionKey,
    tool: item.toolName || 'Unknown tool',
    command: item.command,
  }
}

// Seed the live list from the snapshot so the count is correct after a reload
// while a request is already pending. The
// snapshot is ordered oldest-first, which the deep-link relies on.
async function seedPendingApprovals() {
  if (!appAutomaticRpcMounted || gatewayAccess.availability !== 'available') return
  const generation = ++approvalSeedGeneration
  try {
    const snapshot = await approvalCenter.snapshot()
    if (!appAutomaticRpcMounted || generation !== approvalSeedGeneration
      || gatewayAccess.availability !== 'available') return
    const items = snapshot.pending
      .map(approvalItemToPending)
      .filter((item): item is PendingApproval => item !== null)
    appStore.setPendingApprovals(items)
  } catch (err) {
    console.warn('[App] approvals seed failed:', errorMessage(err))
  }
}

function onApprovalEvent(event: ApprovalEvent) {
  if (event.kind === 'resolved') {
    appStore.removePendingApproval(event.approvalId)
    return
  }
  if (event.approval) {
    const item = approvalItemToPending(event.approval)
    if (item) appStore.upsertPendingApproval(item)
  }
}

// Reconnect re-seeds the list (recovers approvals that arrived while the socket
// was down); the push events keep it live thereafter.
function onApprovalAvailability(state: 'available' | 'recovering' | 'unavailable') {
  if (state !== 'available') {
    approvalSeedGeneration++
    appStore.setPendingApprovals([])
    return
  }
  void seedPendingApprovals()
}

function subscribeApprovals() {
  approvalSubscriptions.push(
    approvalCenter.subscribe(onApprovalEvent),
    approvalCenter.subscribeAvailability(onApprovalAvailability),
  )
}

function unsubscribeApprovals() {
  approvalSeedGeneration++
  approvalSubscriptions.splice(0).forEach(subscription => subscription.close())
  approvalCenter.dispose()
}

// ---------------------------------------------------------------------------
// Tab-title + screen-reader badge for the pending count
// ---------------------------------------------------------------------------

const BASE_TITLE = document.title

const approvalAnnouncement = ref('')

let titleDebounce: ReturnType<typeof setTimeout> | null = null

function applyTitleBadge(count: number) {
  document.title = count > 0 ? `(${count}) ${BASE_TITLE}` : BASE_TITLE
}

// Debounce so a burst of count changes does not thrash the tab title.
watch(() => appStore.approvalCount, count => {
  approvalAnnouncement.value = count > 0 ? `${count} approvals pending` : ''
  if (titleDebounce) clearTimeout(titleDebounce)
  titleDebounce = setTimeout(() => {
    titleDebounce = null
    applyTitleBadge(count)
  }, 500)
})

useDocumentEvent('keydown', handleKeydown)

let desktopDeepLinkUnsubscribe: (() => void) | null = null

onMounted(() => {
  appAutomaticRpcMounted = true
  desktopDeepLinkUnsubscribe = bindDesktopSessionDeepLinks({
    window: platform.window,
    directory: sessionDirectory,
    gatewayContext: () => ({
      endpoint: gatewayAccess.isAvailable ? gatewayAccess.loadConnectionEndpoint() : '',
      epoch: gatewayAccess.isAvailable ? gatewayAccess.subscriptionEpoch : null,
      authenticated: gatewayAccess.isAuthenticated,
    }),
    onGatewayContextChange: callback => watch(() => [
      gatewayAccess.isAvailable,
      gatewayAccess.subscriptionEpoch,
      gatewayAccess.isAuthenticated,
      gatewayAccess.loadConnectionEndpoint(),
    ], callback, { flush: 'sync' }),
    openSession: key => switchToSession(key, 'desktop.deep_link'),
    unavailable: () => pushToast(t('chat.sessionReference.unavailable'), { tone: 'danger' }),
  })
  window.visualViewport?.addEventListener('resize', syncMobileKeyboard)
  window.addEventListener(LOCAL_SESSIONS_DELETED_EVENT, handleLocalSessionsDeleted)
  window.addEventListener('focus', handleAppForeground)
  document.addEventListener('visibilitychange', handleAppForeground)
  void automaticAppRpc.mount()
  // Keep the approval badge/count live app-wide, not just on the Approvals page.
  subscribeApprovals()
  // Seed now in case an approval was pending before mount. Availability events
  // re-seed after reconnects and clear stale data while transport recovers.
  void seedPendingApprovals()
})

onUnmounted(() => {
  sidebarCopySequence += 1
  appAutomaticRpcMounted = false
  automaticAppRpc.dispose()
  window.removeEventListener(LOCAL_SESSIONS_DELETED_EVENT, handleLocalSessionsDeleted)
  window.removeEventListener('focus', handleAppForeground)
  document.removeEventListener('visibilitychange', handleAppForeground)
  sessionDirectoryChangesSubscription.close()
  sessionDirectoryChanges.dispose()
  unsubscribeApprovals()
  desktopDeepLinkUnsubscribe?.()
  desktopDeepLinkUnsubscribe = null
  cronFinishedSubscription?.close()
  cronFinishedSubscription = null
  if (titleDebounce) {
    clearTimeout(titleDebounce)
    titleDebounce = null
  }
  document.title = BASE_TITLE
  window.visualViewport?.removeEventListener('resize', syncMobileKeyboard)
})

</script>

<style scoped>
.app-workspace {
  --workspace-header-height: 49px;
  position: relative;
  display: flex;
  min-width: 0;
  min-height: 0;
  flex: 1;
}

.app-chat-pane--layout-motion { overflow: hidden; }

.app-chat-pane {
  position: relative;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  min-height: 0;
}

.app-chat-pane > .content {
  min-height: 0;
}

.topbar--chat {
  border-bottom: 1px solid var(--border-subtle, var(--border));
}

.sidebar-core {
  border-bottom: 1px solid var(--border-subtle, var(--border));
  margin-bottom: var(--sp-2);
}

.sidebar-foot {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--sp-1);
}

.sidebar-foot .sidebar-fn-item {
  flex: 1 1 auto;
  min-width: 0;
}

.sidebar-update-dot {
  position: absolute;
  top: 5px;
  inset-inline-end: 5px;
  width: 6px;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--accent);
  border: 1px solid var(--bg-surface);
}

.sidebar-update-dot[data-state='danger'] {
  background: var(--danger);
}

/* Off-screen but screen-reader-reachable announcer for the approval count. */
.app-approval-live {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
</style>
