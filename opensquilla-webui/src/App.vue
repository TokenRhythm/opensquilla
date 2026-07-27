<template>
  <!-- Sidebar -->
  <nav
    ref="sidebarRef"
    class="sidebar"
    :class="{
      docked: appStore.sidebarOpen,
      'sidebar--drawer': isSidebarDrawer,
    }"
    :inert="!appStore.sidebarOpen"
    :aria-hidden="appStore.sidebarOpen ? undefined : 'true'"
    :aria-label="t('chrome.primaryNav')"
    id="sidebar-nav"
  >
    <!-- Brand -->
    <div class="sidebar-brand">
      <router-link
        to="/overview"
        class="sidebar-brand-link"
        :aria-label="t('chrome.brandHome')"
        @click="handleNavClick"
      >
        <img class="sidebar-brand-mark" :src="brandMarkUrl" alt="" aria-hidden="true" />
        <span class="sidebar-brand-text">OpenSquilla</span>
      </router-link>
      <button
        ref="sidebarDockToggleRef"
        class="sidebar-dock-toggle"
        :aria-label="t('chrome.collapseSidebar')"
        aria-controls="sidebar-nav"
        :aria-expanded="appStore.sidebarOpen"
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

    <!-- New chat opens a draft instantly against the default agent; the
         landing intentionally does not interrupt the flow with a picker. -->
    <div class="sidebar-actions">
      <button
        class="sidebar-new-session"
        :title="newChatHint ? `Start a new chat (${newChatHint})` : 'Start a new chat'"
        @click="startNewChatInstant"
      >
        <Icon name="plus" :size="16" />
        <span class="sidebar-new-session__label">{{ t('chrome.newChat') }}</span>
        <!-- Badge tracks the configured binding and hides when the shortcut is
             disabled (Settings → Keyboard), so it never advertises a dead key. -->
        <kbd v-if="newChatHint" class="sidebar-kbd" aria-hidden="true">{{ newChatHint }}</kbd>
      </button>
      <!-- Canonical search / go-to. Replaces the rail Search row that truncated;
           the visible chord keeps the shortcut discoverable for mouse users. -->
      <button
        type="button"
        class="sidebar-cmd-btn"
        :title="`Search / Go to… (${commandPaletteHint})`"
        :aria-label="`Search and go to (press ${commandPaletteHint})`"
        aria-haspopup="dialog"
        :aria-expanded="commandPaletteOpen"
        @click="openCommandPalette"
      >
        <Icon name="search" :size="16" />
      </button>
    </div>

    <!-- Always-visible flat nav index. Bounded and self-scrolling under a
         short viewport so it never squeezes Recents, which owns the elastic
         space below; every destination stays a labelled text row. -->
    <div class="sidebar-section sidebar-core" role="navigation" :aria-label="t('chrome.controlNav')">
      <!-- Sessions / Overview / Skills & Channels / Cron, single-sourced from route
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
        <Icon :name="item.icon" :size="16" />
        <span class="sidebar-fn-label">{{ item.title }}</span>
        <!-- Live pending-approvals count on the Sessions row: approvals resolve
             inline in chat; the Sessions attention strip is the queue's home
             and the topbar pill remains the interrupt affordance. -->
        <span
          v-if="item.path === '/sessions' && appStore.approvalCount > 0"
          class="sidebar-count-badge"
        >{{ appStore.approvalCount }}</span>
      </router-link>
    </div>

    <SidebarSetupBanner />

    <!-- Recent conversations -->
    <SidebarConversations
      :sections="sidebarSections"
      :error="sessionListError"
      :loading="isLoading"
      :current-key="currentSessionKey"
      :contract-debug-enabled="contractDebugEnabled"
      @select="switchToSession"
      @refresh="loadSessions"
      @rename="onRenameSession"
      @delete="onDeleteSession"
      @bulk-delete="onBulkDeleteSessions"
      @new-chat="startNewChatInstant"
    />

    <!-- Fixed footer: settings + connection state -->
    <div class="sidebar-foot">
      <button
        type="button"
        class="sidebar-fn-item"
        data-icon="settings"
        @click="openSettings"
      >
        <Icon name="settings" :size="16" />
        <span class="sidebar-fn-label">{{ t('chrome.settings') }}</span>
      </button>
    </div>
  </nav>

  <SidebarResizer
    v-if="appStore.sidebarOpen && isSidebarResizable"
    ref="sidebarResizerRef"
    :enabled="appStore.sidebarOpen && isSidebarResizable"
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
    v-if="appStore.sidebarOpen && isSidebarDrawer"
    class="sidebar-scrim"
    role="presentation"
    aria-hidden="true"
    @click="closeSidebarDrawer"
  />

  <CommandPalette
    v-model:open="commandPaletteOpen"
    :hint="commandPaletteHint"
    @new-chat="onPaletteNewChat"
    @open-settings="onPaletteOpenSettings"
    @toggle-theme="onPaletteToggleTheme"
    @select-session="onPaletteSelectSession"
  />

  <!-- Main content -->
  <div
    id="app-main"
    class="main"
    :inert="appStore.sidebarOpen && isSidebarDrawer"
    :class="{
      docked: appStore.sidebarOpen,
      'main--sidebar-drawer': isSidebarDrawer,
      'main--sidebar-compact': sidebarLayoutMode === 'compact',
      'main--chat': isChatRoute,
      'main--chat-sidebar-collapsed': isChatRoute && !appStore.sidebarOpen,
      'main--tabbar-hidden': mobileKeyboardOpen,
    }"
  >
    <header class="topbar" :class="{ 'topbar--chat': isChatRoute }">
      <div class="topbar-left">
        <!-- Sidebar toggle — visible when sidebar is collapsed -->
        <button
          v-show="!appStore.sidebarOpen"
          ref="topbarSidebarToggleRef"
          class="sidebar-dock-toggle topbar-toggle"
          :aria-label="t('chrome.expandSidebar')"
          aria-controls="sidebar-nav"
          :aria-expanded="appStore.sidebarOpen"
          :aria-keyshortcuts="sidebarToggleAriaShortcut"
          aria-describedby="sidebar-toggle-tip-collapsed"
          data-testid="sidebar-toggle-collapsed"
          @click="toggleDock('topbar-button')"
        >
          <Icon name="sidebar-hidden" :size="18" />
          <span id="sidebar-toggle-tip-collapsed" class="sidebar-toggle-tip" role="tooltip">
            <span>{{ t('chrome.toggleSidebar') }}</span>
            <kbd v-if="sidebarToggleHint">{{ sidebarToggleHint }}</kbd>
          </span>
        </button>
      </div>
      <!-- Permanent target: Chat teleports its route-owned actions into the
           same in-flow header as the app controls without duplicating state. -->
      <div
        id="app-route-header"
        class="topbar-route-header"
        data-testid="route-header-host"
      ></div>
      <div
        class="topbar-right"
        :class="{ 'topbar-right--attention': appStore.approvalCount > 0 }"
      >
        <button
          v-if="appStore.approvalCount > 0"
          class="approval-inline"
          @click="openBlockedApprovalSession"
          :title="t('chrome.openBlockedSession')"
        >
          {{ t('chrome.approvalRequired') }}
        </button>
        <button
          v-if="webConfigEnabled"
          type="button"
          class="conn-pill conn-pill--link"
          :class="rpcStore.state"
          :title="t('chrome.connectionTitle', { state: connectionStateLabel })"
          :aria-label="t('chrome.manageConnection')"
          @click="openConnectionSettings"
        >{{ connectionStateLabel }}</button>
        <span v-else class="conn-pill" :class="rpcStore.state">{{ connectionStateLabel }}</span>
        <DesktopUpdateIndicator />
        <!-- Opt-in (Settings → Appearance or the command palette); off by
             default so the topbar stays music-free until asked for. -->
        <BgmControl v-if="bgmEnabled" />
        <LanguageSwitcher />
        <div class="theme-menu-wrap">
          <button
            ref="themeButtonRef"
            class="btn btn--icon btn--ghost"
            :title="t('chrome.theme')"
            :aria-label="t('chrome.theme')"
            aria-haspopup="menu"
            :aria-expanded="themeMenuOpen"
            @click.stop="themeMenuOpen = !themeMenuOpen"
          >
            <Icon :name="themeIconName" :size="16" />
          </button>
          <div v-if="themeMenuOpen" class="theme-menu" role="menu" :aria-label="t('chrome.theme')">
            <button
              v-for="opt in themeOptions"
              :key="opt.mode"
              type="button"
              class="theme-menu__item"
              role="menuitemradio"
              :aria-checked="appStore.theme === opt.mode"
              @click="pickTheme(opt.mode)"
            >
              <Icon :name="opt.icon" :size="15" />
              <span>{{ opt.labelKey ? t(opt.labelKey) : opt.label }}</span>
              <Icon v-if="appStore.theme === opt.mode" class="theme-menu__check" name="check" :size="14" />
            </button>
            <button
              type="button"
              class="theme-menu__item theme-menu__item--more"
              role="menuitem"
              :title="t('chrome.moreThemesHint')"
              @click="openMoreThemes"
            >
              <Icon name="chevronRight" :size="15" />
              <span>{{ t('chrome.moreThemes') }}</span>
              <Icon v-if="isCustomThemeActive" class="theme-menu__check" name="check" :size="14" />
            </button>
          </div>
        </div>
      </div>
    </header>
    <main
      class="content"
      :class="{ 'content--chat': isChatRoute }"
      :data-skin="skinId || undefined"
      :data-skin-variant="variants || undefined"
      id="content"
    >
      <ErrorBoundary>
        <router-view v-slot="{ Component, route }">
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
      </ErrorBoundary>
    </main>
  </div>

  <!-- Mobile bottom tab bar (<=768px only; hides while the keyboard is up):
       Chat, Sessions, Overview, then More for the flat drawer containing
       Sessions / Overview / Skills & Channels / Cron and Settings. -->
  <nav
    class="mobile-tabbar"
    :class="{ 'is-keyboard-open': mobileKeyboardOpen }"
    :inert="appStore.sidebarOpen && isSidebarDrawer"
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
      to="/sessions"
      class="mobile-tab"
      :class="{ 'is-active': isNavActive('/sessions') }"
      @click="handleNavClick"
    >
      <Icon name="sessions" :size="20" />
      <span class="mobile-tab__label">{{ t('nav.sessions') }}</span>
      <span v-if="appStore.approvalCount > 0" class="mobile-tab__badge">{{ appStore.approvalCount }}</span>
    </router-link>
    <router-link
      to="/overview"
      class="mobile-tab"
      :class="{ 'is-active': isOverviewNavActive }"
      @click="handleNavClick"
    >
      <Icon name="home" :size="20" />
      <span class="mobile-tab__label">{{ t('nav.overview') }}</span>
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

  <ToastHost />

  <ConfirmModal />

  <UpdateBanner />

  <!-- Single app-wide announcer for the pending-approval count. The nav badge
       and topbar pill stay silent (no double-announce); this region carries the
       only spoken update when the count changes. -->
  <p class="app-approval-live" aria-live="polite" role="status">{{ approvalAnnouncement }}</p>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { routeTitle } from './router'
