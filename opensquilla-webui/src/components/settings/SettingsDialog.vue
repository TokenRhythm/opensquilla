<template>
  <Teleport to="body">
    <div class="settings-overlay" @click.self="requestClose($event)">
      <Transition name="settings-pop" appear @after-leave="onLeaveComplete">
      <section
        v-if="visible"
        ref="modalRef"
        class="settings-modal"
        :class="{ 'settings-modal--categories': showCategories }"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-modal-title"
      >
      <div
        class="settings-body"
        :inert="settingsInteractionLocked ? true : undefined"
        :aria-busy="settingsInteractionLocked ? 'true' : undefined"
      >
        <nav ref="railRef" v-show="!isMobile || showCategories" class="settings-rail" :role="isMobile ? undefined : 'tablist'" :inert="isMobile && !showCategories ? true : undefined" :aria-hidden="isMobile && !showCategories ? 'true' : undefined" :aria-label="t('settings.dialog.sections')" :aria-orientation="isMobile ? undefined : 'vertical'" @keydown="onRailKeydown">
          <template v-for="(s, i) in visibleSections" :key="s.id">
            <!-- Presentational group eyebrow: labels the rail without adding a
                 tab stop. Rendered when the group changes so each bin is headed
                 once, including the mobile category list. -->
            <span
              v-if="s.group && (i === 0 || s.group !== visibleSections[i - 1]?.group)"
              class="settings-rail__group"
              role="presentation"
              aria-hidden="true"
            >{{ t('settings.rail.groups.' + s.group) }}</span>
            <button
              :id="'settings-rail-' + s.id"
              type="button"
              :role="isMobile ? undefined : 'tab'"
              class="settings-rail__item"
              :class="{ 'is-active': activeRailSection === s.id }"
              :aria-selected="isMobile ? undefined : activeRailSection === s.id ? 'true' : 'false'"
              :tabindex="isMobile || activeRailSection === s.id ? 0 : -1"
              :aria-controls="isMobile ? undefined : 'settings-section-' + (activeRailSection === s.id ? section : s.id)"
              :aria-label="s.client ? t('settings.rail.' + s.id) : `${t('settings.rail.' + s.id)}: ${sectionStatus(s.id).label}${sectionDirty(s.id) ? t('settings.dialog.unsavedSuffix') : ''}`"
              @click="selectSection(s.id)"
            >
              <Icon :name="s.icon" :size="16" aria-hidden="true" />
              <span class="settings-rail__label">{{ t('settings.rail.' + s.id) }}</span>
              <span v-if="sectionDirty(s.id)" class="settings-rail__dirty" aria-hidden="true"></span>
              <span v-if="s.id === 'gateway'" class="settings-rail__dot" :class="sectionStatus(s.id).tone" aria-hidden="true"></span>
              <span
                v-else-if="s.id === 'provider' && loaded && sectionStatus(s.id).tone === 'is-warn'"
                class="settings-rail__dot is-danger"
                :title="sectionStatus(s.id).label"
                aria-hidden="true"
              ></span>
              <Icon v-if="isMobile" name="chevronRight" :size="16" aria-hidden="true" />
            </button>
          </template>
        </nav>

        <div class="settings-main">
          <header class="settings-modal__head">
            <button v-if="isMobile && !showCategories" type="button" class="btn btn--icon btn--ghost settings-modal__back" :disabled="settingsInteractionLocked" :aria-label="t('settings.dialog.backToCategories')" @click="showCategoryList">
              <Icon name="chevronLeft" :size="18" aria-hidden="true" />
            </button>
            <h2 id="settings-modal-title" class="settings-modal__title">{{ isMobile && !showCategories ? t('settings.rail.' + activeRailSection) : t('settings.dialog.title') }}</h2>
            <SettingsSearch :is-desktop="isDesktop" :disabled="settingsInteractionLocked" @select="selectSearchResult" />
            <button
              ref="closeBtn"
              type="button"
              class="btn btn--icon btn--ghost settings-modal__close"
              :disabled="hasPendingSettingsWrite"
              :aria-label="t('common.close')"
              :title="t('common.close')"
              @click="requestClose($event)"
            >
              <Icon name="x" :size="16" />
            </button>
          </header>

        <div
          :id="'settings-section-' + section"
          ref="panelRef"
          v-show="!showCategories"
          class="settings-panel"
          :role="isMobile ? 'region' : 'tabpanel'"
          :inert="showCategories ? true : undefined"
          :aria-hidden="showCategories ? 'true' : undefined"
          :aria-labelledby="isMobile ? 'settings-modal-title' : 'settings-rail-' + activeRailSection"
          tabindex="-1"
        >
          <fieldset
            class="settings-panel__interactions"
            :disabled="settingsInteractionLocked"
            :aria-busy="settingsInteractionLocked ? 'true' : undefined"
          >
          <!-- Gateway remains available even when config readiness cannot load,
               because this is where users recover the connection/runtime. -->
          <SettingsGatewayPanel
            v-if="section === 'gateway'"
            :is-desktop="isDesktop"
          />

          <!-- Memory & Export is first-level and owns its own RPC gate. -->
          <SettingsMemoryPanel v-else-if="section === 'memory'" />

          <!-- Optional cross-installation discovery is deliberately mounted
               only when the user opens this section. It never runs at app or
               Settings-dialog startup. -->
          <DataMigrationPanel v-else-if="section === 'dataMigration'" />

          <!-- Mixed pages render their local controls immediately and gate only
               the config-backed rows on catalog readiness. -->
          <SettingsGeneralPanel
            v-else-if="section === 'general'"
            :panel="behaviorPanel"
            :loaded="loaded"
            :is-desktop="isDesktop"
            @update-auto-session-titles="setAutoSessionTitles"
          />
          <SettingsSecurityPrivacyPanel
            v-else-if="section === 'securityPrivacy'"
            ref="securityPrivacyPanel"
            :panel="privacyPanel"
            :loaded="loaded"
            :is-desktop="isDesktop"
            @update-network-reporting-enabled="setNetworkReportingEnabled"
          />
          <SettingsAppearancePanel v-else-if="section === 'interface'" />
          <SettingsKeyboardPanel v-else-if="section === 'shortcuts'" />
          <SettingsAdvancedPanel
            v-else-if="section === 'advanced'"
            :auto-capture="memoryPanel.autoCapture"
            :loaded="loaded"
            :config-path="displayConfigPath"
            @copy-config-path="copyDisplayPath"
            @update-auto-capture="setMemoryAutoCapture"
            @open-data-maintenance="openDataMaintenance"
          />

          <!-- AI configuration pages wait for complete readiness baselines. -->
          <div v-else-if="!loaded" class="settings-loading" role="status">
            <LoadingSpinner />
            <strong>{{ t('settings.rail.' + section) }}</strong>
            <span>{{ t('shared.loading') }}</span>
          </div>
          <template v-else>
            <SetupModelCapacity v-if="section === 'modelStrategy' && capacityTarget" :key="JSON.stringify(capacityTarget)"
              v-bind="capacityTarget" initial-open hide-trigger
              :disabled="saveAllPending || primaryMutationPending || modelStrategyRoutingBusy" />
            <SetupProviderPanel
              v-if="section === 'provider'"
              :panel="providerPanel"
              :dirty="providerDraftDirty"
              :saving="saveAllPending || providerSavePending || primaryMutationPending || modelStrategyRoutingBusy || providerPanel.busy"
              @update-provider-selected="selectProvider"
              @provider-change="onProviderChange"
              @update-provider-field="updateProviderField"
              @update-llm-timeout="updateLlmTimeout"
              @update-llm-thinking="updateLlmThinking"
              @update-context-window="updateContextWindow"
              @probe-connection="probeProviderConnection"
              @cancel-provider-probe="cancelProviderProbe"
              @refresh-models="refreshProviderModels"
              @cancel-configured-provider-probe="cancelConfiguredProviderProbe"
              @save-provider="saveProvider"
              @save-provider-and-activate="saveProviderAndActivate"
              @cancel-provider-edit="cancelProviderEdit"
              @copy="copyCommand"
              @go-to-section="selectSection"
              @select-configured-provider="requestSelectConfiguredProvider"
              @remove-provider-profile="removeProviderProfile"
              @add-provider="requestAddProvider"
              @probe-configured-provider="probeConfiguredProvider"
              @activate-provider="activateProvider"
              @update-image-generation-opt-in="setProviderImageGenerationOptIn"
            />
            <SetupModelStrategyPanel
              v-else-if="section === 'modelStrategy'"
              :panel="modelStrategyPanel"
              :routing-mode-busy="modelStrategyRoutingBusy || providerPanel.busy"
              @update-strategy="setModelStrategy"
              @reset-recommended-router="resetRecommendedRouter"
              @update-fixed-provider="setFixedProvider"
              @update-fixed-model="setFixedModel"
              @update-llm-thinking="updateModelStrategyThinking"
              @update-ensemble-thinking="updateEnsembleThinking"
              @update-router-default-tier="setRouterDefaultTier"
              @update-router-visual-mode="setRouterVisualMode"
              @update-tier-field="updateTierField"
              @update-ensemble-scheme="setEnsembleScheme"
              @update-ensemble-min-successful="setEnsembleMinSuccessful"
              @update-ensemble-all-failed-policy="setEnsembleAllFailedPolicy"
              @update-ensemble-proposer-max-retries="setEnsembleProposerMaxRetries"
              @add-ensemble-candidate="addEnsembleCandidate"
              @remove-ensemble-candidate="removeEnsembleCandidate"
              @replace-ensemble-candidate="replaceEnsembleCandidate"
              @set-ensemble-aggregator="setEnsembleAggregator"
              @request-provider-models="discoverModelStrategyProviderModels"
              @import-ensemble-tier-candidates="importEnsembleTierCandidates"
              @migrate-ensemble-legacy="migrateEnsembleLegacy"
              @go-to-section="selectSection"
            />
            <SetupCapabilitiesPanel
              v-else-if="section === 'capabilities'"
              :panel="capabilitiesPanel"
              @update-field="updateCapabilityField"
              @search-provider-change="onSearchProviderChange"
              @image-provider-change="onImageProviderChange"
              @use-image-recommendation="useImageRecommendation"
              @reset-capability="resetCapability"
            />
          </template>
          <VideoGenerationSettings
            v-if="videoSettingsInitialized"
            :hidden="!loaded || section !== 'capabilities'"
            ref="videoSettingsRef"
            @dirty-change="videoDirty = $event"
            @busy-change="videoBusy = $event"
          />
          </fieldset>
        </div>
        </div>
      </div>

      <div
        v-if="loaded && hasUnsavedChanges"
        class="settings-dirtybar"
        :aria-busy="settingsInteractionLocked ? 'true' : undefined"
      >
        <span class="settings-dirtybar__pulse" aria-hidden="true"></span>
        <span class="settings-dirtybar__text" role="status" aria-live="polite" aria-atomic="true">
          {{ settingsInteractionLocked ? t('settings.dialog.savingChanges') : dirtyBarText }}
        </span>
        <span class="settings-dirtybar__spacer"></span>
        <button type="button" class="btn" :disabled="settingsInteractionLocked" @click="discardChanges">
          {{ dirtyDiscardLabel }}
        </button>
        <button
          type="button"
          class="btn btn--primary"
          :disabled="settingsInteractionLocked"
          :aria-busy="settingsInteractionLocked ? 'true' : undefined"
          @click="saveDirtySections"
        >{{ settingsInteractionLocked ? t('settings.dialog.savingChanges') : dirtySaveLabel }}</button>
      </div>

      </section>
      </Transition>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { hasOpenDialogLayer } from '@/composables/useDialogA11y'
