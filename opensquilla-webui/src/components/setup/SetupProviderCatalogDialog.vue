<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

interface ProviderOption { providerId: string; label: string }

const props = defineProps<{
  open: boolean
  providers: ProviderOption[]
  configuredIds: string[]
}>()
const emit = defineEmits<{
  close: [restoreFocus?: boolean]
  select: [providerId: string]
}>()
const { t, locale } = useI18n()
const pickerRef = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)
const query = ref('')
const activeIndex = ref(0)
const FEATURED = ['tokenrhythm', 'openrouter', 'deepseek', 'gemini']

const available = computed(() => {
  const configured = new Set(props.configuredIds.map(id => id.trim().toLowerCase()))
  return props.providers.filter(provider => !configured.has(provider.providerId.trim().toLowerCase()))
})

function sortProviders(rows: ProviderOption[]): ProviderOption[] {
  const featuredRank = new Map(FEATURED.map((providerId, index) => [providerId, index]))
  return [...rows].sort((a, b) => {
    const aId = a.providerId.trim().toLowerCase()
    const bId = b.providerId.trim().toLowerCase()
    const aRank = featuredRank.get(aId) ?? FEATURED.length
    const bRank = featuredRank.get(bId) ?? FEATURED.length
    if (aRank !== bRank) return aRank - bRank
    return a.label.localeCompare(b.label, locale.value) || aId.localeCompare(bId)
  })
}

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return sortProviders(needle
    ? available.value.filter(
      provider => `${provider.label} ${provider.providerId}`.toLowerCase().includes(needle),
    )
    : available.value)
})

const activeId = computed(() => filtered.value[activeIndex.value]
  ? `setup-provider-catalog-option-${activeIndex.value}`
  : undefined)

function close(restoreFocus = true) { emit('close', restoreFocus) }
function choose(providerId: string) { emit('select', providerId) }
function move(index: number) {
  const length = filtered.value.length
  activeIndex.value = length ? (index + length) % length : 0
  void nextTick(() => document.getElementById(activeId.value || '')?.scrollIntoView({ block: 'nearest' }))
}
function onInputFocus() {
  activeIndex.value = 0
}
function onInputKeydown(event: KeyboardEvent) {
  if (event.key === 'ArrowDown') { event.preventDefault(); move(activeIndex.value + 1) }
  else if (event.key === 'ArrowUp') { event.preventDefault(); move(activeIndex.value - 1) }
  else if (event.key === 'Home') { event.preventDefault(); move(0) }
  else if (event.key === 'End') { event.preventDefault(); move(filtered.value.length - 1) }
  else if (event.key === 'Enter' && filtered.value[activeIndex.value]) {
    event.preventDefault(); choose(filtered.value[activeIndex.value]!.providerId)
  }
}
function onPickerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  event.stopPropagation()
  event.preventDefault()
  close()
}

function onDocumentPointerDown(event: PointerEvent) {
  if (!props.open || pickerRef.value?.contains(event.target as Node)) return
  const target = event.target as HTMLElement | null
  if (target?.closest('[data-provider-picker-trigger]')) return
  close(false)
}

onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown))

watch(filtered, rows => {
  activeIndex.value = Math.min(activeIndex.value, Math.max(0, rows.length - 1))
})
watch(() => props.open, open => {
  if (!open) return
  query.value = ''
  activeIndex.value = 0
  void nextTick(() => inputRef.value?.focus())
})
</script>

