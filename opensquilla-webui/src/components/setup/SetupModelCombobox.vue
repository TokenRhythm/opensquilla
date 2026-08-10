<script setup lang="ts">
// Shared model picker for provider defaults, router tiers, and ensemble roles:
// the plain text input stays the primary control (free text ALWAYS works —
// discovery is an accelerator, never a gate), with an optional dropdown of
// live-discovered models layered on top. Presentational only: props in, events
// out (panel-contract pattern).
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { DiscoveredModel } from '@/composables/setup/useSetupProviderForm'

const { t } = useI18n()

interface FieldSpec {
  name: string
  label: string
  required?: boolean
  placeholder?: string
  description?: string
  descriptionPresentation?: 'inline' | 'info'
  [key: string]: unknown
}

interface LeadingOption {
  value: string
  label: string
  description?: string
}

const props = defineProps<{
  field: FieldSpec
  value: string
  models: DiscoveredModel[]
  modelSource: string
  // Cell mode drops the outer control-row + label chrome so the combobox can
  // live inside a table cell (tier table); the field label becomes the input's
  // aria-label. Default (false) renders the full settings-row layout unchanged.
  cell?: boolean
  disabled?: boolean
  // Embedded form rows can opt into the shared input chrome without changing
  // compact tier-table cells that already provide their own styling.
  inputClass?: string
  // Optional status/help text rendered by the parent outside this component.
  // This keeps compact table cells accessible without duplicating visible copy.
  externalDescriptionId?: string
  // Optional semantic choice pinned above provider models. Router C3 uses it
  // for the shared multi-model plan while retaining free-text model entry.
  leadingOption?: LeadingOption
  // Semantic pickers can keep search text local until the user explicitly
  // chooses a catalog row (or the "use typed value" row). This prevents a
  // search query from changing the saved processing mode while it is typed.
  commitOnSelect?: boolean
}>()

const emit = defineEmits<{
  update: [value: string]
}>()

const MAX_ROWS = 40

// Dropdown geometry: the listbox is teleported to <body> because both hosts
// live inside the settings dialog's scrolling panels, which clip an in-flow
// absolute dropdown. Position is computed from the input's viewport rect.
const DROPDOWN_MAX_HEIGHT = 280
// Table cells are intentionally compact, but the catalog needs enough room for
// a model id, friendly name, and capability summary. The floating layer is
// therefore wider than its anchor whenever the viewport allows it.
const DROPDOWN_PREFERRED_WIDTH = 480
const DROPDOWN_GAP = 4
const DROPDOWN_MARGIN = 8

const open = ref(false)
const activeIndex = ref(-1)
const inputEl = ref<HTMLInputElement | null>(null)
const listStyle = ref<Record<string, string>>({})
// A saved model id must not hide the rest of the discovered list: opening the
// dropdown (focus / arrow keys) always shows every model, and the query filter
// only kicks in once the user edits the text during this open.
const typedSinceOpen = ref(false)
const localInputValue = ref('')

const fieldId = computed(() => `setup-provider-${String(props.field.name || 'model')}`)
const fieldName = computed(() => `setup_provider_${String(props.field.name || 'model')}`)
const fieldDescriptionId = computed(() => `${fieldId.value}-description`)
const fieldTooltipId = computed(() => `${fieldId.value}-info-tooltip`)