import Icon from '@/components/Icon.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import SetupProviderPanel from '@/components/setup/SetupProviderPanel.vue'
import SetupModelCapacity from '@/components/setup/SetupModelCapacity.vue'
import SetupModelStrategyPanel from '@/components/setup/SetupModelStrategyPanel.vue'
import SetupCapabilitiesPanel from '@/components/setup/SetupCapabilitiesPanel.vue'
import VideoGenerationSettings from '@/components/setup/VideoGenerationSettings.vue'
import SettingsAppearancePanel from '@/components/settings/SettingsAppearancePanel.vue'
import SettingsSearch from '@/components/settings/SettingsSearch.vue'
import SettingsKeyboardPanel from '@/components/settings/SettingsKeyboardPanel.vue'
import SettingsAdvancedPanel from '@/components/settings/SettingsAdvancedPanel.vue'
import SettingsMemoryPanel from '@/components/settings/SettingsMemoryPanel.vue'
import SettingsGatewayPanel from '@/components/settings/SettingsGatewayPanel.vue'
import SettingsGeneralPanel from '@/components/settings/SettingsGeneralPanel.vue'
import SettingsSecurityPrivacyPanel from '@/components/settings/SettingsSecurityPrivacyPanel.vue'
import DataMigrationPanel from '@/components/settings/DataMigrationPanel.vue'
import { useSetupCatalog, SETTINGS_SECTIONS } from '@/composables/setup/useSetupCatalog'
import {
  parseProviderHash,
  sectionFromRouteParam,
  settingsSectionAliasFor,
} from '@/composables/setup/useSettingsSection'
import { useConfirm } from '@/composables/useConfirm'
import { usePlatform } from '@/platform'
import '@/styles/settings-forms.css'