import { getPlatform } from '@/platform'
import { useAppStore, type ThemeMode, type PendingApproval } from './stores/app'
import { useRpcStore } from './stores/rpc'
import {
  arrangeSidebarSections,
  useSessions,
  type SessionItem,
  type SidebarSection,
  type SidebarSectionRow,
} from './composables/useSessions'
import Icon from './components/Icon.vue'
import ErrorBoundary from './components/ErrorBoundary.vue'
import ToastHost from './components/ToastHost.vue'
import ConfirmModal from './components/ConfirmModal.vue'
import UpdateBanner from './components/UpdateBanner.vue'
import DesktopUpdateIndicator from './components/DesktopUpdateIndicator.vue'
import SidebarConversations from './components/SidebarConversations.vue'
import SidebarSetupBanner from './components/SidebarSetupBanner.vue'
import SidebarResizer from './components/SidebarResizer.vue'
import CommandPalette from './components/CommandPalette.vue'
import LanguageSwitcher from './components/LanguageSwitcher.vue'
import BgmControl from './components/BgmControl.vue'
import { useBgm } from './composables/useBgm'
import { useSidebarLayout } from './composables/useSidebarLayout'
import { useDocumentEvent } from './composables/useDocumentEvent'
import { useAgentOptions } from './composables/useAgentOptions'
import { useSessionListSubscription } from './composables/useSessionListSubscription'
import { useToasts } from './composables/useToasts'
import { useNavigation } from './app/useNavigation'
import { useSurfaceSkin } from './themes/useSurfaceSkin'
import { themePickerOptions, getManifest } from './themes/registry'
import { normalizeAgentId } from './utils/chat/sessionKeys'
import { installSessionNavigationDiagConsole, recordSessionNavigationDiag } from './utils/chat/sessionNavigationDiag'
import type { RpcEventHandler } from '@/lib/rpc'
import { isMacPlatform } from './utils/browser'
import { useShortcutsStore } from './stores/shortcuts'
import { bindingMatches, formatBinding } from './utils/keychord'
import { SIDEBAR_MIN_WIDTH, type SidebarWidthPreference } from './utils/sidebarLayout'
import {
  dispatchLocalSessionsDeleted,
  localSessionsDeletedDetail,
  LOCAL_SESSIONS_DELETED_EVENT,
} from './utils/sessionSync'

