<template>
  <div class="sk-stage control-stage control-stage--spacious">
    <header class="sk-stage__header control-stage__header">
      <div class="sk-stage__title-block control-stage__title-block">
        <h1 class="sk-stage__title control-stage__title">{{ t('cronSkills.skillsView.title') }}</h1>
        <p class="sk-stage__subtitle control-stage__subtitle">{{ t('cronSkills.skillsView.subtitle') }}</p>
      </div>
      <div class="sk-stage__actions control-stage__actions">
        <div class="sk-search-wrap" :style="{ visibility: activeTab === 'installed' ? 'visible' : 'hidden' }">
          <span class="sk-search-icon">
            <Icon name="search" :size="16" />
          </span>
          <input
            v-model="filterText"
            class="sk-search-input"
            type="search"
            :placeholder="t('cronSkills.skillsView.filterPlaceholder')"
            autocomplete="off"
          />
        </div>
        <button
          class="btn btn--ghost"
          data-testid="skills-reload"
          type="button"
          :disabled="reloading"
          :aria-busy="reloading"
          :title="t('cronSkills.skillsView.reload')"
          @click="manualReload"
        >
          <Icon name="refresh" :size="16" />
          <span>{{ reloading ? t('cronSkills.skillsView.refreshing') : t('cronSkills.skillsView.reload') }}</span>
        </button>
      </div>
    </header>

    <SkillsStats
      :tiles="statTiles"
      :active-key="statusFilter"
      :proposal-count="proposals.length"
      @select="selectStatusFilter"
      @show-proposals="showProposalsFromStats"
    />

    <div class="sk-tabs" role="tablist" :aria-label="t('cronSkills.skillsView.tabsLabel')">
      <button
        id="sk-tab-installed"
        class="sk-tab"
        :class="{ 'is-active': activeTab === 'installed' }"
        type="button"
        role="tab"
        :aria-selected="activeTab === 'installed'"
        aria-controls="sk-panel-installed"
        @click="activeTab = 'installed'"
      >
        <Icon name="skills" :size="16" />
        <span>{{ t('cronSkills.skillsView.tabInstalled') }}</span>
      </button>
      <button
        id="sk-tab-registry"
        class="sk-tab"
        :class="{ 'is-active': activeTab === 'registry' }"
        type="button"
        role="tab"
        :aria-selected="activeTab === 'registry'"
        aria-controls="sk-panel-registry"
        @click="activeTab = 'registry'"
      >
        <Icon name="download" :size="16" />
        <span>{{ t('cronSkills.skillsView.tabCommunity') }}</span>
      </button>
    </div>

    <div v-show="activeTab === 'installed'" class="sk-panel" role="tabpanel" id="sk-panel-installed" aria-labelledby="sk-tab-installed">
      <div class="sk-installed">
        <details
          v-if="proposalsSettings.available"
          class="sk-group sk-group--ap-settings"
          :open="proposalsSettingsOn"
        >
          <summary class="sk-group__head">
            <span class="sk-group__caret">▾</span>
            <span class="sk-group__label">{{ t('cronSkills.autoPropose.title') }}</span>
            <span class="sk-group__count">{{ proposalsSettingsOn ? t('cronSkills.autoPropose.on') : t('cronSkills.autoPropose.off') }}</span>
            <span class="sk-group__meta">{{ t('cronSkills.autoPropose.meta') }}</span>
          </summary>
          <div class="sk-ap-settings">
            <label class="sk-ap-toggle">
              <ControlSwitch
                :checked="proposalsSettings.enabled"
                :aria-label="t('cronSkills.autoPropose.scheduledLabel')"
                @change="(v) => toggleAutoPropose('enabled', v)"
              />
              <span class="sk-ap-toggle__label">{{ t('cronSkills.autoPropose.scheduledLabel') }}</span>
              <i18n-t keypath="cronSkills.autoPropose.scheduledHint" tag="span" class="sk-ap-toggle__hint">
                <template #cron><code>{{ proposalsSettings.cron || '0 5 * * *' }}</code></template>
              </i18n-t>
            </label>
            <label class="sk-ap-toggle">
              <ControlSwitch
                :checked="proposalsSettings.on_dream_complete"
                :aria-label="t('cronSkills.autoPropose.dreamLabel')"
                @change="(v) => toggleAutoPropose('on_dream_complete', v)"
              />
              <span class="sk-ap-toggle__label">{{ t('cronSkills.autoPropose.dreamLabel') }}</span>
              <span class="sk-ap-toggle__hint">{{ t('cronSkills.autoPropose.dreamHint') }}</span>
            </label>
            <label class="sk-ap-toggle">
              <ControlSwitch
                :checked="proposalsSettings.auto_enable"
                :aria-label="t('cronSkills.autoPropose.autoEnableLabel')"
                @change="(v) => toggleAutoPropose('auto_enable', v)"
              />
              <span class="sk-ap-toggle__label">{{ t('cronSkills.autoPropose.autoEnableLabel') }}</span>
              <i18n-t keypath="cronSkills.autoPropose.autoEnableHint" tag="span" class="sk-ap-toggle__hint">
                <template #risk><code>{{ proposalsSettings.auto_enable_max_risk || 'low' }}</code></template>
              </i18n-t>
            </label>
            <label class="sk-ap-toggle">
              <span class="sk-ap-toggle__label">{{ t('cronSkills.autoPropose.riskCeilingLabel') }}</span>
              <select
                class="sk-ap-select"
                :value="proposalsSettings.auto_enable_max_risk || 'low'"
                @change="setAutoEnableRisk(($event.target as HTMLSelectElement).value)"
              >
                <option value="low">{{ t('cronSkills.autoPropose.riskLow') }}</option>
                <option value="medium">{{ t('cronSkills.autoPropose.riskMedium') }}</option>
                <option value="high">{{ t('cronSkills.autoPropose.riskHigh') }}</option>
              </select>
              <span class="sk-ap-toggle__hint">{{ t('cronSkills.autoPropose.riskCeilingHint') }}</span>
            </label>
          </div>
        </details>

        <PendingSkillProposals
          ref="proposalsPanelRef"
          :proposals="proposals"
          @show="openProposalDialog"
          @accept="acceptProposal"
          @reject="rejectProposal"
        />
        <AutoEnabledSkills :skills="autoEnabledSkills" @disable="disableAutoEnabled" />
        <SkillGroup
          :title="t('cronSkills.skillsView.metaSkillsTitle')"
          :description="t('cronSkills.skillsView.metaSkillsDesc')"
          :skills="metaSkills"
          group-class="sk-group--meta"
          meta
          @open="openSkillDialog"
        />
        <SkillGroup
          v-for="layer in visibleLayerGroups"
          :key="layer.key"
          :title="skillLayerLabel(layer.key)"
          :description="skillLayerHelp(layer.key)"
          :skills="layer.skills"
          @open="openSkillDialog"
        />

        <div v-if="installedEmpty" class="state">
          <div class="state-icon">
            <Icon name="skills" :size="36" />
          </div>
          <p class="state-text">
            <i18n-t v-if="filterText" keypath="cronSkills.skillsView.noMatchFilter">
              <template #filter><strong>{{ filterText }}</strong></template>
            </i18n-t>
            <template v-else>{{ emptyMessage }}</template>
          </p>
        </div>
      </div>
    </div>

    <div v-show="activeTab === 'registry'" class="sk-panel" role="tabpanel" id="sk-panel-registry" aria-labelledby="sk-tab-registry">
      <SkillsRegistryPanel
        v-model:registry-query="registryQuery"
        v-model:github-url="githubUrl"
        :results="registryResults"
        :loading="registryLoading"
        :installing-id="installingId"
        @search="searchRegistry"
        @install-github="installGithub"
        @install="installSkill"
      />
    </div>

    <SkillDetailDialog
      :skill="selectedSkill"
      :proposal="selectedProposal"
      :loading-content="selectedSkillLoading"
      :content-error="selectedSkillError"
      :install-feedback="installFeedback"
      :installing-deps-id="installingDepsId"
      :uninstalling-name="uninstallingName"
      @close="closeDialog"
      @install-deps="installDepsAndMaybeClose"
      @uninstall="uninstallSkillAndClose"
    />
  </div>