const route = useRoute()
const router = useRouter()
const capacityTarget = computed(() => {
  const provider = route.query.capacityProvider
  const model = route.query.capacityModel
  return typeof provider === 'string' && provider.trim() && provider.length <= 1024
    && typeof model === 'string' && model.trim() && model.length <= 1024 ? { provider, model } : null
})
const { t } = useI18n()
const { confirmChoice, confirmState } = useConfirm()

// The catalog retains the desktopOnly capability for future destinations. The
// consolidated rail currently shares all ten destinations across surfaces.
const isDesktop = usePlatform().capabilities.isDesktop
const visibleSections = computed(() => SETTINGS_SECTIONS.filter(s => !s.desktopOnly || isDesktop))

const {
  section,
  setSection,
  loaded,
  providerPanel,
  behaviorPanel,
  privacyPanel,
  memoryPanel,
  modelStrategyPanel,
  capabilitiesPanel,
  configPath,
  selectInitialSection,
  sectionStatus,
  sectionDirty,
  providerDraftDirty,
  dirtySections,
  hasUnsavedChanges,
  saveAllPending,
  providerSavePending,
  modelStrategyRoutingBusy,
  saveDirtySections,
  discardChanges,
  selectProvider,
  requestSelectConfiguredProvider,
  requestAddProvider,
  cancelProviderEdit,
  setAutoSessionTitles,
  setNetworkReportingEnabled,
  setMemoryAutoCapture,
  setProviderImageGenerationOptIn,
  setModelStrategy,
  resetRecommendedRouter,
  setFixedProvider,
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
  setEnsembleProposerMaxRetries,
  updateProviderField,
  updateLlmTimeout,
  updateLlmThinking,
  updateModelStrategyThinking,
  updateEnsembleThinking,
  updateContextWindow,
  probeProviderConnection,
  cancelProviderProbe,
  refreshProviderModels,
  probeConfiguredProvider,
  cancelConfiguredProviderProbe,
  activateProvider,
  removeProviderProfile,
  updateTierField,
  updateCapabilityField,
  onProviderChange,
  onSearchProviderChange,
  onImageProviderChange,
  useImageRecommendation,
  saveProvider,
  saveProviderAndActivate,
  primaryMutationPending,
  resetCapability,
  copyCommand,
  copyConfigPath,
} = useSetupCatalog()