const leadingSelected = computed(() => Boolean(
  props.leadingOption && props.value === props.leadingOption.value,
))
const committedDisplayValue = computed(() => (
  leadingSelected.value ? props.leadingOption?.label || '' : props.value
))
const inputDisplayValue = computed(() => (
  props.commitOnSelect ? localInputValue.value : committedDisplayValue.value
))
const typedValue = computed(() => (
  props.commitOnSelect ? localInputValue.value : String(props.value || '')
))
const query = computed(() => (
  leadingSelected.value && !(props.commitOnSelect && typedSinceOpen.value)
    ? ''
    : typedValue.value.trim().toLowerCase()
))
const catalogAvailable = computed(() => (
  !props.disabled
  && (
    Boolean(props.leadingOption)
    || (
      (props.modelSource === 'live' || props.modelSource === 'catalog')
      && props.models.length > 0
    )
  )
))
const describedBy = computed(() => {
  const ids: string[] = []
  if (!props.cell && props.field.description) ids.push(fieldDescriptionId.value)
  if (props.externalDescriptionId) ids.push(props.externalDescriptionId)
  if (catalogAvailable.value) ids.push(`${fieldId.value}-catalog-count`)
  return ids.join(' ') || undefined
})

// The discovered id exactly matching the field's current value, if any.
const selectedId = computed(() => {
  const typed = String(props.value || '').trim()
  return typed && props.models.some(model => model.id === typed) ? typed : ''
})

const typedExactId = computed(() => {
  const typed = typedValue.value.trim()
  return typed && props.models.some(model => model.id === typed) ? typed : ''
})

const matches = computed(() => {
  if (!query.value || !typedSinceOpen.value) {
    // Full-list mode: pin the current model first so it stays rendered and
    // findable even when the list is longer than the MAX_ROWS window.
    const idx = selectedId.value ? props.models.findIndex(m => m.id === selectedId.value) : -1
    if (idx <= 0) return props.models
    return [props.models[idx], ...props.models.slice(0, idx), ...props.models.slice(idx + 1)]
  }
  return props.models.filter(model =>
    model.id.toLowerCase().includes(query.value) || model.name.toLowerCase().includes(query.value),
  )
})

const visibleModels = computed(() => matches.value.slice(0, MAX_ROWS))
const truncatedCount = computed(() => Math.max(0, matches.value.length - visibleModels.value.length))

// The escape hatch: whatever was typed is always usable as-is. Shown whenever
// there is typed text that is not an exact discovered id.
const showFreeTextRow = computed(() => {
  if (leadingSelected.value && !(props.commitOnSelect && typedSinceOpen.value)) return false
  const typed = typedValue.value.trim()
  if (!typed) return false
  return !typedExactId.value
})

const leadingOptionCount = computed(() => props.leadingOption ? 1 : 0)
const optionCount = computed(() => (
  leadingOptionCount.value
  + visibleModels.value.length
  + (showFreeTextRow.value ? 1 : 0)
))
const leadingOptionId = computed(() => `${fieldId.value}-option-leading`)
const freeTextOptionId = computed(() => `${fieldId.value}-option-custom`)
const activeOptionId = computed(() => {
  if (!open.value || activeIndex.value < 0) return undefined
  if (props.leadingOption && activeIndex.value === 0) return leadingOptionId.value
  const modelIndex = activeIndex.value - leadingOptionCount.value
  if (modelIndex >= 0 && modelIndex < visibleModels.value.length) {
    return modelOptionId(modelIndex)
  }
  return showFreeTextRow.value ? freeTextOptionId.value : undefined
})

// Muted provenance footer (progressive disclosure): explain metadata sourcing
// once instead of repeating it in every row. Metadata-aware gateways get the
// published/catalog copy; older gateways keep the capability-source fallback.
const provenance = computed(() => {
  if (props.modelSource !== 'live') return ''
  if (props.models.some(model => model.metadata?.schemaVersion === 1)) {
    return t('setup.provider.modelMetadataProvenance')
  }
  const sources = Array.from(new Set(props.models.map(model => model.capabilitySource).filter(Boolean)))
  if (!sources.length) return ''
  return t('setup.provider.modelProvenance', { sources: sources.join(', ') })
})

function compactTokens(count: number | null): string {
  if (count === null || !Number.isFinite(count) || count <= 0) return ''
  if (count >= 1_000_000) {
    const millions = Math.round((count / 1_000_000) * 10) / 10
    return `${millions % 1 === 0 ? millions.toFixed(0) : millions}M`
  }
  if (count >= 1000) return `${Math.round(count / 1000)}k`
  return String(count)
}