</template>

<script setup lang="ts">
import { nextTick, onActivated, onDeactivated, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ControlSwitch from '@/components/ControlSwitch.vue'
import AutoEnabledSkills from '@/components/skills/AutoEnabledSkills.vue'
import PendingSkillProposals from '@/components/skills/PendingSkillProposals.vue'
import SkillDetailDialog from '@/components/skills/SkillDetailDialog.vue'
import SkillGroup from '@/components/skills/SkillGroup.vue'
import SkillsRegistryPanel from '@/components/skills/SkillsRegistryPanel.vue'
import SkillsStats from '@/components/skills/SkillsStats.vue'
import { useSkillProposals } from '@/composables/skills/useSkillProposals'
import { useSkillDetailController } from '@/composables/skills/useSkillDetailController'
import { useSkillRegistry } from '@/composables/skills/useSkillRegistry'
import { skillLayerHelp, skillLayerLabel, useSkillsCatalog } from '@/composables/skills/useSkillsCatalog'
import { useToasts } from '@/composables/useToasts'
import { useRpcStore } from '@/stores/rpc'
import type { Proposal, Skill } from '@/types/skills'

interface SkillReloadError {
  name?: string
  path?: string
  message?: string
  kept_previous?: boolean
}

interface SkillReloadResult {
  success: boolean
  changed: boolean
  partial: boolean
  generation: number
  added?: string[]
  removed?: string[]
  modified?: string[]
  errors?: SkillReloadError[]
}

const { t } = useI18n()
const { pushToast } = useToasts()
const rpc = useRpcStore()
const activeTab = ref('installed')
const reloading = ref(false)
const selectedProposal = ref<Proposal | null>(null)
const proposalsPanelRef = ref<InstanceType<typeof PendingSkillProposals> | null>(null)

let loadData: () => Promise<boolean>

const proposalsModel = useSkillProposals(rpc, async () => { await loadData() })
const {
  proposals,
  autoEnabledSkills,
  proposalsSettings,
  proposalsSettingsOn,
  loadProposals,
  toggleAutoPropose,
  setAutoEnableRisk,
  showProposal,
  acceptProposal,
  rejectProposal,
  disableAutoEnabled,
} = proposalsModel

const catalog = useSkillsCatalog(rpc, {
  proposals,
  autoEnabledSkills,
  proposalsSettings,
  loadProposals,
})

const {
  filterText,
  statusFilter,
  metaSkills,
  visibleLayerGroups,
  installedEmpty,
  emptyMessage,
  statTiles,
  setStatusFilter,
} = catalog

loadData = catalog.loadData

function reloadSummary(result: SkillReloadResult): string {
  return t('cronSkills.skillsView.reloadSummary', {
    added: result.added?.length || 0,
    removed: result.removed?.length || 0,
    modified: result.modified?.length || 0,
  })
}

async function manualReload() {
  if (reloading.value) return
  reloading.value = true
  try {
    await rpc.waitForConnection()
    const result = await rpc.call<SkillReloadResult>('skills.reload')
    // Always redraw from the catalog the Gateway is actually serving. On a
    // failed publish this is the prior last-known-good generation.
    const listed = await loadData()
    if (listed === false) {
      throw new Error(t('cronSkills.skillsView.reloadListFailed'))
    }

    if (!result.success) {
      const error = result.errors?.[0]?.message || t('cronSkills.skillsView.reloadUnknownError')
      pushToast(t('cronSkills.skillsView.reloadFailed', { error }), { tone: 'danger' })
    } else if (result.partial) {
      pushToast(t('cronSkills.skillsView.reloadPartial', {
        generation: result.generation,
        summary: reloadSummary(result),
        errors: result.errors?.length || 0,
      }), { tone: 'warn' })
    } else if (!result.changed) {
      pushToast(t('cronSkills.skillsView.reloadNoChanges', {
        generation: result.generation,
      }))
    } else {
      pushToast(t('cronSkills.skillsView.reloadSuccess', {
        generation: result.generation,
        summary: reloadSummary(result),
      }), { tone: 'ok' })
    }
  } catch (err) {
    pushToast(t('cronSkills.skillsView.reloadFailed', {
      error: (err as Error).message,
    }), { tone: 'danger' })
  } finally {
    reloading.value = false
  }
}

const registry = useSkillRegistry(rpc, loadData)
const {
  registryQuery,
  githubUrl,
  registryResults,
  registryLoading,
  installingId,
  installingDepsId,
  uninstallingName,
  searchRegistry,
  installGithub,
  installSkill,
  installDeps,
  uninstallSkill,
} = registry

const skillDetail = useSkillDetailController({ rpc, installDeps })
const {
  selectedSkill,
  selectedSkillLoading,
  selectedSkillError,
  installFeedback,
  openSkill,
  closeSkill,
  installCurrentDependencies,
} = skillDetail

// This view is kept-alive (route meta.keepAlive), so the data fetch is bound on
// activation rather than mount — onMounted/onUnmounted only fire on first mount /
// cache eviction, not when navigating away and back. onActivated also runs on
// first display, so it covers the initial load while also refreshing on revisit.
// There are no subscriptions or polls in this view's data stack, so teardown is a
// no-op, but it is kept idempotent and wired to both onDeactivated and onUnmounted
// to match the reference pattern and guard against future additions.
let unsubs: Array<() => void> = []

function teardownLive() {
  unsubs.forEach(unsub => unsub())
  unsubs = []
  closeDialog()
}

onActivated(() => {
  void loadData()
})

onDeactivated(teardownLive)
onUnmounted(teardownLive)

function scrollToProposals() {
  proposalsPanelRef.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function selectStatusFilter(key: string) {
  activeTab.value = 'installed'
  setStatusFilter(key)
}

async function showProposalsFromStats() {
  activeTab.value = 'installed'
  await nextTick()
  scrollToProposals()
}

async function openSkillDialog(skill: Skill) {
  selectedProposal.value = null
  await openSkill(skill)
}

async function openProposalDialog(proposalId: string) {
  const proposal = await showProposal(proposalId)
  if (!proposal) return
  closeSkill()
  selectedProposal.value = proposal
}

function closeDialog() {
  closeSkill()
  selectedProposal.value = null
}

async function installDepsAndMaybeClose(name: string, installId: string) {
  await installCurrentDependencies(name, installId)
}

async function uninstallSkillAndClose(name: string) {
  const removed = await uninstallSkill(name)
  if (removed) closeDialog()
}
</script>

<style>
/* Search */
.sk-search-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.sk-search-icon {
  position: absolute;
  left: 10px;
  color: var(--text-dim);
  pointer-events: none;
  display: inline-flex;
  align-items: center;
}
.sk-search-input {
  padding: 8px 12px 8px 34px;
  font-size: var(--fs-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  outline: none;
  min-width: 200px;
  transition: border-color var(--transition), box-shadow var(--transition);
}
/* base.css resets text inputs via input:not([type="radio"]):not([type="checkbox"])
   — specificity (0,2,1), which outranks the .sk-search-input class and drops the
   leading-icon clearance (and elevated fill), letting the search/download icon
   overlap the placeholder. Re-assert just those two properties at matching reach;
   the :not() mirror clears the base reset without touching the --lg width rule. */
.sk-search-input:not([type="radio"]):not([type="checkbox"]) {
  padding: 8px 12px 8px 34px;
  background: var(--bg-elevated);
}
.sk-search-input:focus {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.sk-search-wrap--lg .sk-search-input {
  min-width: 320px;
}

/* Tabs */
.sk-tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border);
}
.sk-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 10px 18px;
  background: transparent;
  border: 0;
  border-bottom: 2px solid transparent;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--text-muted);
  cursor: pointer;
  transition: color var(--transition), border-color var(--transition);
}
.sk-tab.is-active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.sk-tab:hover:not(.is-active) {
  color: var(--text);
}