const appStore = useAppStore()
const rpcStore = useRpcStore()
const shortcutsStore = useShortcutsStore()
const { t } = useI18n()
const $route = useRoute()
const sidebarRef = ref<HTMLElement | null>(null)
const sidebarDockToggleRef = ref<HTMLButtonElement | null>(null)
const topbarSidebarToggleRef = ref<HTMLButtonElement | null>(null)
type SidebarResizerHandle = { cancel: () => boolean }
const sidebarResizerRef = ref<SidebarResizerHandle | null>(null)

const {
  mode: sidebarLayoutMode,
  dynamicMax: sidebarDynamicMaximum,
  effectiveWidth: sidebarEffectiveWidth,
} = useSidebarLayout()
const isSidebarDrawer = computed(() => sidebarLayoutMode.value === 'drawer')
const isSidebarResizable = computed(() => sidebarLayoutMode.value === 'resizable')
const sidebarResizeActive = ref(false)

function setSidebarCssWidth(width: number) {
  if (!Number.isFinite(width)) return
  document.getElementById('app')?.style.setProperty('--sidebar-width', `${Math.round(width)}px`)
}

// Persisted/pre-set changes are infrequent. Pointer previews bypass App's
// reactive tree and write the same root custom property directly once per rAF.
watch(sidebarEffectiveWidth, width => {
  if (!sidebarResizeActive.value) setSidebarCssWidth(width)
}, { immediate: true })

