<template>
  <div :id="paletteId || 'chat-mention-options'" ref="rootRef" class="chat-slash" :style="availableHeight === null ? undefined : { maxHeight: `${availableHeight}px` }"
    role="listbox" aria-label="mentions" @keydown="onKeydown">
    <div class="chat-slash-list">
      <div v-for="group in groups" :key="group.key" role="group" :aria-label="t(`chat.mention.groups.${group.kind}`)">
        <div class="chat-slash-group" aria-hidden="true">{{ t(`chat.mention.groups.${group.kind}`) }}</div>
        <ComposerCapabilityList :items="group.items" role="option" row-class="chat-slash-item" preserve-editor-focus @choose="choose" />
      </div>
      <div v-if="!items.length && !loading && !error" class="chat-slash-empty">{{ t('chat.skillPalette.empty') }}</div>
      <div v-if="loading" class="chat-slash-empty" role="status">{{ t('chat.skillPalette.loading') }}</div>
      <div v-if="error" class="chat-slash-empty" role="status">{{ error }}</div>
    </div>
    <div class="chat-slash-hint">{{ t('chat.mention.hint') }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { MentionCandidate } from '@/composables/chat/useChatMentions'
import type { IconName } from '@/utils/icons'
import ComposerCapabilityList, { type ComposerCapabilityItem } from './ComposerCapabilityList.vue'

const props = defineProps<{ items: MentionCandidate[]; activeIndex: number; loading: boolean; error: string; paletteId?: string;
  composerElement?: HTMLElement | null }>()
const emit = defineEmits<{ choose: [candidate: MentionCandidate]; close: [] }>()
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
// A mention is either a workspace entry (directory or file) or a recent chat.
// Read the candidate fields defensively so the palette keeps rendering whatever
// extra metadata the mention source attaches to a row.
type MentionRecord = { kind?: string; label?: string; name?: string; path?: string; title?: string; description?: string; detail?: string; subtitle?: string; id?: string }
const MENTION_ICONS: Record<string, IconName> = { workspace_directory: 'folder', workspace_file: 'fileText', session: 'chat' }
function record(candidate: MentionCandidate): MentionRecord {
  return (candidate || {}) as unknown as MentionRecord
}
function candidateLabel(candidate: MentionCandidate): string {
  const value = record(candidate).label ?? record(candidate).name ?? record(candidate).path ?? record(candidate).title ?? record(candidate).id
  return typeof value === 'string' ? value : ''
}
function candidateDescription(candidate: MentionCandidate): string | undefined {
  const value = record(candidate).description ?? record(candidate).detail ?? record(candidate).subtitle
  return typeof value === 'string' && value ? value : undefined
}
function candidateIcon(candidate: MentionCandidate): IconName {
  return MENTION_ICONS[record(candidate).kind || ''] || 'fileText'
}
const groups = computed(() => {
  const workspace: ComposerCapabilityItem[] = []
  const sessions: ComposerCapabilityItem[] = []
  props.items.forEach((candidate, index) => {
    const row: ComposerCapabilityItem = { key: String(index), id: `${props.paletteId || 'chat-mention'}-option-${index}`,
      label: candidateLabel(candidate), description: candidateDescription(candidate), icon: candidateIcon(candidate),
      active: index === props.activeIndex }
    if (record(candidate).kind === 'session') sessions.push(row)
    else workspace.push(row)
  })
  // Fixed group order: workspace first, then chats; empty groups never render.
  return [{ key: 'workspace', kind: 'workspace', items: workspace }, { key: 'sessions', kind: 'sessions', items: sessions }]
    .filter(group => group.items.length > 0)
})
function choose(item: ComposerCapabilityItem) {
  const candidate = props.items[Number(item.key)]
  if (candidate) emit('choose', candidate)
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