/* Panels */
.sk-panel {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

/* Groups */
.sk-group {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.sk-group--meta {
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
}
.sk-group--proposals {
  border-color: color-mix(in srgb, var(--warn) 30%, var(--border));
}
.sk-group--ap-settings {
  border-color: color-mix(in srgb, var(--accent) 20%, var(--border));
}
.sk-group__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  cursor: pointer;
  user-select: none;
  background: var(--bg-elevated);
  font-size: var(--fs-sm);
}
.sk-group__caret {
  color: var(--text-dim);
  font-size: 10px;
  transition: transform var(--dur-base) var(--ease-standard);
}
.sk-group[open] .sk-group__caret {
  transform: rotate(180deg);
}
.sk-group__label {
  font-weight: 600;
  color: var(--text);
}
.sk-group__count {
  font-size: var(--fs-xs);
  color: var(--text-dim);
  background: var(--bg);
  padding: 1px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
}
.sk-group__meta {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  margin-left: auto;
}

/* Grid */
.sk-grid {
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
}
.sk-card__head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.sk-card__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.sk-card__dot.is-ready {
  background: var(--ok);
}
.sk-card__dot.is-needs {
  background: var(--warn-fill);
}
.sk-card__dot.is-unverified {
  background: var(--text-dim);
}
.sk-card__emoji {
  font-size: 14px;
  line-height: 1;
}
.sk-card__name {
  font-weight: 600;
  font-size: var(--fs-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.sk-card__kind-badge {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent);
  flex-shrink: 0;
}
.sk-card__desc {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  line-height: 1.4;
}
.sk-card__deps {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.sk-card__dep {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-family: var(--font-mono);
  font-size: 9px;
  padding: 1px 5px;
}
.sk-card__dep--missing {
  border-color: color-mix(in srgb, var(--warn) 45%, var(--border));
  color: var(--warn);
}
.sk-card__dep--advisory {
  border-style: dashed;
  color: var(--text-muted);
}
.sk-card__sub-row {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  margin-top: 2px;
}
.sk-card__sub-label {
  font-size: 10px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-right: 2px;
}
.sk-card__sub-chip {
  font-size: 10px;
  padding: 1px 6px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
}
.sk-card__sub-chip--more {
  background: transparent;
  border-style: dashed;
}

/* Proposals list */
.sk-proposals-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
}
.sk-proposal-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  flex-wrap: wrap;
}
.sk-proposal-row__head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
}
.sk-proposal-row__id {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text);
  background: var(--bg-elevated);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
}
.sk-proposal-row__actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}
.sk-prop-chip {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text-muted);
}
.sk-prop-chip--ok {
  border-color: color-mix(in srgb, var(--ok) 40%, var(--border));
  color: var(--ok);
}
.sk-prop-chip--warn {
  border-color: color-mix(in srgb, var(--warn) 40%, var(--border));
  color: var(--warn);
}
.sk-prop-chip--auto {
  border-style: dashed;
  color: var(--accent);
}
.sk-prop-hash {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-dim);
}

