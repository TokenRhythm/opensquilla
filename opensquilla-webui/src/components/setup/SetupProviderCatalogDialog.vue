<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

interface ProviderOption { providerId: string; label: string }

const props = defineProps<{
  open: boolean
  providers: ProviderOption[]
  configuredIds: string[]
  embedded?: boolean
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

const configured = computed(() => new Set(
  props.configuredIds.map(id => id.trim().toLowerCase()),
))

const available = computed(() => {
  return props.providers.filter(provider => {
    const providerId = provider.providerId.trim().toLowerCase()
    return providerId === 'tokenrhythm' || !configured.value.has(providerId)
  })
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

const recommendedProviders = computed(() => filtered.value
  .map((provider, index) => ({ provider, index }))
  .filter(({ provider }) => provider.providerId.trim().toLowerCase() === 'tokenrhythm'))
const otherProviders = computed(() => filtered.value
  .map((provider, index) => ({ provider, index }))
  .filter(({ provider }) => provider.providerId.trim().toLowerCase() !== 'tokenrhythm'))

const activeId = computed(() => filtered.value[activeIndex.value]
  ? `setup-provider-catalog-option-${activeIndex.value}`
  : undefined)

function close(restoreFocus = true) { emit('close', restoreFocus) }
function isConfigured(providerId: string) {
  return configured.value.has(providerId.trim().toLowerCase())
}
function choose(providerId: string) {
  if (isConfigured(providerId)) return
  emit('select', providerId)
}
function move(index: number, direction = 1) {
  const length = filtered.value.length
  if (!length) {
    activeIndex.value = 0
    return
  }
  let candidate = (index + length) % length
  for (let visited = 0; visited < length; visited += 1) {
    const provider = filtered.value[candidate]
    if (provider && !isConfigured(provider.providerId)) break
    candidate = (candidate + direction + length) % length
  }
  activeIndex.value = candidate
  void nextTick(() => document.getElementById(activeId.value || '')?.scrollIntoView({ block: 'nearest' }))
}
function onInputFocus() {
  move(0)
}
function onInputKeydown(event: KeyboardEvent) {
  if (event.key === 'ArrowDown') { event.preventDefault(); move(activeIndex.value + 1, 1) }
  else if (event.key === 'ArrowUp') { event.preventDefault(); move(activeIndex.value - 1, -1) }
  else if (event.key === 'Home') { event.preventDefault(); move(0) }
  else if (event.key === 'End') { event.preventDefault(); move(filtered.value.length - 1, -1) }
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
  if (props.embedded || !props.open || pickerRef.value?.contains(event.target as Node)) return
  const target = event.target as HTMLElement | null
  if (target?.closest('[data-provider-picker-trigger]')) return
  close(false)
}

function focusPicker() {
  query.value = ''
  void nextTick(() => {
    move(0)
    inputRef.value?.focus()
  })
}

onMounted(() => {
  document.addEventListener('pointerdown', onDocumentPointerDown)
  if (props.open) focusPicker()
})
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown))

watch(filtered, rows => {
  activeIndex.value = Math.min(activeIndex.value, Math.max(0, rows.length - 1))
  const activeProvider = rows[activeIndex.value]
  if (activeProvider && isConfigured(activeProvider.providerId)) move(activeIndex.value)
})
watch(() => props.open, open => {
  if (!open) return
  focusPicker()
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
    <header v-if="!embedded" class="provider-picker__head">
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
      <span class="provider-picker__search-control">
        <Icon name="search" :size="16" aria-hidden="true" />
        <input
          ref="inputRef"
          v-model="query"
          class="provider-picker__search-input"
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
      </span>
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
      <section v-if="recommendedProviders.length" class="provider-picker__group" role="presentation">
        <p class="provider-picker__group-label">{{ t('setup.provider.recommendedProviders') }}</p>
        <button
          v-for="{ provider, index } in recommendedProviders"
          :id="`setup-provider-catalog-option-${index}`"
          :key="provider.providerId"
          type="button"
          class="provider-picker__option"
          :class="{ 'is-active': index === activeIndex }"
          role="option"
          tabindex="-1"
          :disabled="isConfigured(provider.providerId)"
          :aria-selected="index === activeIndex ? 'true' : 'false'"
          :title="isConfigured(provider.providerId) ? t('setup.provider.alreadyConfigured') : undefined"
          @mousemove="activeIndex = index"
          @click="choose(provider.providerId)"
        >
          <span class="provider-picker__option-copy">
            <strong>{{ provider.label }}</strong>
            <code>{{ provider.providerId }}</code>
          </span>
          <span class="control-pill provider-picker__offer">
            {{ t('setup.provider.limitedFreeBadge') }}
          </span>
        </button>
      </section>
      <section v-if="otherProviders.length" class="provider-picker__group" role="presentation">
        <p class="provider-picker__group-label">{{ t('setup.provider.otherProviders') }}</p>
        <button
          v-for="{ provider, index } in otherProviders"
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
          <span class="provider-picker__option-copy">
            <strong>{{ provider.label }}</strong>
            <code>{{ provider.providerId }}</code>
          </span>
        </button>
      </section>
      <p v-if="filtered.length === 0" class="provider-picker__empty">{{ t('setup.provider.noProviderMatches') }}</p>
    </div>
  </section>
</template>

<style scoped>
.provider-picker {
  background: transparent;
  border: 0;
  border-radius: var(--radius-md);
  display: grid;
  gap: var(--sp-3);
  padding: 0;
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
  font-weight: 600;
  gap: var(--sp-2);
}

.provider-picker__search-control {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  display: flex;
  gap: var(--sp-2);
  min-height: 44px;
  padding: 0 var(--sp-3);
  transition: border-color var(--dur-fast) var(--ease-out),
              box-shadow var(--dur-fast) var(--ease-out),
              background var(--dur-fast) var(--ease-out);
}

.provider-picker__search-control:hover {
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border-strong));
}

.provider-picker__search-control:focus-within {
  background: var(--bg-elevated);
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--accent);
}