// The maintenance screen is a child of Advanced, not a first-level tab. Keep
// the parent selected while its nested route is open so the rail communicates
// hierarchy without advertising migration during normal Settings use.
const activeRailSection = computed(() => (
  section.value === 'dataMigration'
    ? 'advanced'
    : section.value
))

const modalRef = ref<HTMLElement | null>(null)
const railRef = ref<HTMLElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)
const closeBtn = ref<HTMLButtonElement | null>(null)
const closeSavePending = ref(false)
const videoSettingsRef = ref<InstanceType<typeof VideoGenerationSettings> | null>(null)
const videoDirty = ref(false)
const videoBusy = ref(false)
const videoSettingsInitialized = ref(false)
watch([loaded, section], ([ready, activeSection], previous) => {
  if (!ready || activeSection !== 'capabilities') return
  if (!videoSettingsInitialized.value) {
    videoSettingsInitialized.value = true
  } else if (previous?.[1] !== 'capabilities') {
    void videoSettingsRef.value?.refreshCredentialStatus()
  }
}, { immediate: true })
const securityPrivacyPanel = ref<InstanceType<typeof SettingsSecurityPrivacyPanel> | null>(null)
const sandboxLeavePending = ref(false)
let sandboxLeaveWork: Promise<boolean> | null = null

function prepareSandboxLeave(): Promise<boolean> {
  if (sandboxLeaveWork) return sandboxLeaveWork
  const panel = securityPrivacyPanel.value
  if (!panel) return Promise.resolve(true)
  sandboxLeavePending.value = true
  const work = panel.prepareToLeave().finally(() => {
    sandboxLeavePending.value = false
    if (sandboxLeaveWork === work) sandboxLeaveWork = null
  })
  sandboxLeaveWork = work
  return work
}

