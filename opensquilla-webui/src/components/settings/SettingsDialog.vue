<template>
  <div class="settings-overlay" @click.self="requestClose()">
    <Transition name="settings-pop" appear @after-leave="onLeaveComplete">
    <section
      v-if="visible"
      ref="modalRef"
      class="settings-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-modal-title"
    >
      <header class="settings-modal__head">
        <h2 id="settings-modal-title" class="settings-modal__title">{{ t('settings.dialog.title') }}</h2>
        <button
          ref="closeBtn"
          type="button"
          class="btn btn--icon btn--ghost"
          :disabled="saveAllPending"
          :aria-label="t('common.close')"
          :title="t('common.close')"
          @click="requestClose()"
        >
          <Icon name="x" :size="16" />
        </button>
      </header>

      <!-- The readiness banner needs config + status, so it waits for load. The
           rail and Connection panel render immediately (below) so the gateway
           can be (re)connected even before any config loads. -->
      <template v-if="loaded">
      <div class="settings-banner" :class="hasSetupAction ? 'is-warn' : 'is-ok'">
        <div class="settings-banner__row">
          <Icon :name="hasSetupAction ? 'info' : 'check'" :size="16" aria-hidden="true" />
          <template v-if="hasSetupAction">
            <strong class="settings-banner__count">{{ t('settings.dialog.actionNeeded', { count: actionItems.length }) }}</strong>
            <span class="settings-banner__items">
              <button
                v-for="item in actionItems"
                :key="item.label"
                type="button"
                class="settings-banner__item"
                :aria-label="t('settings.dialog.openSection', { label: item.label, section: sectionLabel(item.section) })"
                @click="selectSection(item.section)"
              >{{ item.label }}</button>
            </span>
          </template>
          <span v-else class="settings-banner__ready">{{ t('settings.dialog.readyToRun') }}</span>
          <span class="settings-banner__spacer"></span>
          <button
            v-if="showCliHandoff"
            type="button"
            class="settings-banner__toggle"
            :aria-expanded="disclosureOpen ? 'true' : 'false'"
            aria-controls="settings-banner-disclosure"
            @click="disclosureOpen = !disclosureOpen"
          >
            <span class="settings-banner__chevron" :class="{ 'is-open': disclosureOpen }" aria-hidden="true">&#9656;</span>
            <span>{{ t('settings.dialog.cliHandoff') }}</span>
          </button>
        </div>
        <div v-if="showCliHandoff" v-show="disclosureOpen" id="settings-banner-disclosure" class="settings-banner__disclosure">
          <div class="setup-cli">
            <section v-if="fixCommands.length > 0" class="setup-cli__group" :aria-label="t('settings.dialog.fixNow')">
              <div class="setup-cli__group-head"><h4 class="control-panel__eyebrow">{{ t('settings.dialog.fixNow') }}</h4></div>
              <SetupCommandBlock
                v-for="cmd in fixCommands"
                :key="cmd.label"
                class="setup-cli__row"
                :label="cmd.label"
                :command="cmd.command"
                @copy="copyCommand"
              />
            </section>
            <section class="setup-cli__group" :aria-label="t('settings.dialog.cliHandoff')">
              <div class="setup-cli__group-head"><h4 class="control-panel__eyebrow">{{ t('settings.dialog.cliHandoff') }}</h4></div>
              <SetupCommandBlock
                v-for="cmd in handoffCommands"
                :key="cmd.label"
                class="setup-cli__row"
                :label="cmd.label"
                :command="cmd.command"
                @copy="copyCommand"
              />
            </section>
            <section class="setup-cli__group" :aria-label="t('settings.dialog.cliRecipes')">
              <div class="setup-cli__group-head"><h4 class="control-panel__eyebrow">{{ t('settings.dialog.cliRecipes') }}</h4></div>
              <SetupCommandBlock
                v-for="cmd in recipeCommands"
                :key="cmd.label"
                class="setup-cli__row"
                :label="cmd.label"
                :command="cmd.command"
                @copy="copyCommand"
              />
            </section>
          </div>
          <div class="setup-summary" :aria-label="t('settings.dialog.configSummary')">
            <div v-for="row in configSummary" :key="row.label">
              <span>{{ row.label }}</span><strong>{{ row.value }}</strong>
            </div>
          </div>
        </div>
      </div>
      </template>

      <div
        class="settings-body"
        :inert="saveAllPending ? true : undefined"
        :aria-busy="saveAllPending ? 'true' : undefined"
      >
        <nav ref="railRef" class="settings-rail" role="tablist" :aria-label="t('settings.dialog.sections')" :aria-orientation="railOrientation">
          <template v-for="(s, i) in visibleSections" :key="s.id">
            <!-- Presentational group eyebrow: labels the rail without adding a
                 tab stop. Rendered when the group changes so each bin is headed
                 once. Hidden on the mobile horizontal strip. -->
            <span
              v-if="i === 0 || s.group !== visibleSections[i - 1].group"
              class="settings-rail__group"
              role="presentation"
              aria-hidden="true"
            >{{ t('settings.rail.groups.' + s.group) }}</span>
            <button
              :id="'settings-rail-' + s.id"
              type="button"
              role="tab"
              class="settings-rail__item"
              :class="{ 'is-active': activeRailSection === s.id }"
              :aria-selected="activeRailSection === s.id ? 'true' : 'false'"
              :aria-controls="'settings-section-' + (activeRailSection === s.id ? section : s.id)"
              :aria-label="s.client ? t('settings.rail.' + s.id) : `${t('settings.rail.' + s.id)}: ${sectionStatus(s.id).label}${sectionDirty(s.id) ? t('settings.dialog.unsavedSuffix') : ''}`"
              @click="selectSection(s.id)"
            >
              <Icon :name="s.icon" :size="16" aria-hidden="true" />
              <span class="settings-rail__label">{{ t('settings.rail.' + s.id) }}</span>
              <span v-if="sectionDirty(s.id)" class="settings-rail__dirty" aria-hidden="true"></span>
              <span v-if="!s.client && s.id === 'connection'" class="settings-rail__dot" :class="sectionStatus(s.id).tone" aria-hidden="true"></span>
              <span v-else-if="!s.client && sectionStatus(s.id).tone === 'is-warn'" class="settings-rail__warn" aria-hidden="true">!</span>
            </button>
          </template>
        </nav>

        <div
          :id="'settings-section-' + section"
          ref="panelRef"
          class="settings-panel"
          role="tabpanel"
          :aria-labelledby="'settings-rail-' + activeRailSection"
        >
          <fieldset
            class="settings-panel__interactions"
            :disabled="saveAllPending"
            :aria-busy="saveAllPending ? 'true' : undefined"
          >
          <!-- Connection renders regardless of load state: it is how you point
               the UI at a reachable gateway when nothing has loaded yet. -->
          <SetupConnectionPanel v-if="section === 'connection'" />

          <!-- Runtime (desktop only) also renders regardless of load state: it
               reports the owned gateway and offers logs/restart precisely for
               when the gateway is down and config never loaded. -->
          <DesktopRuntimePanel v-else-if="section === 'runtime' && isDesktop" />

          <!-- Optional cross-installation discovery is deliberately mounted
               only when the user opens this section. It never runs at app or
               Settings-dialog startup. -->
          <DataMigrationPanel v-else-if="section === 'dataMigration'" />

          <!-- Config-backed sections wait for readiness so their baselines are
               final before any field can be edited. -->
          <div v-else-if="!loaded" class="settings-loading">
            <LoadingSpinner />
          </div>
          <template v-else>
            <SetupProviderPanel
              v-if="section === 'provider'"
              :panel="providerPanel"
              :preset="presetPanel"
              @update-provider-selected="selectProvider"
              @provider-change="onProviderChange"
              @update-provider-field="updateProviderField"
              @update-llm-timeout="updateLlmTimeout"
              @update-context-window="updateContextWindow"
              @probe-connection="probeProviderConnection"
              @apply-preset="applyProviderPreset"
              @copy="copyCommand"
              @go-to-section="selectSection"
              @select-configured-provider="requestSelectConfiguredProvider"
              @remove-provider-profile="removeProviderProfile"
              @add-provider="requestAddProvider"
              @probe-configured-provider="probeConfiguredProvider"
              @activate-provider="activateProvider"
            />
            <SetupBehaviorPanel
              v-else-if="section === 'behavior'"
              :panel="behaviorPanel"
              @update-auto-session-titles="setAutoSessionTitles"
            />
            <SettingsPrivacyPanel
              v-else-if="section === 'privacy'"
              :panel="privacyPanel"
              @update-disable-network-observability="setDisableNetworkObservability"
            />
            <SetupModelStrategyPanel
              v-else-if="section === 'modelStrategy'"
              :panel="modelStrategyPanel"
              @update-strategy="setModelStrategy"
              @update-fixed-model="setFixedModel"
              @update-router-default-tier="setRouterDefaultTier"
              @update-router-visual-mode="setRouterVisualMode"
              @update-tier-field="updateTierField"
              @update-ensemble-scheme="setEnsembleScheme"
              @add-ensemble-candidate="addEnsembleCandidate"
              @remove-ensemble-candidate="removeEnsembleCandidate"
              @replace-ensemble-candidate="replaceEnsembleCandidate"
              @set-ensemble-aggregator="setEnsembleAggregator"
              @request-provider-models="discoverModelStrategyProviderModels"
              @import-ensemble-tier-candidates="importEnsembleTierCandidates"
              @migrate-ensemble-legacy="migrateEnsembleLegacy"
              @update-ensemble-min-successful="setEnsembleMinSuccessful"
              @update-ensemble-all-failed-policy="setEnsembleAllFailedPolicy"
              @go-to-section="selectSection"
            />
            <SetupCapabilitiesPanel
              v-else-if="section === 'capabilities'"
              :panel="capabilitiesPanel"
              @update-field="updateCapabilityField"
              @search-provider-change="onSearchProviderChange"
              @memory-provider-change="onMemoryProviderChange"
              @image-provider-change="onImageProviderChange"
              @save-search="saveSearch"
              @save-memory="saveMemory"
              @save-image="saveImage"
              @save-audio="saveAudio"
              @copy="copyCommand"
            />
            <SettingsAppearancePanel v-else-if="section === 'appearance'" />
            <SettingsKeyboardPanel v-else-if="section === 'keyboard'" />
            <SettingsAdvancedPanel
              v-else-if="section === 'advanced'"
              @open-agent-configuration="openAgentConfiguration"
              @open-data-maintenance="openDataMaintenance"
            />
          </template>
          </fieldset>
        </div>
      </div>

      <div
        v-if="loaded && hasUnsavedChanges"
        class="settings-dirtybar"
        :aria-busy="saveAllPending ? 'true' : undefined"
      >
        <span class="settings-dirtybar__pulse" aria-hidden="true"></span>
        <span class="settings-dirtybar__text" role="status" aria-live="polite" aria-atomic="true">
          {{ saveAllPending ? t('settings.dialog.savingChanges') : dirtyBarText }}
        </span>
        <span class="settings-dirtybar__spacer"></span>
        <button type="button" class="btn" :disabled="saveAllPending" @click="discardChanges">
          {{ dirtyDiscardLabel }}
        </button>
        <button
          type="button"
          class="btn btn--primary"
          :disabled="saveAllPending"
          :aria-busy="saveAllPending ? 'true' : undefined"
          @click="saveDirtySections"
        >{{ saveAllPending ? t('settings.dialog.savingChanges') : dirtySaveLabel }}</button>
      </div>

      <footer class="settings-foot">
        <span class="settings-foot__text">{{ t('settings.dialog.moreOptionsIn') }}</span>
        <code class="settings-foot__path">{{ displayConfigPath }}</code>
        <button
          type="button"
          class="settings-foot__copy"
          :aria-label="t('settings.dialog.copyConfigPath')"
          :title="t('settings.dialog.copyConfigPath')"
          @click="copyDisplayPath"
        >
          <Icon name="copy" :size="13" />
        </button>
        <span class="settings-foot__sep" aria-hidden="true">&middot;</span>
        <span class="settings-foot__text">{{ t('settings.dialog.applyLiveNote') }}</span>
      </footer>
    </section>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import SetupCommandBlock from '@/components/setup/SetupCommandBlock.vue'