interface DeleteSessionsResponse {
  deleted?: string[]
  errors?: string[]
}

const APP_SESSION_SYNC_SOURCE = 'app-sidebar'

// Localized connection-state label for the topbar pill and its tooltip. The
// store state ('connected' | 'connecting' | 'disconnected') is a stable key, not
// display text; CSS uppercases the result (a no-op for CJK scripts).
const connectionStateLabel = computed(() => t(`chrome.connectionState.${rpcStore.state}`))
const router = useRouter()

// afterEach only fires on navigation, so a same-route language switch needs an
// explicit re-localize of the tab title.
watch(() => appStore.locale, () => {
  document.title = `${routeTitle($route)} — OpenSquilla`
})
const { allSessions, sessionListError, isLoading, loadSessions } = useSessions()
const { bottomRoutes, workNav } = useNavigation()
// Axis-B: the active expressive skin for the routed content area (meta.skin).
const { skinId, variants } = useSurfaceSkin()
const { pushToast } = useToasts()
// Feature-gated topbar music control; the singleton `enabled` ref is written by
// Settings → Appearance and the command palette.
const { enabled: bgmEnabled } = useBgm()
const webConfigEnabled = getPlatform().capabilities.hasWebConfig

installSessionNavigationDiagConsole()

// Shared agents.list state + fetch (singleton) for sidebar session metadata.
const { agents, loadAgents } = useAgentOptions()
const mobileKeyboardOpen = ref(false)
const commandPaletteOpen = ref(false)
const localChatSessions = ref<Record<string, { effectiveAgentId: string; title: string; updatedAt: number }>>({})
// Pending optimistic renames, keyed by session key; cleared after the next list
// reload returns the backend's canonical title.
const renameOverrides = ref<Record<string, string>>({})

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

const themeIconName = computed(() => {
  if (appStore.theme === 'system') return 'monitor'
  const active = getManifest(appStore.resolvedTheme)
  return active?.icon ?? (appStore.resolvedTheme === 'dark' ? 'moon' : 'sun')
})

const themeMenuOpen = ref(false)
const themeButtonRef = ref<HTMLButtonElement | null>(null)

// The compact topbar menu deliberately lists only the basic modes (Light / Dark
// / System). Custom value themes live in Settings → Appearance, reached via the
// "More themes…" action below — see themePickerOptions({ scope }) in registry.ts.
const themeOptions = themePickerOptions({ scope: 'basic' })

// A custom value theme (chosen in Settings) is active but not shown in the basic
// topbar menu; mark "More themes…" instead of leaving no selection indicator.
const isCustomThemeActive = computed(
  () => !themeOptions.some((o) => o.mode === appStore.theme),
)

function pickTheme(mode: ThemeMode) {
  appStore.setTheme(mode)
  themeMenuOpen.value = false
  themeButtonRef.value?.focus()
}

// "More themes…": the full theme list lives in Settings → Appearance. Close the
// menu and deep-link straight to that section.
function openMoreThemes() {
  themeMenuOpen.value = false
  handleNavClick()
  router.push('/settings/appearance')
}