/* Auto-propose settings */
.sk-ap-settings {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-3) var(--sp-4) var(--sp-4);
}
.sk-ap-toggle {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-2);
  flex-wrap: wrap;
  cursor: pointer;
}
.sk-ap-toggle input[type="checkbox"] {
  margin-top: 2px;
  accent-color: var(--accent);
}
.sk-ap-toggle__label {
  font-weight: 600;
  font-size: var(--fs-sm);
  color: var(--text);
}
.sk-ap-toggle__hint {
  font-size: var(--fs-xs);
  color: var(--text-muted);
  width: 100%;
  margin-left: 44px;
}
.sk-ap-select {
  padding: 4px 8px;
  font-size: var(--fs-sm);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  outline: none;
}
.sk-ap-select:focus {
  border-color: var(--accent);
}

/* Registry */
.sk-registry {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.sk-registry__head,
.sk-github-install {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}
.sk-registry__results {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: var(--sp-4);
  min-height: 120px;
}
.sk-registry__hint {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: var(--sp-5);
  color: var(--text-muted);
  text-align: center;
}
.sk-registry__hint-icon {
  color: var(--text-dim);
}
.sk-registry__loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: var(--sp-5);
  color: var(--text-muted);
}
.sk-spinner {
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: sk-spin 0.8s linear infinite;
}
.sk-registry__name {
  font-weight: 600;
}
.sk-registry__desc {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  max-width: 300px;
}