import SetupBehaviorPanel from '@/components/setup/SetupBehaviorPanel.vue'
import SetupConnectionPanel from '@/components/settings/SetupConnectionPanel.vue'
import SetupProviderPanel from '@/components/setup/SetupProviderPanel.vue'
import SetupModelStrategyPanel from '@/components/setup/SetupModelStrategyPanel.vue'
import SetupCapabilitiesPanel from '@/components/setup/SetupCapabilitiesPanel.vue'
import SettingsPrivacyPanel from '@/components/settings/SettingsPrivacyPanel.vue'
import SettingsAppearancePanel from '@/components/settings/SettingsAppearancePanel.vue'
import SettingsKeyboardPanel from '@/components/settings/SettingsKeyboardPanel.vue'
import SettingsAdvancedPanel from '@/components/settings/SettingsAdvancedPanel.vue'
import DesktopRuntimePanel from '@/components/settings/DesktopRuntimePanel.vue'
import DataMigrationPanel from '@/components/settings/DataMigrationPanel.vue'
import { useSetupCatalog, SETTINGS_SECTIONS } from '@/composables/setup/useSetupCatalog'
import { parseProviderHash, sectionFromRouteParam } from '@/composables/setup/useSettingsSection'
import { useConfirm } from '@/composables/useConfirm'
import { usePlatform } from '@/platform'
import '@/styles/settings-forms.css'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const { confirm, confirmState } = useConfirm()