useDocumentEvent('click', (e) => {
  if (!themeMenuOpen.value) return
  const wrap = themeButtonRef.value?.closest('.theme-menu-wrap')
  if (wrap && e.target instanceof Node && !wrap.contains(e.target)) {
    themeMenuOpen.value = false
  }
})

// Current session key from ChatView via URL
const currentSessionKey = computed(() => {
  return ($route.query.session as string) || ''
})

// Chat layout applies to both the session view and the draft route.
const isChatRoute = computed(() => $route.path === '/chat' || $route.path === '/chat/new')

// The Settings overlay (route-mounted dialog) is open on these routes. It owns
// its own Escape/focus, so App-level keyboard shortcuts defer to it. Both web
// and desktop mount the same overlay now (webConfigEnabled is true on both).
const settingsOverlayOpen = computed(() =>
  webConfigEnabled && ($route.name === 'settings' || $route.name === 'settings-section'))

const contractDebugEnabled = computed(() => appStore.features.contractDebug === true)

function isNavActive(path: string): boolean {
  if (path === '/chat') return isChatRoute.value
  return $route.path === path
}

// Overview owns the Status/Usage hub plus its diagnostic Logs route, while
// Skills fronts the Skills/Channels hub. Keep those active families disjoint so
// diagnostic routes never light an unrelated primary destination.
const OVERVIEW_NAV_PATHS = new Set(['/overview', '/usage', '/logs'])
const SKILLS_CHANNELS_HUB_PATHS = new Set(['/skills', '/channels'])
const MOBILE_MORE_PATHS = new Set(['/skills', '/channels', '/cron'])
const isOverviewNavActive = computed(() => OVERVIEW_NAV_PATHS.has($route.path))
const isSkillsChannelsHubActive = computed(() => SKILLS_CHANNELS_HUB_PATHS.has($route.path))
const isMobileMoreActive = computed(() =>
  appStore.sidebarOpen || MOBILE_MORE_PATHS.has($route.path))

function isPrimaryNavActive(path: string): boolean {
  if (path === '/overview') return isOverviewNavActive.value
  if (path === '/skills') return isSkillsChannelsHubActive.value
  return isNavActive(path)
}

function agentDisplayName(agentId: string): string {
  const agent = agents.value.find(a => a.id === agentId)
  return agent?.name || (agentId === 'main' ? 'Main Agent' : agentId)
}

// Raw session keys (agent:…:…) and bare UUIDs must never render in the sidebar.
const RAW_SESSION_KEY_PATTERN = /\bagent:[a-z0-9_-]+:[a-z0-9_-]+:/i
const UUID_PATTERN = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

function looksLikeRawSessionId(value: string): boolean {
  return RAW_SESSION_KEY_PATTERN.test(value) || UUID_PATTERN.test(value) || /^(agent|cron):/i.test(value)
}

function sidebarConversationTitle(item: SessionItem): string {
  for (const candidate of [item.title, item.subtitle, item.groupLabel]) {
    const text = String(candidate || '').trim()
    if (text && !looksLikeRawSessionId(text)) return text
  }
  return 'Untitled session'
}

// A draft / current-session row the backend list does not yet carry. The
// sidebar arranger reads only a handful of fields off the SessionItem, so a
// synthetic chat row carries just those plus a stub `raw` (no parent → root).
function syntheticChatSession(
  key: string,
  effectiveAgentId: string,
  title: string,
  updatedAt: number,
): SessionItem {
  return {
    key,
    title,
    subtitle: '',
    groupLabel: normalizeAgentId(effectiveAgentId),
    effectiveAgentId,
    sessionKind: 'chat',
    surface: 'webchat',
    conversationKind: 'direct',
    threadLabel: '',
    channelContext: null,
    status: 'idle',
    visualStatus: 'idle',
    runStatus: 'idle',
    runLabel: 'Idle',
    messageCount: null,
    updatedAt,
    interactive: true,
    forkedFromParent: false,
    contractGaps: [],
    raw: { key },
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
    items.push(item)
  }
  for (const [key, local] of Object.entries(localChatSessions.value)) {
    if (seen.has(key)) continue
    seen.add(key)
    items.push(syntheticChatSession(key, local.effectiveAgentId, local.title || 'New chat', local.updatedAt))
  }
  const current = currentSessionKey.value
  if (current && !seen.has(current)) {
    const currentAgentId = normalizeAgentId(current.split(':')[1] || 'main')
    items.push(syntheticChatSession(current, currentAgentId, 'Current session', Date.now()))
  }
  return items
})

