<template>
  <section ref="rootRef" tabindex="-1" class="composer-add-menu" :class="{ 'is-positioned': positioned }"
    role="dialog" :aria-label="t('chat.add')" @keydown="onKeydown"
    @compositionstart="composing = true" @compositionend="composing = false">
    <div class="composer-add-menu__search">
      <Icon name="search" :size="17" aria-hidden="true" />
      <input ref="searchRef" v-model="query" type="search" autocomplete="off"
        :aria-label="t('chat.composer.searchCapabilities')" :placeholder="t('chat.composer.searchCapabilities')" />
    </div>
    <div class="composer-add-menu__list" role="menu" :aria-label="t('chat.add')">
      <div v-if="workItems.length" class="composer-add-menu__group" role="group" :aria-label="t('chat.composer.workStyleGroup')">
        <div class="composer-add-menu__heading" aria-hidden="true">{{ t('chat.composer.workStyleGroup') }}</div>
        <ComposerCapabilityList :items="workItems" row-class="composer-add-menu__item" @choose="chooseAction" />
      </div>
      <div v-if="contentItems.length" class="composer-add-menu__group" role="group" :aria-label="t('chat.composer.contentGroup')">
        <div class="composer-add-menu__heading" aria-hidden="true">{{ t('chat.composer.contentGroup') }}</div>
        <ComposerCapabilityList :items="contentItems" row-class="composer-add-menu__item" @choose="chooseAction" />
      </div>
      <div v-if="pluginItems.length" class="composer-add-menu__group" role="group" :aria-label="t('chat.composer.pluginsGroup')">
        <div class="composer-add-menu__heading" aria-hidden="true">{{ t('chat.composer.pluginsGroup') }}</div>
        <ComposerCapabilityList :items="pluginItems" row-class="composer-add-menu__item" @choose="choosePlugin" />
      </div>
      <div v-if="skillItems.length || skillsLoading || skillsError" class="composer-add-menu__group" role="group" :aria-label="t('chat.skillPalette.skill')">
        <div class="composer-add-menu__heading" aria-hidden="true">{{ t('chat.skillPalette.skill') }}</div>
        <ComposerCapabilityList :items="skillItems" row-class="composer-add-menu__item" @choose="chooseSkill" />
        <p v-if="skillsLoading" class="composer-add-menu__message" role="status">{{ t('chat.skillPalette.loading') }}</p>
        <div v-if="skillsError" class="composer-add-menu__message" role="status">
          <span>{{ skillsError }}</span><button type="button" class="composer-add-menu__retry" @click="emit('retrySkills')">{{ t('chat.retry') }}</button>
        </div>
      </div>
      <div v-if="utilityItems.length" class="composer-add-menu__group" role="group" :aria-label="t('chrome.more')">
        <div class="composer-add-menu__heading" aria-hidden="true">{{ t('chrome.more') }}</div>
        <ComposerCapabilityList :items="utilityItems" row-class="composer-add-menu__item" @choose="chooseUtility" />
      </div>
      <p v-if="!contentItems.length && !pluginItems.length && !skillItems.length && !workItems.length && !utilityItems.length && !skillsLoading && !skillsError"
        class="composer-add-menu__message" role="status">{{ t('chat.skillPalette.empty') }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ComposerCapabilityList, { composerCapabilityIcon, type ComposerCapabilityItem } from './ComposerCapabilityList.vue'
import type { ChatSlashCommand } from '@/composables/chat/useChatSlashCommands'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import type { PromptCacheKeepaliveStatus } from '@/types/promptCacheKeepalive'
import { resolveComposerAddMenuPlacement } from '@/utils/chat/composerAddMenuPlacement'
import { slashSearchRank } from '@/utils/chat/slashPalette'

const props = defineProps<{
  avoidElement?: HTMLElement | null
  composerElement?: HTMLElement | null
  attachmentsDisabled?: boolean
  browserUseAvailable?: boolean
  localPathsAvailable?: boolean
  localPathsDisabled?: boolean
  goalModeActive: boolean
  goalModeAvailable: boolean
  goalModeBusy: boolean
  goalModeExisting: boolean
  planModeActive: boolean
  planModeAvailable: boolean
  planModeBusy: boolean
  showUtilityActions?: boolean
  voiceBusy?: boolean
  voiceReady?: boolean
  voiceRecording?: boolean
  promptCacheKeepaliveAvailable?: boolean
  promptCacheKeepaliveSessionReady?: boolean
  promptCacheKeepaliveStatus?: PromptCacheKeepaliveStatus | null
  skills?: ChatSlashCommand[]
  skillsLoading?: boolean
  skillsError?: string
  selectedSkills?: readonly SelectedSkillRef[]
}>()
const emit = defineEmits<{
  activateGoalMode: []
  activatePlanMode: []
  attachFiles: []
  selectBrowserUse: []
  referenceLocalPaths: []
  chooseSkill: [command: ChatSlashCommand]
  retrySkills: []
  voiceInput: []
  voiceSetup: []
  exportMarkdown: []
  openPromptCacheKeepalive: []
  refreshPromptCacheKeepalive: []
  close: []
}>()
const { t } = useI18n()
const rootRef = ref<HTMLElement | null>(null)
const searchRef = ref<HTMLInputElement | null>(null)
const positioned = ref(false)
const query = ref('')
const composing = ref(false)
let placementFrame: number | null = null
let currentLift = 0
let currentMaxHeight: number | null = null

function matching(items: ComposerCapabilityItem[]) {
  const needle = query.value.trim().toLocaleLowerCase()
  return needle ? items.filter(item => `${item.label} ${item.description || ''}`.toLocaleLowerCase().includes(needle)) : items
}
const contentItems = computed(() => matching([
  { key: 'attach', label: t('chat.attachFiles'), icon: 'paperclip', disabled: props.attachmentsDisabled },
  ...(props.localPathsAvailable ? [{ key: 'local', label: t('chat.referenceLocalPaths'), description: t('chat.referenceLocalPathsHint'),
    title: t('chat.referenceLocalPathsHint'), icon: 'folder', disabled: props.localPathsDisabled }] : []),
] as ComposerCapabilityItem[]))
const workItems = computed(() => matching([
  ...(props.planModeAvailable ? [{ key: 'plan', label: t('chat.planMode.label'), description: t(props.planModeActive ? 'chat.planMode.readOnly' : 'chat.planMode.turnOn'),
    icon: 'listChecks', disabled: props.planModeBusy || props.planModeActive, pressed: props.planModeActive }] : []),
  ...(props.goalModeAvailable ? [{ key: 'goal', label: t('chat.goal.modeLabel'),
    description: t(props.goalModeActive ? 'chat.goal.modeReady' : props.goalModeExisting ? 'chat.goal.activeTitle' : 'chat.goal.modeDescription'),
    icon: 'target', disabled: props.goalModeBusy || props.goalModeActive || props.goalModeExisting, pressed: props.goalModeActive }] : []),
] as ComposerCapabilityItem[]))
const pluginNames = new Set(['browser-use', 'visualize'])
function normalizedSkillName(command: ChatSlashCommand) {
  return (command.skill?.name || command.name).replace(/^\//, '').split(':').pop() || ''
}
function skillItem(command: ChatSlashCommand): ComposerCapabilityItem {
  const skill = command.skill
  let status = ''
  if (skill?.disabled) status = t('chat.skillPalette.disabled')
  else if (skill?.reasonCode === 'tools_unavailable') status = t('chat.skillPalette.toolsUnavailable')
  else if (skill && !skill.ready) status = t('chat.skillPalette.needsSetup')
  else if (skill?.manualOnly) status = t('chat.skillPalette.manualOnly')
  return { key: skill?.instanceId || command.cmd, label: command.label, description: command.desc,
    icon: composerCapabilityIcon(skill?.name || command.name), skillName: skill?.name, title: skill?.reason, status,
    selected: Boolean(skill && props.selectedSkills?.some(selected => selected.instanceId === skill.instanceId && selected.digest === skill.digest)) }
}
const pluginItems = computed<ComposerCapabilityItem[]>(() => {
  const items = (props.skills || []).filter(command => pluginNames.has(normalizedSkillName(command))).map(skillItem)
  if (props.browserUseAvailable && !items.some(item => item.skillName === 'browser-use')) {
    items.unshift({ key: 'browser', label: t('chat.composer.browserUse'), description: t('chat.composer.browserUseDescription'), icon: 'languages', badge: '[BETA]', skillName: 'browser-use' })
  }
  return items.filter((item, index) => {
    const command = (props.skills || []).find(candidate => (candidate.skill?.instanceId || candidate.cmd) === item.key)
    return slashSearchRank(query.value, [item.label, item.skillName || item.key, ...(command?.aliases || [])], [item.description || '', ...(command?.searchDescriptions || [])]) >= 0 || (index === 0 && item.key === 'browser' && !query.value.trim())
  })
})
const promptCacheKeepaliveStatusText = computed(() => {
  const status = props.promptCacheKeepaliveStatus
  if (!status || status.state === 'off') return ''
  return t(`chat.promptCacheKeepalive.states.${status.state}`)
})
const utilityItems = computed(() => {
  if (!props.showUtilityActions) return []
  return matching([
    {
      key: 'voice',
      label: props.voiceReady ? t('chat.recordVoice') : t('chat.voiceUnavailableHint'),
      icon: 'microphone',
      title: props.voiceReady ? t('chat.recordVoice') : t('chat.voiceUnavailableHint'),
      ariaLabel: props.voiceReady ? t('chat.recordVoice') : t('chat.voiceUnavailableHint'),
      className: props.voiceReady ? undefined : 'chat-mic--needs-setup',
      disabled: props.voiceBusy,
      active: props.voiceRecording,
    },
    { key: 'export', label: t('chat.exportMarkdown'), icon: 'download', ariaLabel: t('chat.exportMarkdown') },
    ...(props.promptCacheKeepaliveAvailable ? [{
      key: 'keepalive',
      label: t('chat.promptCacheKeepalive.action'),
      description: props.promptCacheKeepaliveSessionReady ? undefined : t('chat.promptCacheKeepalive.unavailableHint'),
      status: promptCacheKeepaliveStatusText.value,
      icon: 'clock',
      title: props.promptCacheKeepaliveSessionReady
        ? t('chat.promptCacheKeepalive.action')
        : t('chat.promptCacheKeepalive.unavailableHint'),
      ariaLabel: promptCacheKeepaliveStatusText.value
        ? `${t('chat.promptCacheKeepalive.action')}. ${promptCacheKeepaliveStatusText.value}`
        : t('chat.promptCacheKeepalive.action'),
      testId: 'chat-composer-action-keepalive',
      disabled: !props.promptCacheKeepaliveSessionReady,
    }] : []),
  ] as ComposerCapabilityItem[])
})
const skillItems = computed<ComposerCapabilityItem[]>(() => (props.skills || [])
  .filter(command => !pluginNames.has(normalizedSkillName(command)))
  .map((command, index) => ({ command, index, rank: slashSearchRank(query.value,
    [command.name, command.cmd, command.label, ...command.aliases], [command.desc, ...(command.searchDescriptions || [])]) }))
  .filter(item => item.rank >= 0).sort((left, right) => left.rank - right.rank || left.index - right.index)
  .map(({ command }) => skillItem(command)))

function chooseAction(item: ComposerCapabilityItem) {
  if (item.disabled) return
  if (item.key === 'attach') emit('attachFiles')
  else if (item.key === 'local') emit('referenceLocalPaths')
  else if (item.key === 'browser') emit('selectBrowserUse')
  else if (item.key === 'plan') emit('activatePlanMode')
  else if (item.key === 'goal') emit('activateGoalMode')
  emit('close')
}
function choosePlugin(item: ComposerCapabilityItem) {
  if (item.disabled) return
  if (item.key === 'browser') {
    emit('selectBrowserUse')
    emit('close')
  } else chooseSkill(item)
}
function chooseUtility(item: ComposerCapabilityItem) {
  if (item.disabled) return
  if (item.key === 'voice') {
    if (props.voiceReady) emit('voiceInput')
    else emit('voiceSetup')
  } else if (item.key === 'export') {
    emit('exportMarkdown')
  } else if (item.key === 'keepalive') {
    if (!props.promptCacheKeepaliveSessionReady) return
    emit('openPromptCacheKeepalive')
  }
  emit('close')
}
function chooseSkill(item: ComposerCapabilityItem) {
  const command = props.skills?.find(candidate => (candidate.skill?.instanceId || candidate.cmd) === item.key)
  if (!command) return
  emit('chooseSkill', command)
  emit('close')
}
function onKeydown(event: KeyboardEvent) {
  // IME candidate confirmation must never choose a menu action or close it.
  if (composing.value || event.isComposing || event.keyCode === 229) {
    if (event.key === 'Enter' && event.target instanceof HTMLButtonElement) event.preventDefault()
    event.stopPropagation()
    return
  }
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); emit('close'); return }
  const rows = [...(rootRef.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') || [])]
  const active = rows.indexOf(document.activeElement as HTMLButtonElement)
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault(); event.stopPropagation()
    const next = active < 0 ? (event.key === 'ArrowDown' ? 0 : rows.length - 1)
      : (active + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length
    rows[next]?.focus()
    rows[next]?.scrollIntoView?.({ block: 'nearest' })
  } else if (event.key === 'Enter' && event.target === searchRef.value) {
    event.preventDefault(); event.stopPropagation(); rows[0]?.click()
  }
  // Tab and Shift+Tab retain normal focus traversal through search and actions.
}
function updatePlacement() {
  const root = rootRef.value
  if (!root) return
  const boundaryTop = props.avoidElement?.getBoundingClientRect().top ?? null
  const composerTop = props.composerElement?.getBoundingClientRect().top
  const viewportTop = window.visualViewport?.offsetTop || 0
  const available = composerTop === undefined ? null : Math.max(0, Math.floor(Math.min(
    400, (window.visualViewport?.height || window.innerHeight) * 0.55, composerTop - viewportTop - 16,
  )))
  let maxHeight = available
  if (boundaryTop === null) {
    if (currentLift) { currentLift = 0; root.style.removeProperty('--composer-add-menu-lift') }
  } else {
    const placement = resolveComposerAddMenuPlacement({ menuBottom: root.getBoundingClientRect().bottom, currentLift, boundaryTop })
    if (placement.lift !== currentLift) { currentLift = placement.lift; root.style.setProperty('--composer-add-menu-lift', `${placement.lift}px`) }
    maxHeight = available === null ? placement.maxHeight : Math.min(available, placement.maxHeight)
  }
  if (maxHeight !== currentMaxHeight) {
    currentMaxHeight = maxHeight
    if (maxHeight === null) root.style.removeProperty('max-height')
    else root.style.setProperty('max-height', `${maxHeight}px`)
  }
  positioned.value = true
}
function trackPlacement() { updatePlacement(); placementFrame = window.requestAnimationFrame(trackPlacement) }
onMounted(async () => {
  updatePlacement()
  await nextTick()
  // Keep touch keyboards closed until the user chooses to search.
  if (!window.matchMedia?.('(pointer: coarse)').matches) searchRef.value?.focus({ preventScroll: true })
  if (typeof window.requestAnimationFrame === 'function') placementFrame = window.requestAnimationFrame(trackPlacement)
  if (props.promptCacheKeepaliveAvailable && props.promptCacheKeepaliveSessionReady) {
    emit('refreshPromptCacheKeepalive')
  }
})
onBeforeUnmount(() => { if (placementFrame !== null) window.cancelAnimationFrame(placementFrame) })
</script>

<style scoped>
.composer-add-menu {
  position: absolute;
  bottom: calc(100% + 8px + var(--composer-add-menu-lift, 0px));
  left: 0;
  z-index: 30;
  display: flex;
  flex-direction: column;
  width: 100%;
  max-height: min(400px, 55dvh);
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-md);
  overflow: hidden;
  visibility: hidden;
  container: composer-capabilities / inline-size;
}
.composer-add-menu.is-positioned { visibility: visible; }
.composer-add-menu__search { display: flex; align-items: center; gap: 9px; flex: 0 0 auto; padding: 9px 14px; color: var(--text-muted); }
.composer-add-menu .composer-add-menu__search input,
.composer-add-menu .composer-add-menu__search input:focus {
  min-width: 0; flex: 1; width: 100%; border: 0; padding: 4px 0; background: transparent;
  color: var(--text); font: inherit; font-size: var(--fs-sm); box-shadow: none; outline: none;
}
.composer-add-menu__list { min-height: 0; padding: 5px; overflow-y: auto; overscroll-behavior: contain; }
.composer-add-menu__group + .composer-add-menu__group { margin-top: 5px; }
.composer-add-menu__heading { padding: 5px 10px 3px; color: var(--text-muted); font-size: var(--fs-xs); font-weight: 500; }
.composer-add-menu__message { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 0; padding: 8px 10px; color: var(--text-muted); font-size: var(--fs-xs); }
.composer-add-menu__retry { border: 0; border-radius: var(--radius-sm); background: var(--bg-hover); color: var(--text); padding: 6px 10px; cursor: pointer; font: inherit; }
.composer-capability.chat-mic--needs-setup { opacity: var(--state-disabled-opacity); }
.composer-capability.chat-mic--needs-setup:hover { opacity: 1; }
@media (pointer: coarse) { .composer-add-menu__retry { min-height: 44px; } }
</style>