// Desktop owns a local gateway, so it exposes a Runtime section the web build
// hides. `desktopOnly` sections are filtered out everywhere else.
const isDesktop = usePlatform().capabilities.isDesktop
// The CLI handoff disclosure assumes a terminal where `opensquilla` resolves;
// the desktop shell has none, so the whole block is web-only.
const showCliHandoff = usePlatform().capabilities.hasTerminalWorkflow
const visibleSections = computed(() => SETTINGS_SECTIONS.filter(s => !s.desktopOnly || isDesktop))

const {
  section,
  setSection,
  loaded,
  providerPanel,
  behaviorPanel,
  privacyPanel,
  modelStrategyPanel,
  presetPanel,
  capabilitiesPanel,
  hasSetupAction,
  actionItems,
  fixCommands,
  handoffCommands,
  recipeCommands,
  configSummary,
  configPath,
  selectInitialSection,
  sectionStatus,
  sectionDirty,
  dirtySections,
  hasUnsavedChanges,
  saveAllPending,
  saveDirtySections,
  discardChanges,
  selectProvider,
  requestSelectConfiguredProvider,
  requestAddProvider,
  setAutoSessionTitles,
  setDisableNetworkObservability,
  setModelStrategy,
  setFixedModel,
  setRouterDefaultTier,
  setRouterVisualMode,
  addEnsembleCandidate,
  removeEnsembleCandidate,
  replaceEnsembleCandidate,
  setEnsembleAggregator,
  discoverModelStrategyProviderModels,
  importEnsembleTierCandidates,
  migrateEnsembleLegacy,
  setEnsembleScheme,
  setEnsembleMinSuccessful,
  setEnsembleAllFailedPolicy,
  applyProviderPreset,
  updateProviderField,
  updateLlmTimeout,
  updateContextWindow,
  probeProviderConnection,
  probeConfiguredProvider,
  activateProvider,
  removeProviderProfile,
  updateTierField,
  updateCapabilityField,
  onProviderChange,
  onSearchProviderChange,
  onMemoryProviderChange,
  onImageProviderChange,
  saveSearch,
  saveMemory,
  saveImage,
  saveAudio,
  copyCommand,
  copyConfigPath,
} = useSetupCatalog()

