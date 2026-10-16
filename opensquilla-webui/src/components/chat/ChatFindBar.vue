<template>
  <div class="chat-find-bar" role="search" :aria-label="t('chat.find.label')" @keydown="onKeydown">
    <Icon name="search" :size="15" class="chat-find-bar__icon" />
    <input
      ref="input"
      class="chat-find-bar__input"
      type="text"
      :value="draft"
      :aria-label="t('chat.find.label')"
      :placeholder="t('chat.find.placeholder')"
      autocomplete="off"
      spellcheck="false"
      maxlength="256"
      @input="onInput"
      @compositionstart="onCompositionStart"
      @compositionend="onCompositionEnd"
    >
    <span
      class="chat-find-bar__count"
      :class="{ 'chat-find-bar__count--error': status === 'error' && resultsCurrent }"
      role="status"
      aria-live="polite"
      :aria-label="statusLabel"
      :title="statusLabel"
    >{{ countText }}</span>
    <div class="chat-find-bar__actions">
      <button type="button" class="chat-find-bar__button" :aria-label="t('chat.find.previous')"
        :title="t('chat.find.previous')" :disabled="!canNavigate" @click="navigate('previous')">
        <Icon name="arrowUp" :size="14" />
      </button>
      <button type="button" class="chat-find-bar__button" :aria-label="t('chat.find.next')"
        :title="t('chat.find.next')" :disabled="!canNavigate" @click="navigate('next')">
        <Icon name="chevronDown" :size="14" />
      </button>
      <button type="button" class="chat-find-bar__button" :aria-label="t('chat.find.close')"
        :title="t('chat.find.close')" @click="close">
        <Icon name="x" :size="15" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

const props = withDefaults(defineProps<{
  query: string
  /** Query owning the displayed results; a pending edit must not show stale counts. */
  resultQuery?: string
  matchCount: number
  /** One-based current result; zero means no active result. */
  activeMatch: number
  status?: 'idle' | 'loading' | 'ready' | 'error'
  focusSequence?: number
  complete?: boolean
  coverageComplete?: boolean
}>(), { status: 'idle', focusSequence: 0, complete: true, coverageComplete: true })
const emit = defineEmits<{
  'update:query': [query: string]
  search: [query: string]
  navigate: [direction: 'previous' | 'next']
  close: []
}>()
const { t } = useI18n()
const input = ref<HTMLInputElement | null>(null)
const draft = ref(props.query)
const composing = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let lastSubmittedQuery = props.resultQuery ?? props.query
const resultsCurrent = computed(() => draft.value === (props.resultQuery ?? props.query))
const pending = computed(() => Boolean(draft.value.trim()) && (!resultsCurrent.value || props.status === 'loading'))
const canNavigate = computed(() => !composing.value && resultsCurrent.value && props.status === 'ready' && (props.matchCount > 0 || !props.complete))
const countText = computed(() => {
  if (!draft.value.trim()) return ''
  if (pending.value) return '…'
  if (props.status === 'error') return '!'
  return `${Math.min(Math.max(props.activeMatch, 0), props.matchCount)}/${props.matchCount}${props.complete && props.coverageComplete ? '' : '+'}`
})
const statusLabel = computed(() => {
  if (!draft.value.trim()) return ''
  if (pending.value) return t('chat.find.loading')
  if (props.status === 'error') return t('chat.find.error')
  if (!props.complete) return t('chat.find.partial', { current: props.activeMatch, total: props.matchCount })
  if (!props.coverageComplete) return t('chat.find.incomplete', { current: props.activeMatch, total: props.matchCount })
  if (props.matchCount === 0) return t('chat.find.noResults')
  return t('chat.find.count', { current: props.activeMatch, total: props.matchCount })
})

function clearTimer() {
  if (timer !== undefined) clearTimeout(timer)
  timer = undefined
}

function submit() {
  clearTimer()
  if (composing.value) return
  lastSubmittedQuery = draft.value
  emit('search', draft.value)
}

function updateDraft(value: string) {
  draft.value = value
  clearTimer()
  if (composing.value) return
  emit('update:query', value)
  // Clearing the field must clear its old highlights immediately.
  if (!value.trim()) submit()
  else timer = setTimeout(submit, 200)
}

function onInput(event: Event) {
  updateDraft((event.target as HTMLInputElement).value)
}

function onCompositionStart() {
  composing.value = true
  clearTimer()
}

function onCompositionEnd(event: CompositionEvent) {
  composing.value = false
  updateDraft((event.target as HTMLInputElement).value)
}

function navigate(direction: 'previous' | 'next') {
  if (canNavigate.value) emit('navigate', direction)
}

function close() {
  clearTimer()
  emit('close')
}

function onKeydown(event: KeyboardEvent) {
  // An IME confirmation/cancellation belongs to the input method, not the find UI.
  if (composing.value || event.isComposing || event.keyCode === 229) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopImmediatePropagation()
    close()
  } else if (event.key === 'Enter') {
    event.preventDefault()
    event.stopPropagation()
    if (event.repeat) return
    if (draft.value !== lastSubmittedQuery || props.status === 'error') submit()
    else navigate(event.shiftKey ? 'previous' : 'next')
  }
}

watch(() => props.query, value => {
  if (!composing.value && value !== draft.value) {
    clearTimer()
    draft.value = value
    lastSubmittedQuery = value
  }
})
watch(() => props.focusSequence, () => {
  void nextTick(() => input.value?.focus({ preventScroll: true }))
}, { immediate: true })
onBeforeUnmount(clearTimer)

defineExpose({ focus: () => input.value?.focus({ preventScroll: true }) })
</script>

<style scoped>
.chat-find-bar {
  display: flex;
  align-items: center;
  gap: var(--sp-1);
  width: min(320px, 100%);
  min-width: 0;
  height: 36px;
  padding: 0 var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  background: var(--bg-elevated);
  color: var(--text);
  box-shadow: var(--shadow-sm);
}
.chat-find-bar__icon { flex-shrink: 0; color: var(--text-muted); }
.chat-find-bar:focus-within { border-color: var(--accent); }
.chat-find-bar input.chat-find-bar__input {
  flex: 1;
  width: 0;
  min-width: 0;
  height: 100%;
  min-height: 0;
  padding: 0 var(--sp-1);
  border: 0;
  border-radius: 0;
  outline: none;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
}
.chat-find-bar input.chat-find-bar__input:focus {
  background: transparent;
  box-shadow: none;
  outline: none;
}
.chat-find-bar__input::placeholder { color: var(--text-muted); }
.chat-find-bar__count {
  flex: 0 0 auto;
  min-width: 30px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
  text-align: center;
}
.chat-find-bar__count--error { color: var(--warn); }
.chat-find-bar__actions { display: flex; flex-shrink: 0; gap: 0; }
.chat-find-bar__button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.chat-find-bar__button:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.chat-find-bar__button:disabled { opacity: 0.4; cursor: default; }
.chat-find-bar__button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
</style>