// Collapsible family sections (Chats / Channels / Automations). Row titles and
// agent names are resolved here so the raw-session-id scrub and the display-name
// lookup stay in App.vue; subagents indent under their parent via the helper.
const sidebarSections = computed((): SidebarSection[] => {
  const byKey = new Map(sidebarSessionItems.value.map(item => [item.key, item]))
  return arrangeSidebarSections(sidebarSessionItems.value).map(section => ({
    ...section,
    rows: section.rows.map((row): SidebarSectionRow => {
      if (row.rowKind === 'workspace') return { ...row, agentName: '' }
      const source = byKey.get(row.key)
      const title = renameOverrides.value[row.key]
        || (source ? sidebarConversationTitle(source) : row.title)
      return {
        ...row,
        title,
        agentName: agentDisplayName(normalizeAgentId(row.effectiveAgentId)),
      }
    }),
  }))
})

let sessionRefreshTimer: ReturnType<typeof setTimeout> | null = null

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
  const wasOpen = appStore.sidebarOpen
  const focusWasInsideSidebar = Boolean(
    sidebarRef.value && document.activeElement && sidebarRef.value.contains(document.activeElement),
  )
  const focusWasOnResizer = document.activeElement instanceof HTMLElement
    && document.activeElement.matches('.sidebar-resizer')
  appStore.toggleSidebar()
  if (!wasOpen && (source === 'topbar-button' || isSidebarDrawer.value)) {
    void nextTick(() => sidebarDockToggleRef.value?.focus())
  } else if (wasOpen && (source === 'sidebar-button' || focusWasInsideSidebar || focusWasOnResizer)) {
    void nextTick(() => topbarSidebarToggleRef.value?.focus())
  }
}

function handleNavClick() {
  if (isSidebarDrawer.value && appStore.sidebarOpen) {
    closeSidebarDrawer()
  }
}

function openSidebarDrawer() {
  if (appStore.sidebarOpen) return
  toggleDock('topbar-button')
}

function closeSidebarDrawer() {
  if (!appStore.sidebarOpen || !isSidebarDrawer.value) return
  sidebarResizerRef.value?.cancel()
  appStore.setSidebarOpen(false)
  void nextTick(() => topbarSidebarToggleRef.value?.focus())
}