/* Dialog */
.sk-dialog {
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  color: var(--text);
  max-width: 640px;
  width: 90vw;
  max-height: 85vh;
  overflow: hidden;
  padding: 0;
  margin: 0;
}
.sk-dialog::backdrop {
  background: var(--scrim);
}
.sk-detail {
  display: flex;
  flex-direction: column;
  max-height: 85vh;
}
.sk-detail__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-4);
  border-bottom: 1px solid var(--border);
}
.sk-detail__head-left {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
  min-width: 0;
}
.sk-detail__emoji {
  font-size: 18px;
  line-height: 1;
}
.sk-detail__name {
  font-size: var(--fs-lg);
  font-weight: 600;
}
.sk-detail__chips {
  display: flex;
  gap: 6px;
}
.sk-detail__body {
  padding: var(--sp-4);
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.sk-detail__desc {
  margin: 0;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.sk-detail__section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.sk-detail__section-title {
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--text-dim);
}
.sk-detail__sub-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.sk-detail__missing {
  margin: 0;
  padding-left: var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--text-muted);
}
.sk-detail__missing li {
  margin-bottom: 4px;
}
.sk-detail__declared {
  margin-top: 0;
}
.sk-detail__dependency-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
}
.sk-detail__dependency-stat {
  align-items: center;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 7px 4px;
  text-align: center;
}
.sk-detail__dependency-stat strong {
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
}
.sk-detail__dependency-stat span {
  color: var(--text-dim);
  font-size: 10px;
}
.sk-detail__dependency-stat.is-missing {
  border-color: color-mix(in srgb, var(--warn) 45%, var(--border));
}
.sk-detail__dependency-stat.is-missing strong {
  color: var(--warn);
}
.sk-detail__advisory-note {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: 0;
}
.sk-detail__install-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2);
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: var(--fs-sm);
}
.sk-detail__link {
  color: var(--accent);
  text-decoration: none;
  font-size: var(--fs-sm);
}
.sk-detail__link:hover {
  text-decoration: underline;
}
.sk-detail__content-state {
  padding: var(--sp-3);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.sk-detail__content-state--error {
  color: var(--danger);
  border-color: color-mix(in srgb, var(--danger) 35%, var(--border));
}
.sk-detail__content-state--warn {
  border-color: color-mix(in srgb, var(--warn) 35%, var(--border));
  color: var(--warn);
}
.sk-detail__foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
  border-top: 1px solid var(--border);
  flex-wrap: wrap;
}
.sk-detail__path {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.sk-iconbtn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  cursor: pointer;
  transition: color var(--transition), border-color var(--transition);
}
.sk-iconbtn:hover {
  color: var(--text);
  border-color: var(--border-focus);
}