// The maintenance screen is a child of Advanced, not a first-level tab. Keep
// the parent selected while its nested route is open so the rail communicates
// hierarchy without advertising migration during normal Settings use.
const activeRailSection = computed(() => (
  section.value === 'dataMigration' ? 'advanced' : section.value
))

const modalRef = ref<HTMLElement | null>(null)
const railRef = ref<HTMLElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)
const closeBtn = ref<HTMLButtonElement | null>(null)

// Keep the active section's rail tab in view — on mobile the rail scrolls
// horizontally, so a deep-linked or later section would otherwise sit off-screen.
function scrollActiveTabIntoView() {
  void nextTick(() => {
    const el = railRef.value?.querySelector<HTMLElement>('.settings-rail__item.is-active')
    if (!el) return
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: reduce ? 'auto' : 'smooth' })
  })
}

function resetActivePanelScroll() {
  void nextTick(() => {
    const panel = panelRef.value
    if (!panel) return
    panel.scrollTo({ top: 0, left: 0, behavior: 'auto' })
  })
}
// Drives the modal's enter/leave <Transition>. Closing flips this to false so the
// leave animation plays; the actual route navigation is deferred to onLeaveComplete.
const visible = ref(true)
const disclosureOpen = ref(false)
const isMobile = ref(window.matchMedia('(max-width: 768px)').matches)
// Set once the user picks a section so the deep-link auto landing (which waits
// on readiness data) never stomps navigation made while config was loading.
let userNavigated = false

const railOrientation = computed(() => (isMobile.value ? 'horizontal' : 'vertical'))
const dirtySectionNames = computed(() => dirtySections.value.map(s => s.label).join(' · '))
const dirtyProviderLabel = computed(() => (
  providerPanel.value.credentialPanel?.providerLabel
  || providerPanel.value.providerSelected
  || t('settings.rail.provider')
))
const onlyDirtySection = computed(() => (
  dirtySections.value.length === 1 ? dirtySections.value[0]?.id : ''
))
const dirtyBarText = computed(() => {
  if (dirtySections.value.length > 1) {
    return t('settings.dialog.unsavedCount', { count: dirtySections.value.length })
  }
  if (onlyDirtySection.value === 'provider') {
    return t('settings.dialog.unsavedProvider', { provider: dirtyProviderLabel.value })
  }
  return t('settings.dialog.unsavedIn', { sections: dirtySectionNames.value })
})
const dirtySaveLabel = computed(() => {
  if (dirtySections.value.length > 1) {
    return t('settings.dialog.saveAll', { count: dirtySections.value.length })
  }
  if (onlyDirtySection.value === 'provider') {
    return t('settings.dialog.saveProvider', { provider: dirtyProviderLabel.value })
  }
  if (onlyDirtySection.value === 'modelStrategy') return t('settings.dialog.saveRouting')
  return t('common.save')
})
const dirtyDiscardLabel = computed(() => {
  if (dirtySections.value.length > 1) {
    return t('settings.dialog.discardAll', { count: dirtySections.value.length })
  }
  if (onlyDirtySection.value === 'provider') {
    return t('settings.dialog.discardProvider', { provider: dirtyProviderLabel.value })
  }
  if (onlyDirtySection.value === 'modelStrategy') return t('settings.dialog.discardRouting')
  return t('common.discard')
})
const displayConfigPath = computed(() => configPath.value || '~/.opensquilla/config.toml')

