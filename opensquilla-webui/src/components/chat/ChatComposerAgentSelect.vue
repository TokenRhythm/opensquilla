<template>
  <section
    ref="rootRef"
    tabindex="-1"
    class="composer-agent-select"
    role="dialog"
    :aria-label="t('chat.composer.agentSelect')"
    @keydown.esc.stop="$emit('close')"
  >
    <div class="composer-agent-select__head">
      <span>{{ t('chat.composer.agentSelect') }}</span>
      <button type="button" class="composer-agent-select__close" :aria-label="t('chat.closeComposerSettings')" @click="$emit('close')">
        <Icon name="x" :size="14" />
      </button>
    </div>

    <p class="composer-agent-select__hint">{{ t('chat.composer.agentSelectHint') }}</p>

    <div class="composer-agent-select__list" role="radiogroup" :aria-label="t('chat.composer.agentSelect')">
      <button
        v-for="option in options"
        :key="option.id"
        type="button"
        class="composer-agent-select__option"
        :class="{ 'is-active': selectedAgentId === option.id }"
        role="radio"
        :aria-checked="selectedAgentId === option.id ? 'true' : 'false'"
        @click="selectAgent(option.id)"
      >
        <span class="composer-agent-select__option-main">
          <Icon name="agents" :size="14" />
          <span class="composer-agent-select__option-label">{{ option.name }}</span>
          <Icon v-if="selectedAgentId === option.id" name="check" :size="14" />
        </span>
        <span v-if="option.model" class="composer-agent-select__option-model">{{ option.model }}</span>
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

const { t } = useI18n()

const props = defineProps<{
  options: Array<{ id: string; name: string; model?: string }>
  selectedAgentId: string
}>()

const emit = defineEmits<{
  close: []
  selectAgent: [agentId: string]
}>()

function selectAgent(agentId: string) {
  if (agentId === props.selectedAgentId) {
    emit('close')
    return
  }
  emit('selectAgent', agentId)
  emit('close')
}

const rootRef = ref<HTMLElement | null>(null)
onMounted(() => rootRef.value?.focus())
</script>

<style scoped>
.composer-agent-select {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  width: min(320px, calc(100vw - 48px));
  padding: 0.75rem;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xl);
  z-index: 30;
}

.composer-agent-select__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.375rem;
  font-size: 0.8125rem;
  font-weight: 700;
  color: var(--text);
}

.composer-agent-select__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid transparent;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.composer-agent-select__close:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.composer-agent-select__hint {
  margin: 0 0 0.5rem;
  color: var(--text-muted);
  font-size: 0.75rem;
  line-height: 1.4;
}

.composer-agent-select__list {
  display: grid;
  gap: 0.375rem;
}

.composer-agent-select__option {
  display: grid;
  gap: 0.25rem;
  width: 100%;
  min-height: 44px;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}

.composer-agent-select__option:hover {
  border-color: color-mix(in srgb, var(--accent) 18%, var(--border));
  background: color-mix(in srgb, var(--accent) 3%, var(--bg-surface));
}

.composer-agent-select__option:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
}

.composer-agent-select__option.is-active {
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
  background: color-mix(in srgb, var(--accent) 5%, var(--bg-surface));
}

.composer-agent-select__option-main {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.composer-agent-select__option-main > .icon:last-child {
  margin-left: auto;
  color: var(--accent);
}

.composer-agent-select__option-label {
  font-size: 0.8125rem;
  font-weight: 700;
}

.composer-agent-select__option-model {
  color: var(--text-muted);
  font-size: 0.6875rem;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 520px) {
  .composer-agent-select {
    left: -2.75rem;
    width: calc(100vw - 32px);
  }
}
</style>