// Keep the active destination in view without scrolling the hidden mobile rail.
function scrollActiveTabIntoView() {
  void nextTick(() => {
    if (isMobile.value && !showCategories.value) return
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
const isMobile = ref(window.matchMedia('(max-width: 768px)').matches)
// Returning to categories hides the existing form; it never remounts it or
// changes the current section. Explicit destinations bypass the category list.
const mobileView = ref<'list' | 'detail'>(route.params.section || route.hash ? 'detail' : 'list')
const showCategories = computed(() => isMobile.value && mobileView.value === 'list')
// Set once the user picks a section so the deep-link auto landing (which waits
// on readiness data) never stomps navigation made while config was loading.
let userNavigated = false

const dirtySectionNames = computed(() => (
  dirtySections.value.map(s => t(`settings.rail.${s.id}`)).join(' · ')
))
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
// Provider drafts deliberately stay out of the settings-wide dirty bar because
// their editor owns Save/Cancel. They still participate in every path that
// unmounts Settings so browser navigation cannot silently discard credentials.
const hasSettingsExitDraft = computed(() => (
  hasUnsavedChanges.value || providerDraftDirty.value || videoDirty.value
))
const hasPendingSettingsWrite = computed(() => (
  saveAllPending.value
  || videoBusy.value
  || providerSavePending.value
  || primaryMutationPending.value
  || modelStrategyRoutingBusy.value
  || closeSavePending.value
))
const settingsInteractionLocked = computed(() => (
  saveAllPending.value || closeSavePending.value || primaryMutationPending.value || sandboxLeavePending.value
))
const shouldGuardBrowserUnload = computed(() => (
  hasSettingsExitDraft.value || hasPendingSettingsWrite.value
  || securityPrivacyPanel.value?.hasUnsettledChanges === true
))

function onBeforeUnload(event: BeforeUnloadEvent) {
  if (!shouldGuardBrowserUnload.value) return
  event.preventDefault()
  event.returnValue = ''
}

let beforeUnloadAttached = false
function setBeforeUnloadAttached(attached: boolean) {
  if (attached === beforeUnloadAttached) return
  beforeUnloadAttached = attached
  if (attached) {
    window.addEventListener('beforeunload', onBeforeUnload)
  } else {
    window.removeEventListener('beforeunload', onBeforeUnload)
  }
}
watch(shouldGuardBrowserUnload, setBeforeUnloadAttached, { immediate: true })

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

const routeParam = computed(() => route.params.section)
// `/setup` → `/settings/auto` asks for the first not-ready section once
// readiness is known; it is a routing sentinel, never a real rail section.
const wantsAutoSection = computed(() => routeParam.value === 'auto')

const COMPOSITE_HASH_TARGETS: Record<string, string> = {
  '#connection': 'settings-gateway-connection',
  '#runtime': 'settings-gateway-runtime',
  '#support': 'settings-gateway-support',
  '#logs': 'settings-gateway-logs',
  '#local-logs': 'settings-gateway-local-logs',
  '#updates': 'settings-gateway-updates',
  '#privacy': 'settings-security-privacy',
  '#sandbox': 'settings-security-sandbox',
}

// Reflect the active section in the URL with replace (not push) so the browser
// Back button exits Settings in one step rather than walking section history.
// Only replace when the section actually changes — an unconditional replace on
// first mount would strip an incoming `#provider-<id>` deep-link hash before
// applyProviderHash could act on it.
function selectSection(id: string): boolean | Promise<boolean> {
  if (settingsInteractionLocked.value) return false
  const leave = id !== section.value
    && securityPrivacyPanel.value?.hasUnsettledChanges === true
    ? prepareSandboxLeave()
    : null
  if (leave) {
    return leave.then(ok => {
      if (!ok) return false
      userNavigated = true
      mobileView.value = 'detail'
      setSection(id)
      if (isMobile.value) void nextTick(() => panelRef.value?.focus({ preventScroll: true }))
      if (route.params.section !== id) return router.replace({ path: `/settings/${id}` }).then(() => true)
      return true
    })
  }
  userNavigated = true
  mobileView.value = 'detail'
  setSection(id)
  if (route.params.section !== id) {
    void router.replace({ path: `/settings/${id}` })
  }
  if (isMobile.value) void nextTick(() => panelRef.value?.focus({ preventScroll: true }))
  return true
}

async function showCategoryList() {
  if (settingsInteractionLocked.value) return
  mobileView.value = 'list'
  await nextTick()
  const active = railRef.value?.querySelector<HTMLElement>('.settings-rail__item.is-active')
  active?.focus({ preventScroll: true })
  active?.scrollIntoView?.({ block: 'nearest' })
}

function onRailKeydown(event: KeyboardEvent) {
  if (settingsInteractionLocked.value || event.altKey || event.ctrlKey || event.metaKey) return
  const tabs = Array.from(railRef.value?.querySelectorAll<HTMLButtonElement>('.settings-rail__item') ?? [])
  const target = event.target instanceof Element ? event.target.closest<HTMLButtonElement>('.settings-rail__item') : null
  if (!target) return
  const current = tabs.indexOf(target)
  if (current < 0 || !tabs.length) return
  const nextKey = 'ArrowDown'
  const previousKey = 'ArrowUp'
  let next: number
  if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = tabs.length - 1
  else if (event.key === nextKey) next = (current + 1) % tabs.length
  else if (event.key === previousKey) next = (current + tabs.length - 1) % tabs.length
  else return
  event.preventDefault()
  if (!isMobile.value) {
    const selected = selectSection(visibleSections.value[next].id)
    if (selected instanceof Promise) {
      void selected.then(ok => { if (ok) tabs[next]?.focus() })
      return
    }
    if (!selected) return
  }
  tabs[next].focus()
}

const SEARCH_TARGET_IDS: Record<string, string> = {
  'setup.connection.wsUrlLabel': 'conn-ws-url',
  'setup.connection.tokenLabel': 'conn-ws-token',
  'setup.runtime.title': 'settings-gateway-runtime',
  'setup.runtime.localGatewayTitle': 'settings-gateway-runtime',
  'setup.runtime.openLocalLogLocation': 'settings-gateway-local-logs',
  'updates.desktop.settingsTitle': 'settings-gateway-updates',
  'monitorSupport.title': 'settings-gateway-support',
  'monitorSupport.downloadBundle': 'settings-gateway-support',
  'settings.search.supportBundle': 'settings-gateway-support',
  'gatewayLogs.viewLogs': 'settings-gateway-logs',
  'settings.search.permissions': 'settings-security-sandbox',
  'settings.sandbox.title': 'settings-security-sandbox',
  'settings.sandbox.mode.title': 'settings-security-sandbox',
}

async function focusSearchResult(id: string, labelKey: string): Promise<void> {
  await nextTick()
  const panel = panelRef.value
  if (!panel || section.value !== id || settingsInteractionLocked.value) return

  const targetId = SEARCH_TARGET_IDS[labelKey]
  const label = labelKey ? t(labelKey) : ''
  const explicitTarget = targetId ? panel.querySelector<HTMLElement>(`#${targetId}`) : null
  const matched = label
    ? Array.from(panel.querySelectorAll<HTMLElement>('label, .control-row__label, .capability-card__title, .video-settings__title, h3, h4'))
      .find(element => element.textContent?.trim() === label)
    : undefined
  const row = matched?.closest<HTMLElement>('.control-row, .capability-card, .video-settings')
  // A section-only result must not focus an unrelated action at the top of
  // that section. Exact setting rows can focus their own native control.
  const candidate = explicitTarget
    ?? row?.querySelector<HTMLElement>('input:not(:disabled), select:not(:disabled), button:not(:disabled)')
    ?? matched
    ?? panel
  const target = candidate.matches('.control-select__native')
    ? candidate.closest('.control-select')?.querySelector<HTMLElement>('.control-select__trigger') ?? candidate
    : candidate
  // Search can target fields inside a collapsed connection disclosure. Open
  // every enclosing details element before scrolling or moving keyboard focus.
  for (let disclosure = target.closest<HTMLDetailsElement>('details'); disclosure;
    disclosure = disclosure.parentElement?.closest<HTMLDetailsElement>('details') ?? null) {
    disclosure.open = true
  }
  target.scrollIntoView?.({ block: 'nearest', behavior: 'auto' })
  if (!target.matches('input, select, button, [tabindex]')) target.tabIndex = -1
  target.focus({ preventScroll: true })
}

function selectSearchResult(id: string, labelKey: string) {
  const selected = selectSection(id)
  if (selected instanceof Promise) {
    void selected.then(ok => { if (ok) void focusSearchResult(id, labelKey) })
    return
  }
  if (selected) void focusSearchResult(id, labelKey)
}

// Resolve aliases and nested destinations. Old URLs are canonicalized with a
// subsection hash so bookmarks keep landing on their original content.
function applyRouteSection() {
  if (route.params.section || route.hash) mobileView.value = 'detail'
  if (wantsAutoSection.value) {
    if (loaded.value && !userNavigated) selectInitialSection('auto')
    return
  }
  const resolved = sectionFromRouteParam(routeParam.value)
  const alias = settingsSectionAliasFor(routeParam.value)
  if (alias) {
    void router.replace({
      path: `/settings/${alias.section}`,
      hash: route.hash || alias.hash || '',
    })
  }
  if (resolved === 'dataMigration') {
    setSection(resolved)
    return
  }
  // A future surface-specific destination requested where unavailable has no
  // rail entry or panel branch; fall back so the dialog never renders empty.
  setSection(visibleSections.value.some(s => s.id === resolved) ? resolved : 'provider')
}

function focusCompositeHash(): boolean {
  if (showCategories.value) return false
  const targetId = COMPOSITE_HASH_TARGETS[route.hash]
  if (!targetId) return false
  const target = document.getElementById(targetId)
  if (!target) return false
  target.scrollIntoView({ block: 'start', inline: 'nearest', behavior: 'auto' })
  const initialFocus = target.querySelector<HTMLElement>('[data-settings-initial-focus]')
  ;(initialFocus ?? target).focus({ preventScroll: true })
  return true
}

function applyCompositeHash() {
  void nextTick(() => { focusCompositeHash() })
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
  if (showCategories.value) return
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

function canRestoreFocus(element: HTMLElement | null): element is HTMLElement {
  if (!element || element === document.body || !element.isConnected) return false
  if (modalRef.value?.contains(element)) return false
  if (element.matches(':disabled') || element.closest('[inert], [hidden], [aria-hidden="true"]')) return false
  for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
    const style = getComputedStyle(ancestor)
    if (style.display === 'none' || style.visibility === 'hidden') return false
  }
  return true
}

// A usable focus-restore target: a real element still in the document that is
// neither <body> (the cold-deep-link case, where activeElement was never a
// meaningful invoker) nor inside the dialog itself (which is about to unmount).
function usableInvoker(): HTMLElement | null {
  return canRestoreFocus(invokerEl) ? invokerEl : null
}

function restoreSettingsFocus() {
  const targets = [
    usableInvoker(),
    document.querySelector<HTMLElement>('.sidebar-foot button[data-icon="settings"]'),
    document.querySelector<HTMLElement>('[data-testid="sidebar-toggle-collapsed"]'),
  ]
  targets.find(canRestoreFocus)?.focus({ preventScroll: true })
}

// Leave the overlay: restore focus first (the route change unmounts us), then
// navigate to the captured return location, or home for a cold deep link.
function navigateAway() {
  // Never route close through bare '/': its redirect re-runs the saved-route
  // logic and could bounce back into Settings. Push the platform default view
  // directly (same breakpoint/platform branch as the '/' redirect in sharedRoutes)
  // so close is a single, predictable, loop-proof exit. `returnTo` is already
  // null for a cold deep link (onMounted rejects any '/settings…' back-entry).
  void router.push(returnTo ?? '/chat')
}

// The modal's leave transition finished — perform the deferred navigation that
// actually unmounts the overlay. Vue fires this on the next frame even when the
// transition is disabled (reduced motion), so close never stalls.
function onLeaveComplete() {
  if (closing) navigateAway()
}

function closeOverlay(restoreFocus = true) {
  if (closing) return
  closing = true
  // Pointer users already chose their next location by clicking, so forcing
  // focus back to the Settings row leaves a misleading orange keyboard ring.
  // Keyboard-triggered closes still restore focus synchronously.
  if (restoreFocus) {
    restoreSettingsFocus()
  }
  invokerEl = null
  // Flip visibility to play the modal's leave transition; onLeaveComplete then
  // navigates away (which unmounts the route component).
  visible.value = false
}

// Unlike a cold/deep-linked maintenance route (where the modal close button
// deliberately keeps initial focus), an explicit activation inside Advanced
// is an in-dialog view transition. Move context to the newly mounted heading
// after Vue has replaced the panel so keyboard and screen-reader users do not
// remain focused on a control that just left the DOM.
async function openDataMaintenance() {
  if (!(await selectSection('dataMigration'))) return
  await nextTick()
  panelRef.value?.querySelector<HTMLElement>('[data-testid="data-migration-heading"]')?.focus()
}

// One exit prompt shared by every exit path: requestClose (Escape, the close
// button, backdrop click) and the history-back leave guard below. Saving is
// the primary action; discarding remains explicit and destructive.
function confirmExitChanges() {
  return confirmChoice({
    title: t('settings.dialog.discardTitle'),
    body: t('settings.dialog.discardBody'),
    primaryLabel: t('settings.dialog.saveAndClose'),
    primaryClass: 'btn--primary',
    secondaryLabel: t('settings.dialog.discardConfirmPrimary'),
    secondaryClass: 'btn--danger',
    showCancel: false,
  })
}

// Save every kind of Settings draft before allowing an exit. Provider drafts
// have their own save path, while the remaining sections share the dirty bar.
// If validation or a request fails, their dirty state remains and the dialog
// stays open for correction.
async function saveExitChanges(): Promise<boolean> {
  if (closeSavePending.value || hasPendingSettingsWrite.value) return false
  closeSavePending.value = true
  try {
    if (providerDraftDirty.value && !(await saveProvider())) return false
    if (hasUnsavedChanges.value) await saveDirtySections()
    if (videoDirty.value && !(await videoSettingsRef.value?.save())) return false
    await nextTick()
    return !hasSettingsExitDraft.value
  } finally {
    closeSavePending.value = false
  }
}

// Closes unless a section carries unsaved edits and the user chooses to keep
// them, save them, or discard them.
async function requestClose(event?: MouseEvent): Promise<boolean> {
  if (hasPendingSettingsWrite.value) return false
  if (!(await prepareSandboxLeave())) return false
  if (hasSettingsExitDraft.value) {
    const choice = await confirmExitChanges()
    if (choice === 'cancel') return false
    if (choice === 'primary' && !(await saveExitChanges())) return false
  }
  // Keyboard-generated click events have detail 0; real pointer clicks have a
  // positive click count. Preserve focus restoration for keyboard activation.
  closeOverlay(!event || event.detail === 0)
  return true
}

// History traversal (browser Back, a trackpad back-swipe) pops the /settings
// route and unmounts the overlay without passing through requestClose, which
// would silently drop unsaved edits. This router-level guard runs the same
// save/discard prompt for any navigation that leaves /settings while the dialog
// is mounted; cancelling restores the URL. Registered on the router rather than
// via onBeforeRouteLeave because selectSection's replace swaps the matched
// record between `settings` and `settings-section` while the viewKey-keyed
// component instance survives — a component guard would stay bound to the
// stale record and never fire on the real exit.
const removeLeaveGuard = router.beforeEach(async (to) => {
  if (closing) return true
  if (to.path === '/settings' || to.path.startsWith('/settings/')) {
    return to.path === route.path || await prepareSandboxLeave()
  }
  if (hasPendingSettingsWrite.value) return false
  if (!(await prepareSandboxLeave())) return false
  if (!hasSettingsExitDraft.value) return true
  // requestClose already has the prompt up — hold this navigation instead of
  // stacking a second prompt (useConfirm cancels a pending request).
  if (confirmState.value) return false
  const choice = await confirmExitChanges()
  if (choice === 'cancel') return false
  if (choice === 'primary') return saveExitChanges()
  return true
})

function isVisibleInDetails(element: HTMLElement): boolean {
  if (element.closest('[hidden], [inert]')) return false
  for (let disclosure = element.parentElement?.closest('details'); disclosure;
    disclosure = disclosure.parentElement?.closest('details')) {
    // Only the first direct summary and its descendants remain visible when
    // details is closed. An inner summary can still be hidden by an outer one.
    if (!disclosure.open && !disclosure.querySelector(':scope > summary')?.contains(element)) return false
  }
  return true
}

function onDocumentKeydown(event: KeyboardEvent) {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return
  // The confirm modal owns the keyboard while it is open; let it handle Escape
  // so a single keypress cannot both dismiss the prompt and re-open it.
  if (confirmState.value || hasOpenDialogLayer()) return
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
    .filter(element => element.getAttribute('tabindex') !== '-1'
      && !element.matches(':disabled')
      && !element.closest('[inert], [hidden], [aria-hidden="true"]')
      && isVisibleInDetails(element))
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
  const active = document.activeElement
  isMobile.value = event.matches
  // Do not leave keyboard focus inside a now-hidden rail or detail form.
  void nextTick(() => {
    if (active instanceof HTMLElement && (!active.isConnected || active.closest('[inert], [aria-hidden="true"]'))) {
      const target = showCategories.value
        ? railRef.value?.querySelector<HTMLElement>('.settings-rail__item.is-active')
        : panelRef.value
      target?.focus({ preventScroll: true })
    }
  })
}

// Keep the active section in sync as the route param changes (deep link, Back,
// or a same-overlay section switch). The auto sentinel resolves once readiness
// loads; the loaded watcher below completes that case.
watch(routeParam, () => applyRouteSection())

// A provider deep-link hash can arrive (or change) after mount. (Legacy
// #channel- hashes never reach this dialog: a router guard rewrites them to
// the /channels workspace before the settings route resolves.)
watch(() => route.hash, () => {
  if (route.hash) mobileView.value = 'detail'
  applyProviderHash()
  applyCompositeHash()
})

// Whenever the active section changes (rail click, deep link, Back), bring its
// tab into view on the horizontally-scrolling mobile rail.
watch(section, () => {
  scrollActiveTabIntoView()
  resetActivePanelScroll()
  applyProviderHash()
  applyCompositeHash()
})

// The auto deep link lands on its readiness-derived section once config is
// known, unless the user already navigated during the load.
watch(loaded, (isLoaded) => {
  if (isLoaded && wantsAutoSection.value && !userNavigated) selectInitialSection('auto')
  // Catalog data is required to validate a provider hash, so (re)try now.
  if (isLoaded) {
    applyProviderHash()
    applyCompositeHash()
  }
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
  nextTick(() => {
    if (!focusCompositeHash()) closeBtn.value?.focus()
  })
})

onUnmounted(() => {
  setBeforeUnloadAttached(false)
  removeLeaveGuard()
  document.removeEventListener('keydown', onDocumentKeydown)
  mq?.removeEventListener('change', onViewportChange)
  mq = null
  // A route-driven unmount that did not go through closeOverlay (e.g. the user
  // pressed browser Back) still owes focus restoration: the real invoker, or
  // the sidebar Settings button for a cold deep link, never a detached node.
  if (!closing) restoreSettingsFocus()
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
  display: flex;
  flex-wrap: wrap;
  flex-shrink: 0;
  gap: var(--sp-3);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--border);
}

.settings-modal__title {
  flex: 1;
  font-size: var(--fs-lg);
  font-weight: 600;
  margin: 0;
}

.settings-modal__close {
  border: 0;
  box-shadow: none;
}

.settings-modal__close:focus-visible {
  box-shadow: 0 0 0 1px var(--border-strong);
}

.settings-loading {
  align-items: center;
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: var(--sp-2);
  justify-content: center;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.settings-loading strong {
  color: var(--text);
  font-size: var(--fs-md);
}

/* Body: rail + active section */
.settings-body {
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.settings-main {
  background: var(--bg-surface);
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.settings-rail {
  background: var(--bg);
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
  min-height: 40px;
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

.settings-rail__item:focus-visible {
  box-shadow: var(--focus-ring-inset);
  outline: none;
}

.settings-rail__label {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.settings-rail__dot {
  border-radius: 50%;
  flex-shrink: 0;
  height: 7px;
  width: 7px;
}

.settings-rail__dot.is-ok { background: var(--ok); }
.settings-rail__dot.is-danger { background: var(--danger); }
.settings-rail__dot.is-warn { background: var(--warn-fill); }
.settings-rail__dot.is-muted { background: var(--text-dim); opacity: 0.5; }

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
  font-size: 0.75rem;
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
  padding: var(--sp-5);
  overscroll-behavior: contain;
}

.settings-panel:focus { outline: none; }

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

/* Mobile: category list -> detail. Both remain mounted so navigating back or
   resizing cannot discard the currently edited form. */
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

  .settings-main {
    display: contents;
  }

  .settings-modal__head {
    order: 0;
    background: var(--bg-surface);
    padding: var(--sp-3);
    gap: var(--sp-2);
  }

  .settings-modal__title {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .settings-modal__back,
  .settings-modal__close {
    flex-shrink: 0;
    min-width: 44px;
    min-height: 44px;
  }

  .settings-rail {
    order: 1;
    border: 0;
    flex: 1;
    min-height: 0;
    flex-direction: column;
    overflow-y: auto;
    padding: var(--sp-3);
    width: 100%;
    box-sizing: border-box;
  }

  .settings-rail__item {
    flex-shrink: 0;
    min-height: 48px;
  }

  .settings-rail__group {
    margin-top: var(--sp-4);
  }

  .settings-panel {
    order: 2;
    min-height: 0;
    background: var(--bg-surface);
    padding: var(--sp-3);
  }

  .settings-dirtybar {
    flex-wrap: wrap;
    padding: var(--sp-3);
    gap: var(--sp-2);
  }

  .settings-dirtybar__text { flex: 1 1 65%; }
  .settings-dirtybar__spacer { display: none; }
}
</style>