// Where to return when the overlay closes. Captured on open from the route the
// user came from; null for a cold deep link (the overlay route was the entry
// point, e.g. someone pasted /settings/connection), which falls back to home.
let returnTo: string | null = null
// The control that had focus when the overlay opened, restored on close. For a
// cold deep link there is no in-app invoker, so close moves focus to the
// sidebar Settings button instead of leaving it on a detached node.
let invokerEl: HTMLElement | null = null
let mq: MediaQueryList | null = null
let closing = false
let transferringFocus = false

const routeParam = computed(() => route.params.section)
// `/setup` → `/settings/auto` asks for the first not-ready section once
// readiness is known; it is a routing sentinel, never a real rail section.
const wantsAutoSection = computed(() => routeParam.value === 'auto')

function sectionLabel(id: string): string {
  return SETTINGS_SECTIONS.find(s => s.id === id)?.label || id
}

// Reflect the active section in the URL with replace (not push) so the browser
// Back button exits Settings in one step rather than walking section history.
// Only replace when the section actually changes — an unconditional replace on
// first mount would strip an incoming `#provider-<id>` deep-link hash before
// applyProviderHash could act on it.
function selectSection(id: string) {
  userNavigated = true
  setSection(id)
  if (route.params.section !== id) {
    void router.replace({ path: `/settings/${id}` })
  }
}

// Resolve the section the route is asking for. Connection works before config
// loads; the auto sentinel waits for readiness; everything else maps the param
// (or the default) straight through.
function applyRouteSection() {
  if (wantsAutoSection.value) {
    if (loaded.value && !userNavigated) selectInitialSection('auto')
    return
  }
  const resolved = sectionFromRouteParam(routeParam.value)
  if (resolved === 'dataMigration') {
    setSection(resolved)
    return
  }
  // A desktopOnly section requested where it is unavailable (e.g. a stale
  // /settings/runtime deep link on web) has no rail entry or panel branch; fall
  // back to the default so the dialog never renders an empty body.
  setSection(visibleSections.value.some(s => s.id === resolved) ? resolved : 'provider')
}

// `#provider-<id>` deep links land on the Provider section with that provider
// preselected and focus on its first unfilled field. Applied once per hash
// value so a later manual provider change is never stomped by a stale hash.
let appliedProviderHash = ''

function applyProviderHash() {
  const providerId = parseProviderHash(route.hash)
  if (!providerId || !loaded.value || section.value !== 'provider') return
  if (appliedProviderHash === route.hash) return
  const panel = providerPanel.value
  if (!panel.runtimeProviders.some((p: { providerId: string }) => p.providerId === providerId)) return
  appliedProviderHash = route.hash
  if (panel.providerSelected !== providerId) {
    // Same path as picking the provider in the <select>: select + reset fields.
    selectProvider(providerId)
    onProviderChange()
  }
  void nextTick(() => focusFirstEmptyProviderInput())
}

// Focus the first empty required input in the freshly-preselected panel;
// rendered inputs don't always carry `required`, so fall back to first-empty.
function focusFirstEmptyProviderInput() {
  const panel = panelRef.value
  if (!panel) return
  const inputs = Array.from(panel.querySelectorAll<HTMLInputElement>(
    'input.control-input:not([disabled]):not([readonly]), textarea.control-input:not([disabled])',
  ))
  const target = inputs.find(input => input.required && !input.value.trim())
    ?? inputs.find(input => !input.value.trim())
  target?.focus()
}

function copyDisplayPath() {
  if (configPath.value) {
    copyConfigPath()
  } else {
    copyCommand(displayConfigPath.value)
  }
}

function sidebarSettingsButton(): HTMLElement | null {
  return document.querySelector<HTMLElement>('.sidebar-foot button[data-icon="settings"]')
}

// A usable focus-restore target: a real element still in the document that is
// neither <body> (the cold-deep-link case, where activeElement was never a
// meaningful invoker) nor inside the dialog itself (which is about to unmount).
function usableInvoker(): HTMLElement | null {
  if (!invokerEl || invokerEl === document.body) return null
  if (!document.contains(invokerEl)) return null
  if (modalRef.value?.contains(invokerEl)) return null
  return invokerEl
}

// Leave the overlay: restore focus first (the route change unmounts us), then
// navigate to the captured return location, or home for a cold deep link.
function navigateAway() {
  // Never route close through bare '/': its redirect re-runs the saved-route
  // logic and could bounce back into Settings. Push the platform default view
  // directly (same breakpoint/platform branch as the '/' redirect in sharedRoutes)
  // so close is a single, predictable, loop-proof exit. `returnTo` is already
  // null for a cold deep link (onMounted rejects any '/settings…' back-entry).
  const fallback = isDesktop || window.matchMedia('(max-width: 768px)').matches ? '/chat' : '/sessions'
  void router.push(returnTo ?? fallback)
}