interface ModelRowMetric {
  kind: 'context' | 'output'
  tokens: string
  shortLabel: string
  accessibleLabel: string
}

const NORMAL_MODEL_STATUSES = new Set(['active', 'available', 'online', 'ready'])

function rowMetrics(model: DiscoveredModel): ModelRowMetric[] {
  const metrics: ModelRowMetric[] = []
  const published = model.metadata?.schemaVersion === 1 ? model.metadata.published : null
  const publishedContext = compactTokens(published?.contextWindow ?? null)
  const ctx = publishedContext || compactTokens(model.contextWindow)
  if (ctx) {
    metrics.push({
      kind: 'context',
      tokens: ctx,
      shortLabel: t('setup.provider.modelContextShort'),
      accessibleLabel: t(
        publishedContext
          ? 'setup.provider.modelPublishedContext'
          : 'setup.provider.modelCatalogContext',
        { tokens: ctx },
      ),
    })
  }
  const publishedMaxOutput = compactTokens(published?.maxOutputTokens ?? null)
  const maxOutput = publishedMaxOutput || compactTokens(model.maxOutputTokens)
  if (maxOutput) {
    metrics.push({
      kind: 'output',
      tokens: maxOutput,
      shortLabel: t('setup.provider.modelOutputShort'),
      accessibleLabel: t(
        publishedMaxOutput
          ? 'setup.provider.modelPublishedMaxOutput'
          : 'setup.provider.modelCatalogMaxOutput',
        { tokens: maxOutput },
      ),
    })
  }
  return metrics
}

function rowStatus(model: DiscoveredModel): string {
  const metadata = model.metadata?.schemaVersion === 1 ? model.metadata : null
  const status = String(metadata?.published?.status || metadata?.declared?.status || '').trim()
  const normalized = status.toLowerCase()
  if (!normalized || NORMAL_MODEL_STATUSES.has(normalized)) return ''
  if (normalized === 'testing') return t('setup.provider.modelStatusTesting')
  if (normalized === 'offline') return t('setup.provider.modelStatusOffline')
  return status
}

function usesCatalogOnlyMetadata(model: DiscoveredModel): boolean {
  return model.metadata?.schemaVersion === 1 && model.metadata.published === null
}

function modelOptionId(index: number): string {
  return `${fieldId.value}-option-${index}`
}

function scrollActiveOptionIntoView() {
  void nextTick(() => {
    const id = activeOptionId.value
    if (!id) return
    document.getElementById(id)?.scrollIntoView?.({ block: 'nearest' })
  })
}

function onInput(event: Event) {
  const value = (event.target as HTMLInputElement).value
  if (props.commitOnSelect && catalogAvailable.value) localInputValue.value = value
  else emit('update', value)
  if (!catalogAvailable.value) return
  open.value = true
  typedSinceOpen.value = true
  activeIndex.value = -1
}

// The single way to open in full-list mode; every open path that is not a
// text edit must go through it so the filter never leaks across opens.
function openList() {
  if (!catalogAvailable.value) return
  if (props.commitOnSelect) localInputValue.value = committedDisplayValue.value
  open.value = true
  typedSinceOpen.value = false
  activeIndex.value = -1
  if (leadingSelected.value) {
    void nextTick(() => inputEl.value?.select())
  }
}

function onClick() {
  // Reopen on click for a still-focused input (row click / Escape keep DOM
  // focus, so no new `focus` event will fire). Never touch an open list —
  // caret moves while editing must not clear an in-progress filter.
  if (catalogAvailable.value && !open.value) openList()
}

function toggleList() {
  if (!catalogAvailable.value) return
  if (open.value) {
    close(true)
    return
  }
  openList()
  inputEl.value?.focus()
}