<template>
  <section
    v-if="open"
    id="setup-provider-catalog-picker"
    ref="pickerRef"
    class="provider-picker"
    role="region"
    :aria-labelledby="'setup-provider-catalog-title'"
    data-testid="provider-catalog-picker"
    @keydown.capture="onPickerKeydown"
  >
    <header class="provider-picker__head">
      <div>
        <h4 id="setup-provider-catalog-title">{{ t('setup.provider.catalogTitle') }}</h4>
        <p>{{ t('setup.provider.catalogDesc') }}</p>
      </div>
      <button type="button" class="btn btn--icon btn--ghost" :aria-label="t('common.close')" @click="close()">
        <Icon name="x" :size="16" />
      </button>
    </header>
    <label class="provider-picker__search">
      <span>{{ t('setup.provider.searchProviders') }}</span>
      <input
        ref="inputRef"
        v-model="query"
        class="control-input"
        name="setup_provider_search"
        type="search"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded="true"
        aria-controls="setup-provider-catalog-list"
        :aria-activedescendant="activeId"
        :placeholder="t('setup.provider.searchProvidersPlaceholder')"
        autocomplete="off"
        @focus="onInputFocus"
        @keydown="onInputKeydown"
      >
    </label>
    <div class="provider-picker__results-head">
      <p class="provider-picker__section-label">
        {{ t('setup.provider.allProviders') }}
      </p>
      <span class="provider-picker__count" role="status" aria-live="polite" aria-atomic="true">
        {{ t('setup.provider.providerResultCount', { count: filtered.length }) }}
      </span>
    </div>
    <div
      id="setup-provider-catalog-list"
      class="provider-picker__list"
      role="listbox"
      :aria-label="t('setup.provider.providerResults')"
    >
      <button
        v-for="(provider, index) in filtered"
        :id="`setup-provider-catalog-option-${index}`"
        :key="provider.providerId"
        type="button"
        class="provider-picker__option"
        :class="{ 'is-active': index === activeIndex }"
        role="option"
        tabindex="-1"
        :aria-selected="index === activeIndex ? 'true' : 'false'"
        @mousemove="activeIndex = index"
        @click="choose(provider.providerId)"
      >
        <strong>{{ provider.label }}</strong>
        <span v-if="provider.providerId.trim().toLowerCase() === 'tokenrhythm'" class="control-pill">
          {{ t('setup.provider.recommendedBadge') }}
        </span>
        <code v-else>{{ provider.providerId }}</code>
      </button>
      <p v-if="filtered.length === 0" class="provider-picker__empty">{{ t('setup.provider.noProviderMatches') }}</p>
    </div>
  </section>
</template>

<style scoped>
.provider-picker {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: grid;
  gap: var(--sp-2);
  padding: var(--sp-3);
  width: 100%;
}
.provider-picker__head {
  align-items: flex-start;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
}
.provider-picker__head h4,
.provider-picker__head p { margin: 0; }
.provider-picker__head p {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin-top: 2px;
}
.provider-picker__search {
  display: grid;
  font-size: var(--fs-sm);
  gap: 2px;
}
.provider-picker__search .control-input {
  max-width: none;
  width: 100%;
}
.provider-picker__results-head {
  align-items: baseline;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
}
.provider-picker__section-label,
.provider-picker__count {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  margin: 0;
}
.provider-picker__section-label {
  font-weight: 600;
  text-transform: uppercase;
}
.provider-picker__list {
  display: grid;
  gap: var(--sp-1);
  max-height: min(320px, 42dvh);
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}
.provider-picker__option {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text);
  cursor: pointer;
  display: flex;
  gap: var(--sp-3);
  justify-content: space-between;
  min-height: 42px;
  padding: var(--sp-2) var(--sp-3);
  text-align: left;
}
.provider-picker__option code {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.provider-picker__option:hover,
.provider-picker__option.is-active,
.provider-picker__option:focus-visible {
  background: var(--bg-hover);
  border-color: var(--border);
  outline: none;
}
.provider-picker__empty {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: var(--sp-3);
  text-align: center;
}
@container provider-panel (max-width: 560px) {
  .provider-picker__option { align-items: flex-start; flex-direction: column; gap: var(--sp-1); }
  .provider-picker__results-head { align-items: flex-start; flex-direction: column; gap: var(--sp-1); }
}
</style>