function handleSidebarResizeStart() {
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

// Layout mode is a single state machine shared with Settings. Entering a drawer
// force-closes the persistent dock; returning to desktop intentionally leaves it
// closed until the user reopens it. Compact mode keeps the current open state.
watch(sidebarLayoutMode, (nextMode, previousMode) => {
  const focusWasOnResizer = document.activeElement instanceof HTMLElement
    && document.activeElement.matches('.sidebar-resizer')
  sidebarResizerRef.value?.cancel()
  sidebarResizeActive.value = false
  setSidebarCssWidth(sidebarEffectiveWidth.value)

  if (nextMode === 'drawer' && appStore.sidebarOpen) {
    const focusWasInsideSidebar = Boolean(
      sidebarRef.value && document.activeElement && sidebarRef.value.contains(document.activeElement),
    )
    appStore.setSidebarOpen(false)
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
// Explicit custom-Agent launches still receive their Agent-scoped session key
// from advanced Agent administration.
function openDefaultDraft() {
  return router.push({ path: '/chat/new', query: { agent: 'main' } })
}

function startNewChatInstant() {
  handleNavClick()
  void openDefaultDraft()
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
  // Cycle the appearance mode through the same registry-driven order as the
  // topbar picker (every selectable value theme + system), so the palette
  // never resets a custom theme back to 'light'.
  appStore.cycleTheme()
}

function onPaletteSelectSession(key: string) {
  switchToSession(key, 'command_palette.select_session')
}

function switchToSession(key: string, source = 'app.switchToSession') {
  if (!key) return
  recordSessionNavigationDiag(source, {
    from: currentSessionKey.value,
    to: key,
  })
  router.push({ path: '/chat', query: { session: key } })
}

// Optimistic rename: show the new title immediately, then persist via
// sessions.patch (display_name is the top-precedence title) and reload so the
// backend's canonical title wins. The override clears once the reload lands.
async function onRenameSession({ key, title }: { key: string; title: string }) {
  const next = title.trim()
  if (!key || !next) return
  renameOverrides.value = { ...renameOverrides.value, [key]: next }
  const local = localChatSessions.value[key]
  if (local) localChatSessions.value[key] = { ...local, title: next }
  try {
    await rpcStore.call('sessions.patch', { key, displayName: next })
    pushToast('Session renamed', { tone: 'ok' })
  } catch (err: unknown) {
    console.warn('[App] sessions.patch error:', errorMessage(err))
    pushToast('Failed to rename session', { tone: 'danger' })
  } finally {
    await loadSessions()
    const { [key]: _dropped, ...rest } = renameOverrides.value
    renameOverrides.value = rest
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
  removeLocalSessions(new Set(detail.keys))
  void loadSessions()
}

async function deleteSessions(keys: string[]): Promise<DeleteSessionsResponse | null> {
  const uniqueKeys = [...new Set(keys.map(key => key.trim()).filter(Boolean))]
  if (uniqueKeys.length === 0) return null
  try {
    return await rpcStore.call<DeleteSessionsResponse>('sessions.delete', { keys: uniqueKeys })
  } catch (err: unknown) {
    console.warn('[App] sessions.delete error:', errorMessage(err))
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
    console.warn('[App] sessions.delete reported failure:', result?.errors)
    pushToast(t('shared.sidebar.bulkDeleteFailed'), { tone: 'danger' })
    return
  }
  removeLocalSessions(deleted)
  dispatchLocalSessionsDeleted(deleted, APP_SESSION_SYNC_SOURCE)
  const failedCount = Math.max(0, uniqueKeys.length - deleted.size)
  pushToast(t('shared.sidebar.bulkDeleteDone', { count: deleted.size }), { tone: 'ok' })
  if (failedCount > 0 || (result.errors?.length || 0) > 0) {
    console.warn('[App] sessions.delete partial failure:', result.errors)
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
    console.warn('[App] sessions.delete reported failure:', result?.errors)
    pushToast('Failed to delete session', { tone: 'danger' })
    return
  }
  pushToast('Session deleted', { tone: 'ok' })
  const deleted = new Set([key])
  removeLocalSessions(deleted)
  dispatchLocalSessionsDeleted(deleted, APP_SESSION_SYNC_SOURCE)
  await loadSessions()
  if (wasCurrent) {
    void openDefaultDraft()
  }
}

// Topbar approval pill: jump straight to the blocked session's chat so the
// in-thread card can be answered. The live `pendingApprovals` list (kept fresh
// by the push subscription + reconnect seed) is the source of truth — no
// re-fetch — and the oldest pending session (closest to timeout) is the
// deterministic target. With no routable session, fall back to the Approvals
// page.
function openBlockedApprovalSession() {
  const oldest = appStore.oldestPendingWithSession
  if (oldest?.sessionKey) {
    switchToSession(oldest.sessionKey, 'approval.openBlockedSession')
    return
  }
  // No session attached to the pending approval: land on Sessions, whose
  // attention strip shows the pending count (the /approvals page is retired).
  router.push('/sessions')
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
  router.push('/settings/connection')
}

function scheduleSessionRefresh() {
  if (sessionRefreshTimer) clearTimeout(sessionRefreshTimer)
  sessionRefreshTimer = setTimeout(() => {
    sessionRefreshTimer = null
    loadSessions()
  }, 150)
}

const sessionListSubscription = useSessionListSubscription({
  rpc: rpcStore,
  isConnected: () => rpcStore.isConnected,
  refresh: loadSessions,
  scheduleRefresh: scheduleSessionRefresh,
  warn: (message, error) => console.warn(`[App] ${message}:`, errorMessage(error)),
})

function handleKeydown(e: KeyboardEvent) {
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
  // run later and are NOT covered by this guard; their collision with the
  // sidebar-Escape branch is ruled out by the mobile-only gate below instead.
  if (e.defaultPrevented) return

  if (e.key === 'Escape' && themeMenuOpen.value) {
    themeMenuOpen.value = false
    themeButtonRef.value?.focus()
    return
  }
  // Escape dismisses the sidebar only as the mobile slide-over. On desktop the
  // sidebar is a persistent dock toggled by its own button, so it must never
  // collapse as a side effect of an Escape meant for an overlay opened on top of
  // it. Because those overlays run after this handler (see above), this
  // mobile-only gate — not the defaultPrevented check — is what prevents that
  // collision; keep it. The settings overlay owns Escape while open and is excluded.
  if (e.key === 'Escape' && appStore.sidebarOpen && !settingsOverlayOpen.value && isSidebarDrawer.value) {
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

interface ApprovalPushPayload {
  approval_id?: string
  approvalId?: string
  session_key?: string
  sessionKey?: string
  tool_name?: string
  toolName?: string
  command?: string
}

interface ApprovalSnapshotItem {
  id?: string
  sessionKey?: string
  toolName?: string
  pluginId?: string
  actionKind?: string
  command?: string
  argv?: string[]
}

const rpcApprovalUnsubs: Array<() => void> = []

function approvalAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  try {
    const token = sessionStorage.getItem('opensquilla.wsToken') || ''
    if (token) headers['Authorization'] = `Bearer ${token}`
  } catch { /* ignore */ }
  return headers
}

function snapshotItemToPending(item: ApprovalSnapshotItem): PendingApproval | null {
  const approvalId = String(item.id || '').trim()
  if (!approvalId) return null
  let command = String(item.command || '')
  if (!command && Array.isArray(item.argv) && item.argv.length > 0) {
    command = item.argv.map(String).join(' ')
  }
  return {
    approvalId,
    sessionKey: String(item.sessionKey || ''),
    tool: String(item.toolName || item.pluginId || item.actionKind || 'Unknown tool'),
    command,
  }
}

// Seed the live list from the snapshot so the count is correct after a reload
// while a request is already pending. The
// snapshot is ordered oldest-first, which the deep-link relies on.
async function seedPendingApprovals() {
  try {
    const res = await fetch('/api/approvals', { headers: approvalAuthHeaders() })
    if (!res.ok) throw new Error('HTTP ' + res.status)
    const data = await res.json() as { pending?: ApprovalSnapshotItem[] }
    const items = (data.pending || [])
      .map(snapshotItemToPending)
      .filter((item): item is PendingApproval => item !== null)
    appStore.setPendingApprovals(items)
  } catch (err) {
    console.warn('[App] approvals seed failed:', errorMessage(err))
  }
}

function onApprovalRequested(payload: ApprovalPushPayload) {
  const approvalId = String(payload.approval_id || payload.approvalId || '').trim()
  if (!approvalId) return
  appStore.upsertPendingApproval({
    approvalId,
    sessionKey: String(payload.session_key || payload.sessionKey || ''),
    tool: String(payload.tool_name || payload.toolName || 'Unknown tool'),
    command: String(payload.command || ''),
  })
}

function onApprovalResolved(payload: ApprovalPushPayload) {
  const approvalId = String(payload.approval_id || payload.approvalId || '').trim()
  if (approvalId) appStore.removePendingApproval(approvalId)
}

// Reconnect re-seeds the list (recovers approvals that arrived while the socket
// was down); the push events keep it live thereafter.
function onApprovalConnectionState(state: unknown) {
  if (state === 'connected') void seedPendingApprovals()
}

function subscribeApprovals() {
  rpcApprovalUnsubs.push(
    rpcStore.on('exec.approval.requested', onApprovalRequested as RpcEventHandler),
    rpcStore.on('exec.approval.resolved', onApprovalResolved as RpcEventHandler),
    rpcStore.on('plugin.approval.requested', onApprovalRequested as RpcEventHandler),
    rpcStore.on('plugin.approval.resolved', onApprovalResolved as RpcEventHandler),
    rpcStore.on('_state', onApprovalConnectionState as RpcEventHandler),
  )
}

function unsubscribeApprovals() {
  rpcApprovalUnsubs.forEach(unsub => unsub())
  rpcApprovalUnsubs.length = 0
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

onMounted(() => {
  window.visualViewport?.addEventListener('resize', syncMobileKeyboard)
  window.addEventListener(LOCAL_SESSIONS_DELETED_EVENT, handleLocalSessionsDeleted)
  loadAgents()
  loadSessions()
  sessionListSubscription.subscribe()
  // Keep the approval badge/count live app-wide, not just on the Approvals page.
  subscribeApprovals()
  // Seed now in case the socket is already connected (the `_state` listener
  // covers later reconnects); recovers a request pending before mount.
  if (rpcStore.isConnected) void seedPendingApprovals()
})

onUnmounted(() => {
  window.removeEventListener(LOCAL_SESSIONS_DELETED_EVENT, handleLocalSessionsDeleted)
  if (sessionRefreshTimer) clearTimeout(sessionRefreshTimer)
  sessionListSubscription.cleanup()
  unsubscribeApprovals()
  if (titleDebounce) {
    clearTimeout(titleDebounce)
    titleDebounce = null
  }
  document.title = BASE_TITLE
  window.visualViewport?.removeEventListener('resize', syncMobileKeyboard)
})

</script>

<style scoped>
/* Topbar connection pill as a button (web): inherits the base .conn-pill look
   and state colors, adds button reset + an affordance that it is clickable. */
.conn-pill--link {
  cursor: pointer;
  font-family: inherit;
}
.conn-pill--link:hover {
  filter: brightness(1.08);
}
.conn-pill--link:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
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