// The modal's leave transition finished — perform the deferred navigation that
// actually unmounts the overlay. Vue fires this on the next frame even when the
// transition is disabled (reduced motion), so close never stalls.
function onLeaveComplete() {
  if (closing) navigateAway()
}

function closeOverlay() {
  if (closing) return
  closing = true
  // Restore focus to the invoker synchronously (don't wait out the leave
  // animation) so keyboard users and the focus-return tests see focus land now.
  const target = usableInvoker() ?? sidebarSettingsButton()
  target?.focus()
  invokerEl = null
  // Flip visibility to play the modal's leave transition; onLeaveComplete then
  // navigates away (which unmounts the route component).
  visible.value = false
}

// This is an intentional modal-to-page transition, not a Settings close/back
// action. Suppress the old invoker restoration while routing, then focus the
// destination heading so keyboard and screen-reader users perceive the change.
async function openAgentConfiguration() {
  if (transferringFocus) return
  transferringFocus = true
  try {
    const failure = await router.push('/agents')
    if (failure) {
      transferringFocus = false
      return
    }
    await nextTick()
    document.getElementById('agents-page-title')?.focus()
  } catch (error) {
    transferringFocus = false
    throw error
  }
}

// Unlike a cold/deep-linked maintenance route (where the modal close button
// deliberately keeps initial focus), an explicit activation inside Advanced
// is an in-dialog view transition. Move context to the newly mounted heading
// after Vue has replaced the panel so keyboard and screen-reader users do not
// remain focused on a control that just left the DOM.
async function openDataMaintenance() {
  selectSection('dataMigration')
  await nextTick()
  panelRef.value?.querySelector<HTMLElement>('[data-testid="data-migration-heading"]')?.focus()
}

// One discard prompt shared by every exit path: requestClose (Escape, the
// close button, backdrop click) and the history-back leave guard below.
function confirmDiscard(): Promise<boolean> {
  return confirm({
    title: t('settings.dialog.discardTitle'),
    body: t('settings.dialog.discardBody'),
    primaryLabel: t('settings.dialog.discardPrimary'),
  })
}

// Closes unless a section carries unsaved edits and the user keeps them.
async function requestClose(): Promise<boolean> {
  if (saveAllPending.value) return false
  if (hasUnsavedChanges.value && !(await confirmDiscard())) return false
  closeOverlay()
  return true
}

// History traversal (browser Back, a trackpad back-swipe) pops the /settings
// route and unmounts the overlay without passing through requestClose, which
// would silently drop unsaved edits. This router-level guard runs the same
// discard prompt for any navigation that leaves /settings while the dialog is
// mounted; cancelling restores the URL. Registered on the router rather than
// via onBeforeRouteLeave because selectSection's replace swaps the matched
// record between `settings` and `settings-section` while the viewKey-keyed
// component instance survives — a component guard would stay bound to the
// stale record and never fire on the real exit.
const removeLeaveGuard = router.beforeEach(async (to) => {
  if (closing) return true
  if (to.path === '/settings' || to.path.startsWith('/settings/')) return true
  if (saveAllPending.value) return false
  if (!hasUnsavedChanges.value) return true
  // requestClose already has the prompt up — hold this navigation instead of
  // stacking a second prompt (useConfirm cancels a pending request).
  if (confirmState.value) return false
  return confirmDiscard()
})

function onDocumentKeydown(event: KeyboardEvent) {
  if (event.defaultPrevented) return
  // The confirm modal owns the keyboard while it is open; let it handle Escape
  // so a single keypress cannot both dismiss the prompt and re-open it.
  if (confirmState.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    void requestClose()
    return
  }
  if (event.key !== 'Tab') return
  const rootEl = modalRef.value
  if (!rootEl) return
  const focusables = Array.from(rootEl.querySelectorAll<HTMLElement>(
    'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'))
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const active = document.activeElement as HTMLElement | null
  const inside = !!active && rootEl.contains(active)
  if (event.shiftKey && (!inside || active === first)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (!inside || active === last)) {
    event.preventDefault()
    first.focus()
  }
}

function onViewportChange(event: MediaQueryListEvent) {
  isMobile.value = event.matches
}

// Keep the active section in sync as the route param changes (deep link, Back,
// or a same-overlay section switch). The auto sentinel resolves once readiness
// loads; the loaded watcher below completes that case.
watch(routeParam, () => applyRouteSection())

// A provider deep-link hash can arrive (or change) after mount. (Legacy
// #channel- hashes never reach this dialog: a router guard rewrites them to
// the /channels workspace before the settings route resolves.)
watch(() => route.hash, () => { applyProviderHash() })

// Whenever the active section changes (rail click, deep link, Back), bring its
// tab into view on the horizontally-scrolling mobile rail.
watch(section, () => {
  scrollActiveTabIntoView()
  resetActivePanelScroll()
  applyProviderHash()
})

