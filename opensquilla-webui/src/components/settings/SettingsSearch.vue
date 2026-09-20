<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { SETTINGS_SECTIONS, type SettingsRailSectionId } from '@/composables/setup/settingsSections'

const props = defineProps<{ disabled?: boolean; isDesktop: boolean }>()
const emit = defineEmits<{ select: [section: SettingsRailSectionId, labelKey: string] }>()
const { t } = useI18n()
const query = ref('')
const root = ref<HTMLElement | null>(null)
const input = ref<HTMLInputElement | null>(null)
const focused = ref(false)

// Reuse existing setting labels. This index is local, small, and does not mount
// hidden panels or ask the Gateway to load configuration for a search.
const labels: Partial<Record<SettingsRailSectionId, string[]>> = {
  gateway: ['setup.connection.wsUrlLabel', 'setup.connection.tokenLabel'],
  provider: ['setup.provider.defaultModelLabel', 'settings.search.defaultModel'],
  modelStrategy: ['setup.modelStrategy.singleModelLabel', 'setup.modelStrategy.routerTitle', 'setup.modelStrategy.ensembleTitle'],
  capabilities: ['setup.search.title', 'setup.memory.title', 'setup.image.title', 'setup.audio.title'],
  general: ['settings.appearance.languageLabel', 'setup.behavior.autoTitlesLabel'],
  interface: ['settings.appearance.themeLabel', 'settings.appearance.sidebarWidthLabel', 'settings.appearance.toolDetailsLabel', 'settings.appearance.visualEffectsLabel', 'settings.appearance.composerFxLabel', 'settings.appearance.bgmLabel'],
  securityPrivacy: ['settings.sandbox.title', 'settings.sandbox.mode.title', 'settings.search.permissions', 'setup.privacy.networkReportingLabel'],
  memory: ['settings.memoryOverview.title'],
  advanced: ['settings.memoryOverview.autoCaptureLabel', 'setup.advanced.configFileLabel', 'setup.advanced.dataMaintenanceLabel', 'setup.advanced.agentConfigLabel'],
}
const results = computed(() => {
  const terms = query.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return []
  return SETTINGS_SECTIONS.filter(section => !section.desktopOnly || props.isDesktop).flatMap(section => {
    const title = t(`settings.rail.${section.id}`)
    const keys = section.id === 'gateway' && props.isDesktop
      ? ['setup.runtime.title']
      : labels[section.id] ?? []
    const settingLabels = keys.map(key => ({ key, label: t(key) }))
    const haystack = [title, section.label, ...settingLabels.map(item => item.label)].join(' ').toLocaleLowerCase()
    if (!terms.every(term => haystack.includes(term))) return []
    const match = settingLabels.find(item => terms.every(term => item.label.toLocaleLowerCase().includes(term)))
    return [{ ...section, title, match: match?.label ?? '', matchKey: match?.key ?? '' }]
  })
})
const showResults = computed(() => focused.value && Boolean(query.value.trim()))

function select(result: (typeof results.value)[number]) {
  query.value = ''
  focused.value = false
  emit('select', result.id, result.matchKey)
}
function clearSearch(event: KeyboardEvent) {
  if (!query.value) return
  event.preventDefault()
  event.stopPropagation()
  query.value = ''
  input.value?.focus()
}
function moveResult(event: KeyboardEvent, direction: 1 | -1) {
  const buttons = Array.from(root.value?.querySelectorAll<HTMLButtonElement>('.settings-search__result') ?? [])
  if (!buttons.length) return
  event.preventDefault()
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement)
  const index = current < 0
    ? (direction > 0 ? 0 : buttons.length - 1)
    : (current + direction + buttons.length) % buttons.length
  buttons[index]?.focus()
}
function onFocusOut(event: FocusEvent) {
  // During a pointer focus transition activeElement can still be body. Use
  // the destination so the result survives until its click is delivered.
  if (!root.value?.contains(event.relatedTarget as Node | null)) focused.value = false
}
</script>

<template>
  <div ref="root" class="settings-search" @focusin="focused = true" @focusout="onFocusOut" @keydown.esc="clearSearch" @keydown.down="moveResult($event, 1)" @keydown.up="moveResult($event, -1)">
    <Icon name="search" :size="15" aria-hidden="true" />
    <input ref="input" v-model="query" type="search" :disabled="disabled" :aria-label="t('settings.search.label')" :placeholder="t('settings.search.label')" autocomplete="off" @keydown.enter.prevent="results[0] && select(results[0])">
    <div v-if="showResults" role="group" class="settings-search__results" :aria-label="t('settings.search.results')">
      <button v-for="result in results" :key="result.id" type="button" class="settings-search__result" @keydown.enter.stop @click="select(result)">
        <Icon :name="result.icon" :size="16" aria-hidden="true" />
        <span><strong>{{ result.title }}</strong><small v-if="result.match">{{ result.match }}</small></span>
      </button>
      <p v-if="!results.length" class="settings-search__empty" role="status">{{ t('settings.search.empty') }}</p>
    </div>
  </div>
</template>

<style scoped>
.settings-search { align-items: center; display: flex; flex: 1 1 220px; gap: 8px; max-width: 360px; min-width: 0; position: relative; }
.settings-search > .icon { left: 10px; pointer-events: none; position: absolute; color: var(--text-dim); }
.settings-search input[type="search"] { background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius-md); color: var(--text); font: inherit; font-size: var(--fs-sm); min-height: 40px; padding: 8px 10px 8px 32px; width: 100%; }
.settings-search input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.settings-search__results { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-md); box-shadow: var(--shadow-lg); left: 0; max-height: min(45vh, 340px); overflow-y: auto; padding: 4px; position: absolute; right: 0; top: calc(100% + 6px); z-index: 5; }
.settings-search__result { align-items: center; background: transparent; border: 0; border-radius: var(--radius-sm); color: var(--text); cursor: pointer; display: flex; font: inherit; gap: 10px; min-height: 44px; padding: 8px; text-align: left; width: 100%; }
.settings-search__result:hover, .settings-search__result:focus-visible { background: var(--bg-hover); outline: 2px solid var(--accent); outline-offset: -2px; }
.settings-search__result span { display: flex; flex-direction: column; min-width: 0; overflow-wrap: anywhere; }
.settings-search__result strong { font-size: var(--fs-sm); font-weight: 500; }
.settings-search__result small, .settings-search__empty { color: var(--text-dim); font-size: var(--fs-xs); }
.settings-search__empty { margin: 8px; }
@media (max-width: 560px) { .settings-search { flex-basis: 100%; max-width: none; order: 3; } }
</style>