function close(restoreCommitted = true) {
  open.value = false
  typedSinceOpen.value = false
  activeIndex.value = -1
  if (props.commitOnSelect && restoreCommitted) {
    localInputValue.value = committedDisplayValue.value
  }
}

watch(catalogAvailable, available => {
  if (!available) close(true)
})

watch(committedDisplayValue, value => {
  // Parent updates following an explicit selection become the next committed
  // display. Do not overwrite an in-progress local search query.
  if (!open.value || !typedSinceOpen.value) localInputValue.value = value
}, { immediate: true })

function selectModel(id: string, displayValue = id) {
  if (props.commitOnSelect) localInputValue.value = displayValue
  emit('update', id)
  close(false)
}

function selectTypedValue() {
  if (!props.commitOnSelect) {
    close()
    return
  }
  const value = localInputValue.value.trim()
  if (!value) return
  selectModel(value)
}

function selectAt(index: number) {
  if (index < 0 || index >= optionCount.value) return
  if (props.leadingOption && index === 0) {
    selectModel(props.leadingOption.value, props.leadingOption.label)
  }
  else if (index - leadingOptionCount.value < visibleModels.value.length) {
    selectModel(visibleModels.value[index - leadingOptionCount.value].id)
  }
  else selectTypedValue()
}

// Anchor the teleported listbox to the input: below by default, flipped above
// when the space under the input is too short, clamped to the viewport.
function updateListPosition() {
  const input = inputEl.value
  if (!input) return
  const rect = input.getBoundingClientRect()
  const viewportW = window.innerWidth
  const viewportH = window.innerHeight
  const spaceBelow = viewportH - rect.bottom - DROPDOWN_GAP - DROPDOWN_MARGIN
  const spaceAbove = rect.top - DROPDOWN_GAP - DROPDOWN_MARGIN
  const openUp = spaceBelow < DROPDOWN_MAX_HEIGHT && spaceAbove > spaceBelow
  const maxHeight = Math.max(120, Math.min(DROPDOWN_MAX_HEIGHT, openUp ? spaceAbove : spaceBelow))
  const width = Math.min(
    Math.max(rect.width, DROPDOWN_PREFERRED_WIDTH),
    viewportW - 2 * DROPDOWN_MARGIN,
  )
  const left = Math.min(Math.max(rect.left, DROPDOWN_MARGIN), viewportW - width - DROPDOWN_MARGIN)
  listStyle.value = {
    left: `${left}px`,
    width: `${width}px`,
    maxHeight: `${maxHeight}px`,
    top: openUp ? 'auto' : `${rect.bottom + DROPDOWN_GAP}px`,
    bottom: openUp ? `${viewportH - rect.top + DROPDOWN_GAP}px` : 'auto',
  }
}