// The auto deep link lands on its readiness-derived section once config is
// known, unless the user already navigated during the load.
watch(loaded, (isLoaded) => {
  if (isLoaded && wantsAutoSection.value && !userNavigated) selectInitialSection('auto')
  // Catalog data is required to validate a provider hash, so (re)try now.
  if (isLoaded) { applyProviderHash() }
})

onMounted(() => {
  // Capture the return location from where we entered the overlay. router.back()
  // is avoided because it cannot be trusted for cold deep links; an explicit
  // push to the stored path (or home) gives a single, predictable exit.
  const from = router.options.history.state.back
  returnTo = typeof from === 'string' && !from.startsWith('/settings') ? from : null
  invokerEl = document.activeElement instanceof HTMLElement ? document.activeElement : null
  applyRouteSection()
  applyProviderHash()
  scrollActiveTabIntoView()
  document.addEventListener('keydown', onDocumentKeydown)
  mq = window.matchMedia('(max-width: 768px)')
  mq.addEventListener('change', onViewportChange)
  nextTick(() => closeBtn.value?.focus())
})

onUnmounted(() => {
  removeLeaveGuard()
  document.removeEventListener('keydown', onDocumentKeydown)
  mq?.removeEventListener('change', onViewportChange)
  mq = null
  // A route-driven unmount that did not go through closeOverlay (e.g. the user
  // pressed browser Back) still owes focus restoration: the real invoker, or
  // the sidebar Settings button for a cold deep link, never a detached node.
  if (!closing && !transferringFocus) (usableInvoker() ?? sidebarSettingsButton())?.focus()
  invokerEl = null
})
</script>

<style scoped>
.settings-overlay {
  align-items: center;
  background: var(--scrim);
  display: flex;
  inset: 0;
  justify-content: center;
  padding: var(--sp-6);
  position: fixed;
  z-index: 300;
}

.settings-modal {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  box-shadow: var(--shadow-xl);
  display: flex;
  flex-direction: column;
  height: min(85vh, 100%);
  overflow: hidden;
  width: min(1200px, 100%);
}

/* Symmetric modal motion: slides up + fades in on open, slides down + fades out
   on close. The close navigation is deferred until this leave finishes (see
   closeOverlay / onLeaveComplete), so unlike the old entrance-only keyframe the
   modal no longer pops out instantly. Tokens: entrance decelerates; the exit is
   a tier faster and accelerates. */
.settings-pop-enter-active {
  transition: opacity var(--dur-base) var(--ease-out),
              transform var(--dur-base) var(--ease-out);
}
.settings-pop-leave-active {
  transition: opacity var(--dur-fast) var(--ease-in),
              transform var(--dur-fast) var(--ease-in);
}
.settings-pop-enter-from {
  opacity: 0;
  transform: translateY(12px);
}
.settings-pop-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

@media (prefers-reduced-motion: reduce) {
  .settings-pop-enter-active,
  .settings-pop-leave-active {
    transition: none;
  }
}

.settings-modal__head {
  align-items: center;
  border-bottom: 1px solid var(--border);
  display: flex;
  flex-shrink: 0;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4);
}

.settings-modal__title {
  flex: 1;
  font-size: var(--fs-lg);
  font-weight: 700;
  margin: 0;
}

.settings-loading {
  align-items: center;
  display: flex;
  flex: 1;
  justify-content: center;
}

/* Readiness banner */
.settings-banner {
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
  max-height: 45%;
  overflow-y: auto;
}

.settings-banner.is-ok {
  background: color-mix(in srgb, var(--ok) 8%, var(--bg-surface));
  color: var(--ok);
}

.settings-banner.is-warn {
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  color: var(--warn);
}

.settings-banner__row {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  min-height: 40px;
  padding: var(--sp-2) var(--sp-4);
}

.settings-banner__count {
  font-size: var(--fs-sm);
  font-weight: 600;
  white-space: nowrap;
}

.settings-banner__ready {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.settings-banner__items {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-1);
  min-width: 0;
}

.settings-banner__item {
  background: transparent;
  border: 1px solid color-mix(in srgb, var(--warn) 30%, var(--border));
  border-radius: var(--radius-full);
  color: var(--text-muted);
  cursor: pointer;
  font-size: var(--fs-xs);
  padding: 2px 10px;
}

.settings-banner__item:hover {
  border-color: var(--warn);
  color: var(--text);
}

.settings-banner__spacer {
  flex: 1;
}

.settings-banner__toggle {
  align-items: center;
  background: transparent;
  border: none;
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font-size: var(--fs-xs);
  gap: var(--sp-1);
  padding: 4px var(--sp-2);
}

.settings-banner__toggle:hover {
  color: var(--text);
}

.settings-banner__chevron {
  display: inline-block;
  transition: transform var(--transition);
}

.settings-banner__chevron.is-open {
  transform: rotate(90deg);
}

@media (prefers-reduced-motion: reduce) {
  .settings-banner__chevron {
    transition: none;
  }
}

