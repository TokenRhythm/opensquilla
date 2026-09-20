<template>
  <div
    v-if="open"
    class="session-search"
    role="search"
    :aria-label="t('chat.sessionSearch.label')"
    data-testid="session-search"
  >
    <input
      ref="inputRef"
      :value="query"
      class="session-search__input"
      type="text"
      :placeholder="t('chat.sessionSearch.placeholder')"
      :aria-label="t('chat.sessionSearch.label')"
      spellcheck="false"
      data-testid="session-search-input"
      @input="$emit('update:query', ($event.target as HTMLInputElement).value)"
      @keydown.enter.prevent="$event.shiftKey ? $emit('prev') : $emit('next')"
      @keydown.esc.prevent="$emit('close')"
    />
    <span
      v-if="query.trim()"
      class="session-search__count"
      data-testid="session-search-count"
      :class="{ 'session-search__count--empty': total === 0 }"
    >
      {{ total === 0 ? t('chat.sessionSearch.noResults') : t('chat.sessionSearch.count', { index: active + 1, total }) }}
    </span>
    <div class="session-search__actions">
      <button
        type="button"
        class="session-search__button"
        :disabled="total === 0"
        :aria-label="t('chat.sessionSearch.previous')"
        :title="t('chat.sessionSearch.previous')"
        data-testid="session-search-prev"
        @click="$emit('prev')"
      >
        <Icon name="arrowUp" :size="14" aria-hidden="true" />
      </button>
      <button
        type="button"
        class="session-search__button"
        :disabled="total === 0"
        :aria-label="t('chat.sessionSearch.next')"
        :title="t('chat.sessionSearch.next')"
        data-testid="session-search-next"
        @click="$emit('next')"
      >
        <Icon name="chevronDown" :size="14" aria-hidden="true" />
      </button>
      <button
        type="button"
        class="session-search__button"
        :aria-label="t('chat.sessionSearch.close')"
        :title="t('chat.sessionSearch.close')"
        data-testid="session-search-close"
        @click="$emit('close')"
      >
        <Icon name="x" :size="14" aria-hidden="true" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

const props = defineProps<{
  open: boolean
  query: string
  active: number
  total: number
}>()

const emit = defineEmits<{
  'update:query': [value: string]
  next: []
  prev: []
  close: []
}>()

const { t } = useI18n()
const inputRef = ref<HTMLInputElement | null>(null)

watch(() => props.open, open => {
  if (!open) return
  void nextTick(() => {
    inputRef.value?.focus()
    inputRef.value?.select()
  })
}, { immediate: true })

defineExpose({
  focus: () => inputRef.value?.focus(),
})
</script>

<style scoped>
.session-search {
  position: absolute;
  top: 10px;
  right: 18px;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: min(420px, calc(100% - 36px));
  padding: 5px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-card);
  background: var(--bg-elevated, var(--card));
  box-shadow: var(--shadow-lg, var(--shadow-md));
}
.session-search__input {
  flex: 1 1 auto;
  min-width: 140px;
  min-height: 28px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg, var(--card));
  color: var(--text);
  font: inherit;
  font-size: 0.8125rem;
}
.session-search__input:focus-visible {
  outline: 0;
  border-color: color-mix(in srgb, var(--accent) 55%, var(--border));
  box-shadow: var(--focus-ring);
}
.session-search__count {
  flex: 0 0 auto;
  padding: 0 2px;
  font-size: 0.72rem;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted, var(--muted));
  white-space: nowrap;
}
.session-search__count--empty {
  color: var(--warn, var(--muted));
}
.session-search__actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.session-search__button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 28px;
  min-height: 28px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted, var(--muted));
  cursor: pointer;
}
.session-search__button:hover:not(:disabled),
.session-search__button:focus-visible {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--text);
}
.session-search__button:disabled {
  opacity: 0.45;
  cursor: default;
}
@media (max-width: 720px) {
  .session-search {
    right: 12px;
  }
  .session-search__button {
    min-width: 36px;
    min-height: 36px;
  }
}
</style>