.provider-picker__search-control > input.provider-picker__search-input,
.provider-picker__search-control > input.provider-picker__search-input:focus {
  appearance: none;
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  color: var(--text);
  flex: 1 1 auto;
  font: inherit;
  font-weight: 400;
  max-width: none;
  min-height: 42px;
  outline: none;
  padding: 0;
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
  gap: var(--sp-3);
  max-height: min(320px, 42dvh);
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}
.provider-picker__group {
  display: grid;
  gap: var(--sp-1);
}
.provider-picker__group-label {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  font-weight: 600;
  margin: 0;
  padding: 0 var(--sp-2);
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
.provider-picker__option-copy {
  display: grid;
  gap: 1px;
  min-width: 0;
}
.provider-picker__option code {
  color: var(--text-muted);
  font-size: var(--fs-xs);
  overflow-wrap: anywhere;
}
.provider-picker__offer {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-color: color-mix(in srgb, var(--accent) 25%, transparent);
  color: var(--accent-deep);
  flex: 0 0 auto;
}
.provider-picker__option:hover,
.provider-picker__option.is-active,
.provider-picker__option:focus-visible {
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-hover));
  border-color: color-mix(in srgb, var(--accent) 24%, var(--border));
  color: var(--accent-deep);
  outline: none;
}
.provider-picker__option:disabled {
  cursor: default;
  opacity: 0.66;
}
.provider-picker__option:disabled:hover {
  background: transparent;
  border-color: transparent;
  color: var(--text);
}
.provider-picker__empty {
  color: var(--text-muted);
  font-size: var(--fs-sm);
  margin: var(--sp-3);
  text-align: center;
}
@container provider-panel (max-width: 560px) {
  .provider-picker__option { align-items: center; gap: var(--sp-2); }
  .provider-picker__results-head { align-items: flex-start; flex-direction: column; gap: var(--sp-1); }
}
</style>
