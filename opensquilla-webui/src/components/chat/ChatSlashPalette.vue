<template>
  <div id="chat-slash-options" ref="rootRef" class="chat-slash" :style="availableHeight === null ? undefined : { maxHeight: `${availableHeight}px` }"
    role="listbox" :aria-label="t('chat.skillPalette.title')" @keydown="onKeydown">
    <div class="chat-slash-list">
      <div v-for="group in groups" :key="group.key" role="group" :aria-label="t(`chat.skillPalette.${group.kind}`)">
        <div class="chat-slash-group" aria-hidden="true">{{ t(`chat.skillPalette.${group.kind}`) }}</div>
        <ComposerCapabilityList :items="group.items" role="option" row-class="chat-slash-item" preserve-editor-focus @choose="choose" />
      </div>
      <div v-if="!items.length && !loading && !error" class="chat-slash-empty">{{ t('chat.skillPalette.empty') }}</div>
      <div v-if="loading" class="chat-slash-empty" role="status">{{ t('chat.skillPalette.loading') }}</div>
      <div v-if="error" class="chat-slash-empty" role="status">{{ error }}</div>
    </div>
    <div class="chat-slash-hint">{{ t('chat.skillPalette.autoHint') }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatSlashCommand } from '@/composables/chat/useChatSlashCommands'
import type { SelectedSkillRef } from '@/types/selectedSkills'
import ComposerCapabilityList, { composerCapabilityIcon, type ComposerCapabilityItem } from './ComposerCapabilityList.vue'

const props = defineProps<{ items: ChatSlashCommand[]; activeIndex: number; loading: boolean; error: string;
  selectedSkills?: readonly SelectedSkillRef[]; composerElement?: HTMLElement | null }>()
const emit = defineEmits<{ choose: [command: ChatSlashCommand]; close: [] }>()
const { t } = useI18n()
const rootRef = ref<HTMLElement | null>(null)
const availableHeight = ref<number | null>(null)
let resizeObserver: ResizeObserver | null = null
function updatePlacement() {
  const top = props.composerElement?.getBoundingClientRect().top
  availableHeight.value = top === undefined ? null : Math.max(0, Math.floor(Math.min(
    400, (window.visualViewport?.height || window.innerHeight) * 0.5,
    top - (window.visualViewport?.offsetTop || 0) - 16,
  )))
}
watch(() => props.composerElement, (element, previous) => {
  if (previous) resizeObserver?.unobserve(previous)
  if (element) resizeObserver?.observe(element)
  updatePlacement()
})
onMounted(() => {
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(updatePlacement)
    if (props.composerElement) resizeObserver.observe(props.composerElement)
  }
  window.addEventListener('resize', updatePlacement)
  window.visualViewport?.addEventListener('resize', updatePlacement)
  window.visualViewport?.addEventListener('scroll', updatePlacement)
  updatePlacement()
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  window.removeEventListener('resize', updatePlacement)
  window.visualViewport?.removeEventListener('resize', updatePlacement)
  window.visualViewport?.removeEventListener('scroll', updatePlacement)
})
const groups = computed(() => {
  const result: { key: string; kind: string; items: ComposerCapabilityItem[] }[] = []
  props.items.forEach((command, index) => {
    const kind = command.kind || 'command'
    if (result[result.length - 1]?.kind !== kind) result.push({ key: `${kind}:${index}`, kind, items: [] })
    const skill = command.skill
    result[result.length - 1]!.items.push({ key: String(index), id: `chat-slash-option-${index}`, label: kind === 'command' ? command.cmd : command.label,
      description: command.desc, icon: kind === 'command' ? 'terminal' : composerCapabilityIcon(skill?.name || command.name), command: kind === 'command',
      status: status(command), title: skill?.reason, skillName: skill?.name, active: index === props.activeIndex,
      selected: Boolean(skill && props.selectedSkills?.some(selected => selected.instanceId === skill.instanceId && selected.digest === skill.digest)) })
  })
  return result
})
function choose(item: ComposerCapabilityItem) {
  const command = props.items[Number(item.key)]
  if (command) emit('choose', command)
}
function status(command: ChatSlashCommand): string {
  const skill = command.skill
  if (skill?.disabled) return t('chat.skillPalette.disabled')
  if (skill?.reasonCode === 'tools_unavailable') return t('chat.skillPalette.toolsUnavailable')
  if (skill && !skill.ready) return t('chat.skillPalette.needsSetup')
  if (skill?.manualOnly) return t('chat.skillPalette.manualOnly')
  return ''
}
function onKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) {
    if (event.key === 'Enter') event.preventDefault()
    event.stopPropagation()
    return
  }
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); emit('close'); return }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
    const rows = [...(rootRef.value?.querySelectorAll<HTMLButtonElement>('[role="option"]') || [])]
    const active = rows.indexOf(document.activeElement as HTMLButtonElement)
    if (active < 0 || !rows.length) return
    event.preventDefault(); event.stopPropagation()
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? rows.length - 1
      : (active + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length
    rows[next]?.focus()
    rows[next]?.scrollIntoView?.({ block: 'nearest' })
  } else if (event.key === 'Enter' || event.key === ' ') event.stopPropagation()
}
</script>

<style scoped>
.chat-slash {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  right: 0;
  display: flex;
  flex-direction: column;
  width: 100%;
  max-height: min(400px, 50dvh);
  box-sizing: border-box;
  overflow: hidden;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
  z-index: 30;
  container: composer-capabilities / inline-size;
}
.chat-slash-list { min-height: 0; padding: 5px; overflow-y: auto; overscroll-behavior: contain; }
.chat-slash-group { padding: 5px 10px 3px; color: var(--text-muted); font-size: var(--fs-xs); font-weight: 500; line-height: 1.4; }
.chat-slash-list > [role="group"] + [role="group"] { margin-top: 5px; }
.chat-slash-empty, .chat-slash-hint { padding: 8px 10px; color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.5; }
.chat-slash-hint { flex-shrink: 0; border-top: 1px solid var(--border); padding-inline: 15px; }
.chat-slash:has(button:focus-visible) :deep(.composer-capability.is-active:not(:focus-visible)) { background: transparent; }
</style>
