<template>
  <div class="chat-slash" role="listbox" :aria-label="t('chat.skillPalette.title')">
    <div class="chat-slash-list">
      <template v-for="(cmd, i) in items" :key="`${cmd.kind || 'command'}:${cmd.cmd}`">
        <div v-if="i === 0 || cmd.kind !== items[i - 1]?.kind" class="chat-slash-group">
          {{ t(`chat.skillPalette.${cmd.kind || 'command'}`) }}
          <span class="chat-slash-count">{{ groupSize(i) }}</span>
        </div>
        <button
          type="button"
          role="option"
          class="chat-slash-item"
          :class="{ 'chat-slash-item--active': i === activeIndex }"
          :aria-selected="i === activeIndex"
          :data-skill-name="cmd.skill?.name"
          @mousedown.prevent
          @click="emit('choose', cmd)"
        >
          <span class="chat-slash-name" :class="{ 'chat-slash-name--command': !cmd.kind || cmd.kind === 'command' }">
            {{ cmd.kind === 'command' || !cmd.kind ? cmd.cmd : cmd.label }}
          </span>
          <span class="chat-slash-desc">{{ cmd.desc }}</span>
          <span v-if="status(cmd)" class="chat-slash-status" :title="cmd.skill?.reason">{{ status(cmd) }}</span>
        </button>
      </template>
      <div v-if="!items.length && !loading" class="chat-slash-empty">{{ t('chat.skillPalette.empty') }}</div>
      <div v-if="loading" class="chat-slash-empty" role="status">{{ t('chat.skillPalette.loading') }}</div>
      <div v-if="error" class="chat-slash-empty" role="status">{{ error }}</div>
    </div>
    <div class="chat-slash-hint">{{ t('chat.skillPalette.autoHint') }}</div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { ChatSlashCommand } from '@/composables/chat/useChatSlashCommands'

const props = defineProps<{ items: ChatSlashCommand[]; activeIndex: number; loading: boolean; error: string }>()
const emit = defineEmits<{ choose: [command: ChatSlashCommand] }>()
const { t } = useI18n()

function groupSize(start: number): number {
  const kind = props.items[start]?.kind || 'command'
  let end = start + 1
  while (end < props.items.length && (props.items[end]?.kind || 'command') === kind) end += 1
  return end - start
}

function status(command: ChatSlashCommand): string {
  const skill = command.skill
  if (skill?.disabled) return t('chat.skillPalette.disabled')
  if (skill && !skill.ready) return t('chat.skillPalette.needsSetup')
  if (skill?.manualOnly) return t('chat.skillPalette.manualOnly')
  return ''
}
</script>

<style scoped>
.chat-slash {
  position: absolute;
  bottom: calc(100% + 0.5rem);
  left: 0;
  right: 0;
  display: flex;
  flex-direction: column;
  width: min(calc(100% - 2rem), var(--composer-col));
  max-height: min(360px, 45vh);
  margin-inline: auto;
  overflow: hidden;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  z-index: 30;
}

.chat-slash-list {
  min-height: 0;
  padding: 0.25rem;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.chat-slash-group {
  display: flex;
  align-items: baseline;
  gap: 0.375rem;
  padding: 0.25rem 0.625rem 0.125rem;
  color: var(--text-muted);
  font-size: 0.6875rem;
  font-weight: 600;
  line-height: 1.4;
}

.chat-slash-count {
  font-weight: 400;
  font-variant-numeric: tabular-nums;
}

.chat-slash-item {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  width: 100%;
  min-height: 2rem;
  padding: 0.375rem 0.625rem;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: 0.8125rem;
  line-height: 1.4;
  text-align: start;
  cursor: pointer;
}

.chat-slash-item:hover,
.chat-slash-item--active {
  background: var(--bg-hover);
}

.chat-slash-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.chat-slash-name {
  flex: 0 0 26%;
  min-width: 0;
  max-width: 11rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 600;
}

.chat-slash-name--command {
  color: var(--accent);
  font-family: var(--font-mono);
}

.chat-slash-desc {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--text-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-slash-status {
  flex-shrink: 0;
  max-width: 6rem;
  overflow: hidden;
  color: var(--warn);
  font-size: 0.6875rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-slash-empty,
.chat-slash-hint {
  padding: 0.375rem 0.625rem;
  color: var(--text-muted);
  font-size: 0.6875rem;
  line-height: 1.5;
}

.chat-slash-hint {
  flex-shrink: 0;
  border-top: 1px solid var(--border);
  padding-inline: 1rem;
}

@media (max-width: 600px) {
  .chat-slash-item {
    gap: 0.5rem;
  }

  .chat-slash-name {
    flex-basis: 34%;
  }
}
</style>