.settings-banner__disclosure {
  border-top: 1px solid var(--border);
  color: var(--text);
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  padding: var(--sp-4);
}

/* Body: rail + active section */
.settings-body {
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.settings-rail {
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  gap: 2px;
  overflow-y: auto;
  padding: var(--sp-3) var(--sp-2);
  width: 200px;
}

.settings-rail__item {
  align-items: center;
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  text-align: left;
}

.settings-rail__item:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.settings-rail__item.is-active {
  background: var(--bg-elevated);
  box-shadow: inset 2px 0 0 var(--accent);
  color: var(--text);
  font-weight: 600;
}

.settings-rail__label {
  flex: 1;
}

.settings-rail__dot {
  border-radius: 50%;
  flex-shrink: 0;
  height: 7px;
  width: 7px;
}

.settings-rail__dot.is-ok { background: var(--ok); }
.settings-rail__dot.is-warn { background: var(--warn-fill); }
.settings-rail__dot.is-muted { background: var(--text-dim); opacity: 0.5; }

.settings-rail__warn {
  align-items: center;
  background: var(--warn-fill);
  clip-path: polygon(50% 0, 100% 100%, 0 100%);
  color: var(--bg);
  display: inline-flex;
  flex-shrink: 0;
  font-size: 7px;
  font-weight: 700;
  height: 11px;
  justify-content: center;
  line-height: 1;
  padding-top: 3px;
  width: 12px;
}

.settings-rail__dirty {
  background: var(--accent);
  border-radius: 50%;
  flex-shrink: 0;
  height: 5px;
  width: 5px;
}

/* Quiet uppercase eyebrow that heads each rail group (see .control-nav-group__label).
   Non-interactive, so it never enters the tab order. */
.settings-rail__group {
  color: var(--text-dim);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  margin: var(--sp-3) 0 var(--sp-1);
  padding: 0 var(--sp-3);
  text-transform: uppercase;
}

.settings-rail__group:first-child {
  margin-top: 0;
}

.settings-panel {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: var(--sp-4);
}

.settings-panel__interactions {
  border: 0;
  margin: 0;
  min-inline-size: 0;
  padding: 0;
}

/* Dirty bar */
.settings-dirtybar {
  align-items: center;
  background: var(--bg-elevated);
  border-top: 1px solid var(--border);
  display: flex;
  flex-shrink: 0;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-4);
}

.settings-dirtybar__pulse {
  background: var(--accent);
  border-radius: 50%;
  height: 8px;
  width: 8px;
}

.settings-dirtybar__text {
  color: var(--text);
  font-size: var(--fs-sm);
}

.settings-dirtybar__spacer {
  flex: 1;
}

/* Footer */
.settings-foot {
  align-items: center;
  border-top: 1px solid var(--border);
  color: var(--text-dim);
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  min-width: 0;
  padding: var(--sp-2) var(--sp-4);
}

.settings-foot__path {
  color: var(--text-muted);
  flex: 1 1 240px;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings-foot__copy {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  height: 24px;
  justify-content: center;
  width: 24px;
}

.settings-foot__copy:hover {
  background: var(--bg-hover);
  border-color: var(--border);
  color: var(--text);
}

/* Mobile: full screen, horizontal section chips */
@media (max-width: 768px) {
  .settings-overlay {
    padding: 0;
  }

  .settings-modal {
    border: none;
    border-radius: 0;
    height: 100%;
    width: 100%;
  }

  .settings-body {
    flex-direction: column;
  }

  .settings-rail {
    border-bottom: 1px solid var(--border);
    border-right: none;
    flex-direction: row;
    overflow-x: auto;
    overflow-y: hidden;
    padding: var(--sp-2);
    width: 100%;
    /* Signal that the strip scrolls: fade the leading/trailing edges, and snap
       tabs so they don't end mid-cut. (black = opaque in an alpha mask.) */
    scroll-snap-type: x proximity;
    -webkit-mask-image: linear-gradient(to right, transparent 0, black 16px, black calc(100% - 16px), transparent 100%);
    mask-image: linear-gradient(to right, transparent 0, black 16px, black calc(100% - 16px), transparent 100%);
  }

  .settings-rail__item {
    flex-shrink: 0;
    min-height: 44px;
    scroll-snap-align: start;
  }

  /* The horizontal chip strip stays flat — group eyebrows would break the row. */
  .settings-rail__group {
    display: none;
  }

  .settings-panel {
    padding: var(--sp-3);
  }

  .settings-foot {
    align-items: flex-start;
    gap: var(--sp-1) var(--sp-2);
    padding-bottom: max(var(--sp-2), env(safe-area-inset-bottom));
  }

  .settings-foot__text:first-child {
    display: none;
  }

  .settings-foot__path {
    flex-basis: calc(100% - 32px);
  }

  .settings-foot__sep {
    display: none;
  }
}
</style>