/* Chips */
.sk-chip {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 10.5px;
  font-weight: 600;
  border: 1px solid var(--border);
  background: var(--bg-elevated);
  color: var(--text-muted);
}
.sk-chip--ok {
  border-color: color-mix(in srgb, var(--ok) 40%, var(--border));
  color: var(--ok);
}
.sk-chip--warn {
  border-color: color-mix(in srgb, var(--warn) 40%, var(--border));
  color: var(--warn);
}
.sk-chip--unverified {
  border-color: color-mix(in srgb, var(--text-dim) 40%, var(--border));
  color: var(--text-dim);
}
.sk-chip--sub {
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
  color: var(--accent);
}
.sk-chip--trigger {
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--bg);
}

/* Audit grid */
.sk-audit-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 16px;
  font-size: var(--fs-sm);
}
.sk-audit-grid__wide {
  grid-column: 1 / -1;
}
.sk-audit-grid span {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}
.sk-audit-grid strong {
  color: var(--text);
  font-weight: 600;
}
.sk-audit-grid code {
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--bg-elevated);
  padding: 1px 4px;
  border-radius: var(--radius-sm);
  margin-right: 4px;
}
.sk-audit-empty {
  font-size: var(--fs-sm);
  color: var(--text-muted);
  padding: var(--sp-2);
}

/* Preformatted */
.sk-detail__pre {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--sp-3);
  font-family: var(--font-mono);
  font-size: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  color: var(--text-muted);
}

/* Empty state */
.state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: var(--sp-5);
  color: var(--text-muted);
}
.state-icon {
  color: var(--text-dim);
}
.state-text {
  margin: 0;
  font-size: var(--fs-sm);
}
.state-text strong {
  color: var(--text);
}

/* Utility */
.sk-dim {
  color: var(--text-dim);
}
.sk-mono {
  font-family: var(--font-mono);
}

/* Animations */
@keyframes sk-fade-up {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes sk-spin {
  to { transform: rotate(360deg); }
}

/* Responsive */
@media (max-width: 720px) {
  .sk-stage__header {
    flex-direction: column;
    align-items: stretch;
  }
  .sk-stage__actions {
    width: 100%;
  }
  .sk-search-input,
  .sk-search-wrap--lg .sk-search-input {
    min-width: 0;
    width: 100%;
  }
  .sk-grid {
    grid-template-columns: 1fr;
  }
  .sk-detail__dependency-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .sk-proposal-row {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