watch(open, isOpen => {
  if (isOpen) {
    updateListPosition()
    // Capture-phase scroll also catches the settings panel's inner scrolling,
    // which never bubbles to window.
    window.addEventListener('scroll', updateListPosition, { capture: true, passive: true })
    window.addEventListener('resize', updateListPosition)
  } else {
    window.removeEventListener('scroll', updateListPosition, { capture: true })
    window.removeEventListener('resize', updateListPosition)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('scroll', updateListPosition, { capture: true })
  window.removeEventListener('resize', updateListPosition)
})

function onKeydown(event: KeyboardEvent) {
  if (!catalogAvailable.value) return
  if (event.key === 'Escape') {
    // Consume Escape only when it dismisses the list; a closed combobox lets
    // it bubble so enclosing dialogs keep their Escape-to-close behavior.
    if (open.value) {
      event.preventDefault()
      event.stopPropagation()
      close(true)
    }
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    if (!open.value) {
      openList()
      return
    }
    if (!optionCount.value) return
    const delta = event.key === 'ArrowDown' ? 1 : -1
    activeIndex.value = (activeIndex.value + delta + optionCount.value) % optionCount.value
    scrollActiveOptionIntoView()
    return
  }
  if (event.key === 'Enter' && open.value && activeIndex.value >= 0) {
    event.preventDefault()
    selectAt(activeIndex.value)
  }
}
</script>

<template>
  <div :class="cell ? 'setup-model-combobox--cellwrap' : 'control-row control-row--stack'" :data-name="cell ? undefined : field.name" :data-scope="cell ? undefined : 'provider'">
    <div v-if="!cell" class="control-row__label-block">
      <label
        class="control-row__label"
        :class="{ 'setup-model-combobox__label--with-info': field.description && field.descriptionPresentation === 'info' }"
        :for="fieldId"
      >
        {{ field.label }}{{ field.required ? ' *' : '' }}
        <span
          v-if="field.description && field.descriptionPresentation === 'info'"
          class="setup-model-combobox__info"
          :aria-label="field.description"
          :aria-describedby="fieldTooltipId"
          tabindex="0"
        >
          <Icon name="info" :size="14" aria-hidden="true" />
          <span
            :id="fieldTooltipId"
            class="setup-model-combobox__info-tooltip"
            role="tooltip"
          >
            {{ field.description }}
          </span>
        </span>
      </label>
      <span
        v-if="field.description"
        :id="fieldDescriptionId"
        :class="field.descriptionPresentation === 'info'
          ? 'setup-model-combobox__sr-only'
          : 'control-row__desc'"
      >{{ field.description }}</span>
    </div>
    <div
      class="setup-model-combobox"
      :class="[cell ? undefined : 'control-row__control', { 'has-catalog': catalogAvailable }]"
    >
      <input
        :id="fieldId"
        ref="inputEl"
        :class="[cell ? undefined : 'control-input', inputClass]"
        :name="fieldName"
        type="text"
        :role="catalogAvailable ? 'combobox' : undefined"
        :aria-autocomplete="catalogAvailable ? 'list' : undefined"
        :aria-expanded="catalogAvailable ? (open ? 'true' : 'false') : undefined"
        :aria-controls="catalogAvailable ? `${fieldId}-listbox` : undefined"
        :aria-activedescendant="catalogAvailable ? activeOptionId : undefined"
        :aria-describedby="describedBy"
        :aria-label="cell ? field.label : undefined"
        autocomplete="one-time-code"
        autocapitalize="off"
        data-1p-ignore
        data-bwignore
        data-form-type="other"
        data-lpignore="true"
        data-protonpass-ignore="true"
        spellcheck="false"
        :disabled="disabled"
        :value="inputDisplayValue"
        :placeholder="field.placeholder || t('setup.provider.modelSearchOrCustom')"
        @input="onInput"
        @focus="openList"
        @click="onClick"
        @blur="close(true)"
        @keydown="onKeydown"
      >
      <span v-if="catalogAvailable" :id="`${fieldId}-catalog-count`" class="setup-model-combobox__sr-only">
        {{ t('setup.provider.modelOptionsToggle', { count: models.length + leadingOptionCount }) }}
      </span>
      <button
        v-if="catalogAvailable"
        type="button"
        class="setup-model-combobox__trigger"
        data-testid="setup-model-options-toggle"
        tabindex="-1"
        :aria-label="t('setup.provider.modelOptionsToggle', { count: models.length + leadingOptionCount })"
        :aria-controls="`${fieldId}-listbox`"
        :aria-expanded="open ? 'true' : 'false'"
        @mousedown.prevent
        @click="toggleList"
      >
        <Icon
          class="setup-model-combobox__chevron"
          name="chevronDown"
          :size="14"
          aria-hidden="true"
        />
      </button>
      <Teleport to="body">
        <div
          v-if="open"
          class="setup-model-combobox__popup"
          :style="listStyle"
          @mousedown.prevent
        >
          <div v-if="models.length" :id="`${fieldId}-catalog-readout`" class="setup-model-combobox__readout">
            <span>{{ t('setup.provider.modelListReadout', { count: models.length }) }}</span>
            <span v-if="modelSource === 'live'" class="setup-model-combobox__live-source">
              <span class="setup-model-combobox__live-dot" aria-hidden="true"></span>
              {{ t('setup.provider.modelLiveSource') }}
            </span>
          </div>
          <div
            :id="`${fieldId}-listbox`"
            class="setup-model-combobox__list"
            role="listbox"
            :aria-label="t('setup.provider.modelListLabel')"
            :aria-describedby="models.length ? `${fieldId}-catalog-readout` : undefined"
          >
            <button
              v-if="leadingOption"
              :id="leadingOptionId"
              type="button"
              class="setup-model-combobox__row setup-model-combobox__row--leading"
              :class="{ 'is-active': activeIndex === 0 }"
              role="option"
              :aria-selected="leadingSelected ? 'true' : 'false'"
              @mousedown.prevent
              @click="selectModel(leadingOption.value, leadingOption.label)"
            >
              <span class="setup-model-combobox__identity">
                <span class="setup-model-combobox__id">{{ leadingOption.label }}</span>
                <span v-if="leadingOption.description" class="setup-model-combobox__name">
                  {{ leadingOption.description }}
                </span>
              </span>
              <span v-if="leadingSelected" class="setup-model-combobox__selected">
                <Icon name="check" :size="12" aria-hidden="true" />
                <span class="setup-model-combobox__sr-only">
                  {{ t('setup.provider.modelSelected') }}
                </span>
              </span>
            </button>
            <button
              v-for="(model, index) in visibleModels"
              :key="model.id"
              :id="modelOptionId(index)"
              type="button"
              class="setup-model-combobox__row"
              :class="{ 'is-active': index + leadingOptionCount === activeIndex }"
              role="option"
              :aria-selected="model.id === selectedId ? 'true' : 'false'"
              @mousedown.prevent
              @click="selectModel(model.id)"
            >
              <span class="setup-model-combobox__identity">
                <span class="setup-model-combobox__id" :title="model.id">{{ model.id }}</span>
                <span class="setup-model-combobox__subline">
                  <span
                    v-if="model.name && model.name !== model.id"
                    class="setup-model-combobox__name"
                    :title="model.name"
                  >
                    {{ model.name }}
                  </span>
                  <span
                    v-if="rowStatus(model)"
                    class="setup-model-combobox__badge setup-model-combobox__badge--status"
                  >
                    {{ rowStatus(model) }}
                  </span>
                  <span
                    v-if="usesCatalogOnlyMetadata(model)"
                    class="setup-model-combobox__badge setup-model-combobox__badge--catalog"
                  >
                    {{ t('setup.provider.modelCatalogValue') }}
                  </span>
                </span>
              </span>
              <span class="setup-model-combobox__aside">
                <span v-if="rowMetrics(model).length" class="setup-model-combobox__meta">
                  <template v-for="(metric, metricIndex) in rowMetrics(model)" :key="metric.kind">
                    <span
                      v-if="metricIndex > 0"
                      class="setup-model-combobox__metric-separator"
                      aria-hidden="true"
                    >·</span>
                    <span class="setup-model-combobox__metric" :title="metric.accessibleLabel">
                      <span class="setup-model-combobox__sr-only">
                        {{ metric.accessibleLabel }}
                      </span>
                      <span class="setup-model-combobox__metric-label" aria-hidden="true">
                        {{ metric.shortLabel }}
                      </span>
                      <span class="setup-model-combobox__metric-value" aria-hidden="true">
                        {{ metric.tokens }}
                      </span>
                    </span>
                  </template>
                </span>
                <span v-if="model.id === selectedId" class="setup-model-combobox__selected">
                  <Icon name="check" :size="12" aria-hidden="true" />
                  <span class="setup-model-combobox__sr-only">
                    {{ t('setup.provider.modelSelected') }}
                  </span>
                </span>
              </span>
            </button>
            <button
              v-if="showFreeTextRow"
              :id="freeTextOptionId"
              type="button"
              class="setup-model-combobox__row setup-model-combobox__row--freetext"
              :class="{ 'is-active': activeIndex === visibleModels.length + leadingOptionCount }"
              role="option"
              aria-selected="false"
              @mousedown.prevent
              @click="selectTypedValue"
            >
              <span class="setup-model-combobox__id" :title="typedValue.trim()">{{ t('setup.provider.modelUseTyped', { value: typedValue.trim() }) }}</span>
            </button>
          </div>
          <div v-if="!leadingOption && !visibleModels.length && !showFreeTextRow" class="setup-model-combobox__footer">
            {{ t('setup.provider.modelNoMatches') }}
          </div>
          <div v-if="truncatedCount" class="setup-model-combobox__footer">
            {{ t('setup.provider.modelListTruncated', { shown: visibleModels.length, total: matches.length }) }}
          </div>
          <div v-if="provenance" class="setup-model-combobox__footer">{{ provenance }}</div>
        </div>
      </Teleport>
    </div>
  </div>
</template>

<style scoped>
/* Cell mode: the wrapper is a grid/table cell — the input fills it. No label
   chrome. The dropdown is teleported to <body>, so the cell never clips it. */
.setup-model-combobox--cellwrap {
  min-width: 0;
}

.setup-model-combobox--cellwrap .setup-model-combobox input {
  box-sizing: border-box;
  max-width: none;
  width: 100%;
}

.setup-model-combobox {
  min-width: 0;
  position: relative;
}

.setup-model-combobox.has-catalog input {
  padding-right: 36px;
}

.setup-model-combobox__label--with-info {
  align-items: center;
  display: inline-flex;
  gap: 5px;
}

.setup-model-combobox__info {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  justify-content: center;
  position: relative;
}

.setup-model-combobox__info:hover {
  color: var(--text);
}

.setup-model-combobox__info-tooltip {
  background: var(--text);
  border-radius: var(--radius-sm);
  bottom: calc(100% + 9px);
  box-shadow: var(--shadow-md);
  color: var(--bg-elevated);
  font-size: var(--fs-xs);
  font-weight: 400;
  left: 50%;
  line-height: 1.45;
  max-width: min(300px, 70vw);
  opacity: 0;
  padding: 7px 9px;
  pointer-events: none;
  position: absolute;
  text-align: left;
  transform: translateX(-50%);
  visibility: hidden;
  white-space: normal;
  width: max-content;
  z-index: 40;
}

.setup-model-combobox__info-tooltip::after {
  border: 5px solid transparent;
  border-top-color: var(--text);
  content: '';
  left: 50%;
  position: absolute;
  top: 100%;
  transform: translateX(-50%);
}

.setup-model-combobox__info:hover .setup-model-combobox__info-tooltip,
.setup-model-combobox__info:focus-visible .setup-model-combobox__info-tooltip {
  opacity: 1;
  visibility: visible;
}

.setup-model-combobox__info:focus-visible {
  border-radius: var(--radius-full);
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.setup-model-combobox__trigger {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  bottom: 2px;
  color: var(--text-dim);
  cursor: pointer;
  display: inline-flex;
  justify-content: center;
  padding: 0;
  position: absolute;
  right: 2px;
  top: 2px;
  width: 32px;
}

.setup-model-combobox__trigger:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.setup-model-combobox__trigger:focus-visible {
  box-shadow: var(--focus-ring);
  outline: none;
}

.setup-model-combobox__chevron {
  transition: transform var(--dur-fast) var(--ease-standard);
}

.setup-model-combobox__trigger[aria-expanded='true'] .setup-model-combobox__chevron {
  transform: rotate(180deg);
}

.setup-model-combobox__sr-only {
  clip: rect(0, 0, 0, 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}

.setup-model-combobox__popup {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* Teleported to <body>; left/top/bottom/width/max-height come from the
     inline style computed off the input's viewport rect. Keep this above both
     the settings dialog (300) and its nested provider modal overlay (420), so
     the list remains interactive and is never visually clipped by the modal. */
  position: fixed;
  z-index: 440;
}

.setup-model-combobox__list {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
  overscroll-behavior: contain;
  overflow-y: auto;
}

.setup-model-combobox__readout {
  align-items: center;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  display: flex;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  gap: var(--sp-2);
  justify-content: space-between;
  padding: var(--sp-2) var(--sp-3);
  flex-shrink: 0;
}

.setup-model-combobox__live-dot {
  background: var(--info);
  border-radius: var(--radius-pill);
  height: 6px;
  width: 6px;
}

.setup-model-combobox__live-source {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  gap: var(--sp-1);
  white-space: nowrap;
}

.setup-model-combobox__row {
  align-items: center;
  background: none;
  border: none;
  color: var(--text);
  cursor: pointer;
  display: grid;
  font: inherit;
  gap: var(--sp-2);
  grid-template-columns: minmax(0, 1fr) auto;
  min-height: 48px;
  padding: var(--sp-2) var(--sp-3);
  text-align: left;
}

.setup-model-combobox__row:hover,
.setup-model-combobox__row.is-active {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}

.setup-model-combobox__row[aria-selected='true'] {
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  box-shadow: inset 2px 0 0 var(--accent);
}

.setup-model-combobox__row--freetext {
  border-top: 1px solid var(--border);
  color: var(--text-muted);
}

/* The row matching the field's current value, visible when the full list is
   shown over a saved model id. */
.setup-model-combobox__row[aria-selected='true'] .setup-model-combobox__id {
  color: var(--accent);
}

.setup-model-combobox__id {
  display: block;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.setup-model-combobox__identity {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.setup-model-combobox__subline {
  align-items: center;
  display: flex;
  gap: var(--sp-1);
  min-width: 0;
}

.setup-model-combobox__subline:empty {
  display: none;
}

.setup-model-combobox__name {
  color: var(--text-dim);
  display: block;
  flex: 0 1 auto;
  font-size: var(--fs-xs);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.setup-model-combobox__meta {
  align-items: center;
  color: var(--text-dim);
  display: inline-flex;
  font-size: var(--fs-xs);
  gap: var(--sp-1);
  min-width: 0;
}

.setup-model-combobox__aside,
.setup-model-combobox__selected,
.setup-model-combobox__metric {
  align-items: center;
  display: inline-flex;
  gap: var(--sp-1);
}

.setup-model-combobox__aside {
  justify-self: end;
  min-width: 0;
}

.setup-model-combobox__selected {
  color: var(--accent);
  flex: 0 0 auto;
  font-size: var(--fs-xs);
}

.setup-model-combobox__metric,
.setup-model-combobox__metric-value {
  white-space: nowrap;
}

.setup-model-combobox__metric-value {
  font-variant-numeric: tabular-nums;
}

.setup-model-combobox__metric-separator {
  color: var(--border-strong);
}

.setup-model-combobox__badge {
  border-radius: var(--radius-pill);
  flex: 0 0 auto;
  font-size: var(--fs-xs);
  padding: 1px var(--sp-1);
  white-space: nowrap;
}

.setup-model-combobox__badge--status {
  background: color-mix(in srgb, var(--warn) 12%, transparent);
  color: var(--warn);
}

.setup-model-combobox__badge--catalog {
  background: var(--bg-hover);
  color: var(--text-dim);
}

.setup-model-combobox__footer {
  border-top: 1px solid var(--border);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  padding: var(--sp-1) var(--sp-3);
  flex-shrink: 0;
}

@media (max-width: 520px) {
  .setup-model-combobox__metric-label {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .setup-model-combobox__chevron {
    transition: none;
  }
}
</style>
