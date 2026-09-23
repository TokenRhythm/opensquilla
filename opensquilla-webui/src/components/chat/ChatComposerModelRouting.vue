<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { ModelDescriptor, ProviderListError } from '@/modules/providerConfiguration'
import type { ModelRoutingMode } from '@/types/modelRouting'
import { calculateModelRoutingPlacement } from './modelRoutingPlacement'
const props = withDefaults(
  defineProps<{
    anchor?: HTMLElement | null
    modelRoutingMode: ModelRoutingMode
    busy: boolean
    routingAvailable?: boolean
    isNewTask?: boolean
    modelSelectionAvailable?: boolean
    availableModels?: readonly ModelDescriptor[]
    modelSelection?: { model: string; provider: string | null } | null
    defaultModel?: { model: string; provider: string } | null
    sessionModelName?: string | null
    modelsLoading?: boolean
    modelsError?: string | null
    modelProviderErrors?: readonly ProviderListError[]
    modelSelectionDisabledReason?: 'routing' | 'busy' | 'unavailable' | null
  }>(),
  { routingAvailable: true, isNewTask: true },
)
const emit = defineEmits<{
  close: [restoreFocus?: boolean]
  setSessionRoutingMode: [mode: ModelRoutingMode]
  selectModel: [selection: { model: string; provider: string } | null]
  openModelSettings: []
  refreshModels: []
}>()
const { t } = useI18n()
const id = useId()
const rootRef = ref<HTMLElement | null>(null)
const primaryRef = ref<HTMLElement | null>(null)
const searchRef = ref<HTMLInputElement | null>(null)
const showAllRef = ref<HTMLButtonElement | null>(null)
const singleRef = ref<HTMLButtonElement | null>(null)
const search = ref('')
const allModelsExpanded = ref(false)
const MODELS_PER_PROVIDER = 12
const submenuOpen = ref(false)
const activeModel = ref(-1)
const compact = ref(false)
const position = ref({ left: '12px', bottom: '12px', width: '224px', '--routing-height': '400px' })
const submenuPosition = ref({ left: '12px', top: '12px', height: '360px' })
const submenuSide = ref<'left' | 'right' | 'compact'>('right')
const hasModelPicker = computed(() =>
  Boolean(props.modelSelectionAvailable || props.modelSelection),
)
type RoutingModeOption = {
  value: ModelRoutingMode
  label: string
  badge?: string
  description: string
}
const modes = computed<readonly RoutingModeOption[]>(() => [
  {
    value: 'off',
    label: t('chat.modelRouting.direct'),
    description: t('chat.composer.modelRoutingOffDesc'),
  },
  {
    value: 'squilla_router',
    label: t('chat.modelRouting.router'),
    badge: t('setup.modelStrategy.cards.router.badge'),
    description: t('chat.composer.modelRoutingSquillaRouterDesc'),
  },
  {
    value: 'llm_ensemble',
    label: t('chat.modelRouting.ensemble'),
    badge: t('setup.modelStrategy.cards.ensemble.badge'),
    description: t('chat.composer.modelRoutingEnsembleDesc'),
  },
])
const key = (model: { model: string; provider: string | null }) =>
  JSON.stringify([model.provider, model.model])
const selectedKey = computed(() =>
  props.modelSelection ? key(props.modelSelection) : 'default',
)
type ModelOption = { key: string; label: string; model: string; provider: string | null }
const models = computed(() => {
  const catalog = new Map<string, ModelOption>(
    (props.availableModels ?? []).map((model) => [
      key({ model: model.id, provider: model.provider }),
      {
        key: key({ model: model.id, provider: model.provider }),
        label: model.name || model.id,
        model: model.id,
        provider: model.provider,
      },
    ]),
  )
  const selection = props.modelSelection
  if (selection && !catalog.has(selectedKey.value))
    catalog.set(selectedKey.value, {
      key: selectedKey.value,
      label: selection.model,
      ...selection,
    })
  const providers = new Map<string | null, ModelOption[]>()
  for (const model of catalog.values()) {
    const group = providers.get(model.provider) ?? []
    group.push(model)
    providers.set(model.provider, group)
  }
  return [
    {
      key: 'default',
      label: t('chat.newTaskModel.gatewayDefault'),
      model: '',
      provider: '',
    },
    ...[
      ...(props.defaultModel && providers.has(props.defaultModel.provider)
        ? [providers.get(props.defaultModel.provider)!] : []),
      ...[...providers.entries()]
        .filter(([provider]) => provider !== props.defaultModel?.provider)
        .map(([, group]) => group),
    ].flatMap((group) => {
      const primaryKey = props.defaultModel ? key(props.defaultModel) : null
      const primary = group.find((model) => model.key === primaryKey)
      return primary ? [primary, ...group.filter((model) => model !== primary)] : group
    }),
  ]
})
const pickerTitle = computed(() => t(props.isNewTask ? 'chat.newTaskModel.title' : 'chat.modelRouting.sessionModelTitle'))
const pickerHint = computed(() => t(props.modelSelectionDisabledReason === 'busy' && !props.isNewTask
  ? 'chat.modelRouting.sessionModelBusy'
  : props.isNewTask ? 'chat.newTaskModel.scope' : 'chat.modelRouting.sessionModelHint'))
const defaultModelName = computed(() => {
  const model = props.defaultModel
  return model
    ? props.availableModels?.find(
        (item) => item.id === model.model && item.provider === model.provider,
      )?.name || model.model
    : ''
})
const selectedModelLabel = computed(() => {
  if (props.modelRoutingMode !== 'off') return undefined
  if (!hasModelPicker.value) return props.sessionModelName || t('chat.modelRouting.sessionModelFallback')
  return props.modelSelection
    ? models.value.find((model) => model.key === selectedKey.value)?.label
    : defaultModelName.value || t('chat.newTaskModel.gatewayDefault')
})
const defaultModelHint = computed(() =>
  defaultModelName.value
    ? `${defaultModelName.value} · ${props.defaultModel!.provider}`
    : t('chat.newTaskModel.gatewayDefaultHint'),
)
const filteredModels = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  if (query) return models.value.filter((model) =>
    `${model.label} ${model.provider} ${model.model}`.toLocaleLowerCase().includes(query),
  )
  if (allModelsExpanded.value) return models.value
  const groups = new Map<string | null, ModelOption[]>()
  for (const model of models.value) {
    if (model.key === 'default') continue
    const group = groups.get(model.provider) ?? []
    group.push(model)
    groups.set(model.provider, group)
  }
  const visible = new Set(['default'])
  for (const group of groups.values()) {
    const initial = group.slice(0, MODELS_PER_PROVIDER)
    const selected = group.find((model) => model.key === selectedKey.value)
    // Keep a saved choice visible without exceeding the provider's budget or
    // changing the relative order of the other returned models.
    if (selected && !initial.includes(selected)) initial[MODELS_PER_PROVIDER - 1] = selected
    initial.forEach((model) => visible.add(model.key))
  }
  return models.value.filter((model) => visible.has(model.key))
})
const hasHiddenModels = computed(() => !search.value.trim() && filteredModels.value.length < models.value.length)
async function showAllModels() {
  allModelsExpanded.value = true
  await nextTick()
  searchRef.value?.focus({ preventScroll: true })
  if (activeModel.value >= 0)
    document.getElementById(`${id}-model-${activeModel.value}`)?.scrollIntoView({ block: 'nearest' })
}
// Provider sections stay inside the existing second level; search and keyboard
// navigation continue to operate on one flat list of model identities.
const modelGroups = computed(() => {
  const groups = new Map<string, { key: string; provider: string | null; rows: { model: ModelOption; index: number }[] }>()
  filteredModels.value.forEach((model, index) => {
    const provider = model.key === 'default' ? null : model.provider
    const groupKey = model.key === 'default' ? 'default' : JSON.stringify(provider)
    let group = groups.get(groupKey)
    if (!group) {
      group = { key: groupKey, provider, rows: [] }
      groups.set(groupKey, group)
    }
    group.rows.push({ model, index })
  })
  return [...groups.values()]
})
const showProviderGroups = computed(() => new Set(
  models.value.filter((model) => model.key !== 'default' && model.provider).map((model) => model.provider),
).size > 1)
const issue = computed(() => {
  if (props.modelSelectionDisabledReason === 'unavailable') return t('chat.newTaskModel.unavailable')
  if (props.modelsError && !props.availableModels?.length) return props.modelsError
  // A compatible last-good catalog remains usable during a discovery outage.
  // Only surface provider failures that actually leave its options unavailable.
  const failures = (props.modelProviderErrors ?? []).filter((error) =>
    !props.availableModels?.some((model) => model.provider === error.provider),
  )
  return failures.length
    ? t('chat.newTaskModel.partialFailure', {
        providers: failures.map((error) => error.provider).join(', '),
      })
    : ''
})
function modelDisabled(model: (typeof models.value)[number]) {
  return (
    props.busy ||
    props.modelSelectionDisabledReason === 'busy' ||
    (model.key !== 'default' && !model.provider) ||
    (!props.modelSelectionAvailable && (!props.isNewTask || model.key !== 'default'))
  )
}
function selectMode(mode: ModelRoutingMode) {
  if (props.busy || !props.routingAvailable) return
  emit('setSessionRoutingMode', mode)
  emit('close')
}
function selectModel(index: number) {
  const model = filteredModels.value[index]
  if (!model || modelDisabled(model) || (model.key !== 'default' && !model.provider)) return
  emit(
    'selectModel',
    model.key === 'default' ? null : { model: model.model, provider: model.provider! },
  )
  emit('close')
}
async function openModels(focus = false) {
  if (!hasModelPicker.value) return
  submenuOpen.value = true
  if (focus) {
    await nextTick()
    searchRef.value?.focus()
  }
}
function onSinglePointerEnter(event: PointerEvent) {
  if (event.pointerType === 'mouse' && !compact.value) void openModels()
}
function closeModels(focus = true) {
  submenuOpen.value = false
  if (focus) nextTick(() => singleRef.value?.focus())
}
function onPrimaryKey(event: KeyboardEvent) {
  if (event.isComposing) return
  const controls = Array.from(
    primaryRef.value?.querySelectorAll<HTMLButtonElement>('[role^="menuitem"]') ?? [],
  )
  const index = controls.indexOf(document.activeElement as HTMLButtonElement)
  if (
    event.key === 'ArrowRight' &&
    document.activeElement === singleRef.value &&
    hasModelPicker.value
  ) {
    event.preventDefault()
    void openModels(true)
    return
  }
  let next = index
  if (event.key === 'ArrowDown') next = (index + 1) % controls.length
  else if (event.key === 'ArrowUp') next = (index - 1 + controls.length) % controls.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = controls.length - 1
  else return
  event.preventDefault()
  controls[next]?.focus()
}
function onSearchKey(event: KeyboardEvent) {
  if (event.isComposing) return
  const available = filteredModels.value
    .map((model, index) => (modelDisabled(model) ? -1 : index))
    .filter((index) => index >= 0)
  if (event.key === 'ArrowLeft' && !search.value) {
    event.preventDefault()
    closeModels()
    return
  }
  if (event.key === 'Enter') {
    event.preventDefault()
    selectModel(activeModel.value)
    return
  }
  let next = activeModel.value
  if (event.key === 'ArrowDown')
    next = available[(available.indexOf(next) + 1) % available.length] ?? -1
  else if (event.key === 'ArrowUp') {
    const current = available.indexOf(next)
    next =
      available[
        current < 0 ? available.length - 1 : (current - 1 + available.length) % available.length
      ] ?? -1
  } else if (event.key === 'Home' && event.ctrlKey) next = available[0] ?? -1
  else if (event.key === 'End' && event.ctrlKey) next = available[available.length - 1] ?? -1
  else return
  event.preventDefault()
  activeModel.value = next
  nextTick(() =>
    document.getElementById(`${id}-model-${next}`)?.scrollIntoView({ block: 'nearest' }),
  )
}
function onKey(event: KeyboardEvent) {
  if (event.isComposing) return
  if (event.key === 'Escape') {
    event.stopPropagation()
    event.preventDefault()
    if (submenuOpen.value) closeModels()
    else emit('close')
  } else if (event.key === 'Tab') {
    if (!event.shiftKey && document.activeElement === searchRef.value && showAllRef.value) {
      event.preventDefault()
      showAllRef.value.focus()
      return
    }
    if (event.shiftKey && document.activeElement === showAllRef.value) {
      event.preventDefault()
      searchRef.value?.focus()
      return
    }
    props.anchor?.querySelector<HTMLButtonElement>('button')?.focus()
    emit('close', false)
  }
}
watch(search, () => {
  activeModel.value = -1
})
watch(filteredModels, (next, previous) => {
  const activeKey = previous[activeModel.value]?.key
  activeModel.value = activeKey ? next.findIndex((model) => model.key === activeKey) : -1
})
// Sample only while this popover is mounted: position can change without the
// trigger resizing (sidebar, composer growth, client zoom, visual viewport pan).
let frame = 0
let primaryHeight = 0
function placeMenu() {
  const viewport = window.visualViewport
  const width = viewport?.width ?? window.innerWidth
  const height = viewport?.height ?? window.innerHeight
  const left = viewport?.offsetLeft ?? 0
  const top = viewport?.offsetTop ?? 0
  const anchor = props.anchor?.getBoundingClientRect() ?? {
    left: left + width - 12,
    right: left + width - 12,
    top: top + height - 24,
    bottom: top + height - 24,
  }
  const primary = primaryRef.value?.getBoundingClientRect()
  // A drilled-in mobile menu hides the primary; retain its last visible height.
  if (primary?.height) primaryHeight = primary.height
  const single = singleRef.value?.getBoundingClientRect()
  const placement = calculateModelRoutingPlacement({
    viewport: { left, top, width, height },
    anchor,
    primaryHeight,
    singleRowTop: single?.height ? single.top : undefined,
  })
  compact.value = placement.compact
  submenuSide.value = placement.submenuSide
  const nextPosition = {
    left: `${placement.primaryLeft}px`,
    bottom: `${window.innerHeight - placement.primaryBottom}px`,
    width: `${placement.width}px`,
    '--routing-height': `${placement.availableHeight}px`,
  }
  const nextSubmenuPosition = {
    left: `${placement.submenuLeft}px`,
    top: `${placement.submenuTop}px`,
    height: `${placement.submenuHeight}px`,
  }
  if (
    Object.entries(nextPosition).some(
      ([name, value]) => position.value[name as keyof typeof nextPosition] !== value,
    )
  )
    position.value = nextPosition
  if (
    Object.entries(nextSubmenuPosition).some(
      ([name, value]) => submenuPosition.value[name as keyof typeof nextSubmenuPosition] !== value,
    )
  )
    submenuPosition.value = nextSubmenuPosition
}
function followAnchor() {
  placeMenu()
  frame = requestAnimationFrame(followAnchor)
}
watch(hasModelPicker, (available) => {
  if (!available) closeModels(false)
})
watch(compact, (isCompact) => {
  if (isCompact && submenuOpen.value && primaryRef.value?.contains(document.activeElement))
    nextTick(() => searchRef.value?.focus())
})
onMounted(() => {
  followAnchor()
  primaryRef.value
    ?.querySelector<HTMLButtonElement>(`[data-mode="${props.modelRoutingMode}"]`)
    ?.focus()
})
onBeforeUnmount(() => cancelAnimationFrame(frame))
defineExpose({ element: () => rootRef.value })
</script>

<template>
  <Teleport to="body">
    <div
      ref="rootRef"
      class="composer-model-routing"
      :class="{ 'is-compact': compact, 'is-drilled': compact && submenuOpen && hasModelPicker }"
      :style="position"
      role="dialog"
      :aria-label="t('chat.modelRouting.title')"
      :aria-busy="busy"
      @keydown="onKey"
    >
      <section ref="primaryRef" class="routing-primary">
        <header class="routing-heading">
          <strong>{{ t('chat.modelRouting.title') }}</strong>
          <button
            class="routing-icon-button"
            type="button"
            :aria-label="t('chat.closeComposerSettings')"
            @click="emit('close')"
          >
            <Icon name="x" :size="16" />
          </button>
        </header>
        <div
          role="menu"
          :aria-label="t('chat.modelRouting.title')"
          class="routing-modes"
          @keydown="onPrimaryKey"
        >
          <div class="routing-mode-list">
            <button
              v-for="(option, index) in modes"
              :key="option.value"
              :ref="
                (el) => {
                  if (index === 0) singleRef = el as HTMLButtonElement
                }
              "
              type="button"
              class="routing-mode"
              :data-mode="option.value"
              :aria-description="option.description"
              :class="{
                'is-selected': modelRoutingMode === option.value,
                'is-expanded': index === 0 && submenuOpen,
              }"
              :role="index === 0 && hasModelPicker ? 'menuitem' : 'menuitemradio'"
              :aria-checked="
                index === 0 && hasModelPicker ? undefined : modelRoutingMode === option.value
              "
              :aria-haspopup="index === 0 && hasModelPicker ? 'listbox' : undefined"
              :aria-expanded="index === 0 && hasModelPicker ? submenuOpen : undefined"
              :aria-controls="index === 0 && hasModelPicker ? `${id}-models` : undefined"
              :aria-disabled="busy || (!routingAvailable && !(index === 0 && hasModelPicker))"
              @pointerenter="index === 0 && onSinglePointerEnter($event)"
              @click="index === 0 && hasModelPicker ? openModels(true) : selectMode(option.value)"
            >
              <span class="routing-mode__top">
                <span class="routing-mode__label-group">
                  <span class="routing-mode__label">{{ option.label }}</span>
                  <span v-if="option.badge" class="routing-mode__benefit">{{ option.badge }}</span>
                </span>
                <span class="routing-mode__indicators">
                  <Icon v-if="modelRoutingMode === option.value" name="check" :size="14" />
                  <Icon
                    v-if="index === 0 && hasModelPicker"
                    :name="submenuSide === 'left' ? 'chevronLeft' : 'chevronRight'"
                    :size="15"
                  />
                </span>
              </span>
              <span
                v-if="index === 0 && selectedModelLabel"
                class="routing-mode__model"
                :title="selectedModelLabel"
              >
                <span class="routing-mode__model-dot" aria-hidden="true" />
                <span class="routing-mode__model-name">{{ selectedModelLabel }}</span>
                <span
                  v-if="hasModelPicker && !modelSelection && defaultModelName"
                  class="routing-mode__default"
                  >{{ t('chat.newTaskModel.defaultBadge') }}</span
                >
              </span>
            </button>
          </div>
          <button
            type="button"
            role="menuitem"
            class="routing-settings"
            @click="emit('openModelSettings')"
          >
            <Icon name="settings" :size="16" />
            <span>{{ t('chat.modelRouting.manage') }}</span>
            <Icon name="chevronRight" :size="14" />
          </button>
        </div>
        <p v-if="!hasModelPicker" class="routing-scope">
          {{ t('chat.composer.modelRoutingSessionScope') }}
        </p>
      </section>
      <section
        v-if="submenuOpen && hasModelPicker"
        class="new-task-model-menu"
        :data-side="submenuSide"
        :style="compact ? undefined : submenuPosition"
        :aria-label="pickerTitle"
      >
        <header class="routing-heading">
          <button
            v-if="compact"
            class="routing-icon-button"
            type="button"
            :aria-label="t('chat.modelRouting.back')"
            @click="closeModels()"
          >
            <Icon name="chevronLeft" :size="16" />
          </button>
          <strong>{{ pickerTitle }}</strong>
          <span class="routing-catalog-label">{{ t('chat.newTaskModel.catalog') }}</span>
        </header>
        <label class="routing-search">
          <Icon name="search" :size="16" />
          <input
            ref="searchRef"
            v-model="search"
            type="text"
            role="combobox"
            :aria-label="t('chat.newTaskModel.search')"
            :placeholder="t('chat.newTaskModel.search')"
            aria-autocomplete="list"
            aria-expanded="true"
            :aria-controls="`${id}-models`"
            :aria-activedescendant="activeModel >= 0 ? `${id}-model-${activeModel}` : undefined"
            autocomplete="off"
            @keydown="onSearchKey"
          />
        </label>
        <div class="routing-models">
          <div
            :id="`${id}-models`"
            role="listbox"
            :aria-label="pickerTitle"
            :aria-busy="modelsLoading"
          >
            <div
              v-for="(group, groupIndex) in modelGroups"
              :key="group.key"
              role="group"
              :aria-labelledby="showProviderGroups && group.provider ? `${id}-provider-${groupIndex}` : undefined"
            >
              <div
                v-if="showProviderGroups && group.provider"
                :id="`${id}-provider-${groupIndex}`"
                class="routing-provider-heading"
              >{{ group.provider }}</div>
              <button
                v-for="{ model, index } in group.rows"
                :id="`${id}-model-${index}`"
                :key="model.key"
                type="button"
                role="option"
                tabindex="-1"
                :aria-selected="modelRoutingMode === 'off' && model.key === selectedKey"
                :aria-disabled="modelDisabled(model)"
                class="routing-model"
                :class="{ 'is-highlighted': activeModel === index }"
                @pointermove="activeModel = index"
                @mousedown.prevent
                @click="selectModel(index)"
              >
                <span class="routing-model__avatar" aria-hidden="true">
                  <Icon v-if="model.key === 'default'" name="settings" :size="15" />
                  <template v-else>{{ model.label.charAt(0).toUpperCase() }}</template>
                </span>
                <span class="routing-model__copy">
                  <span class="routing-model__name">{{ model.label }}</span>
                  <span class="routing-model__provider">
                    {{ model.key === 'default' ? defaultModelHint : model.provider }}
                  </span>
                </span>
                <Icon
                  v-if="modelRoutingMode === 'off' && model.key === selectedKey"
                  name="check"
                  :size="16"
                />
              </button>
            </div>
            <p v-if="!filteredModels.length" class="routing-empty" role="status">
              {{
                modelsLoading ? t('chat.newTaskModel.loading') : t('chat.newTaskModel.empty')
              }}
            </p>
          </div>
        </div>
        <button
          v-if="hasHiddenModels"
          ref="showAllRef"
          type="button"
          class="routing-show-all"
          @click="showAllModels"
        >{{ t('chat.newTaskModel.showAll') }}</button>
        <div v-if="issue || (modelsLoading && !availableModels?.length)" class="routing-issue" role="status">
          <span>{{ modelsLoading && !availableModels?.length ? t('chat.newTaskModel.loading') : issue }}</span>
          <button
            v-if="issue"
            type="button"
            class="routing-retry"
            :disabled="modelsLoading"
            @click="emit('refreshModels')"
          >
            {{ t('chat.newTaskModel.retry') }}
          </button>
        </div>
        <p class="routing-model-scope">{{ pickerHint }}</p>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.composer-model-routing {
  position: fixed;
  z-index: var(--z-popover, 1200);
  max-height: var(--routing-height);
  max-width: calc(100vw - 24px);
  color: var(--text);
  font-family: var(--font-sans);
}
.routing-primary,
.new-task-model-menu {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-panel);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xl);
  overflow: hidden;
}
.routing-primary {
  width: 100%;
  max-height: var(--routing-height);
}
.new-task-model-menu {
  position: fixed;
  width: 316px;
  height: min(360px, var(--routing-height));
  container: model-routing-menu / size;
  animation: routing-submenu-in var(--dur-fast) var(--ease-out);
}
.routing-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  min-height: 38px;
  padding: 7px 12px;
  font-size: var(--fs-sm);
}
.routing-heading strong {
  flex: 1;
  font-weight: 650;
}
.routing-icon-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  border: 0;
  border-radius: var(--radius-control);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.routing-icon-button:hover {
  background: var(--bg-hover);
  color: var(--text);
}
.routing-modes {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  padding: 0 8px 8px;
  overflow: hidden;
}
.routing-mode-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.routing-mode {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  width: 100%;
  padding: 9px 10px;
  margin-bottom: 2px;
  border: 1px solid transparent;
  border-radius: var(--radius-control);
  text-align: left;
  background: transparent;
  color: var(--text);
  cursor: pointer;
}
.routing-mode__top {
  width: 100%;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  font-size: var(--fs-sm);
  font-weight: 500;
}
.routing-mode__label-group {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  flex: 1 1 auto;
  min-width: 0;
  gap: 6px;
}
.routing-mode__label {
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: normal;
}
.routing-mode__benefit {
  flex: 0 0 auto;
  padding: 1px 5px;
  border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  border-radius: var(--radius-control);
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-surface));
  color: color-mix(in srgb, var(--accent) 80%, var(--text));
  font-size: 10px;
  font-weight: 500;
  line-height: 1.35;
  white-space: nowrap;
}
.routing-mode__indicators {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
}
.routing-mode__model {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  margin-top: 3px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.4;
  min-height: calc(1.4em + 2px);
}
.routing-mode__model-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.routing-mode__default {
  flex-shrink: 0;
  padding: 0 4px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text-dim);
}
.routing-mode__model-dot {
  flex: 0 0 4px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--accent);
}
.routing-mode:hover,
.routing-mode.is-expanded {
  background: var(--bg-hover);
}
.routing-mode.is-selected {
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--accent) 20%, var(--border));
}
.routing-mode[aria-disabled='true'],
.routing-model[aria-disabled='true'] {
  opacity: var(--state-disabled-opacity);
  cursor: default;
}
.routing-settings {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px;
  margin-top: 4px;
  flex-shrink: 0;
  border: 0;
  border-top: 1px solid var(--border);
  border-radius: 0;
  background: transparent;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  text-align: left;
  cursor: pointer;
}
.routing-settings > span:not(.icon) {
  flex: 1;
}
.routing-settings:hover {
  color: var(--text);
  background: var(--bg-hover);
}
.routing-scope,
.routing-model-scope {
  flex-shrink: 0;
  margin: 0;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  line-height: 1.5;
}
.routing-catalog-label {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}
.routing-search {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  height: 36px;
  margin: 0 10px 8px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  background: var(--bg);
  color: var(--text-muted);
}
.routing-search:focus-within {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.routing-search input[type='text'],
.routing-search input[type='text']:focus {
  min-width: 0;
  width: 100%;
  height: 100%;
  padding: 0;
  border: 0;
  outline: 0;
  box-shadow: none;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
}
.routing-models {
  flex: 1;
  min-height: 0;
  padding: 0 8px 8px;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.routing-show-all {
  flex-shrink: 0;
  width: 100%;
  min-height: 40px;
  padding: 10px 14px;
  border: 0;
  border-top: 1px solid var(--border);
  border-radius: 0;
  background: var(--bg-surface);
  color: var(--text-muted);
  font: inherit;
  font-size: var(--fs-xs);
  text-align: left;
  cursor: pointer;
}
.routing-show-all:hover {
  background: var(--bg-hover);
  color: var(--text);
}
.routing-provider-heading {
  padding: 10px 10px 4px;
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 500;
}
.routing-model {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 56px;
  padding: 9px 10px;
  border: 0;
  border-radius: var(--radius-control);
  color: var(--text);
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.routing-model:hover,
.routing-model.is-highlighted {
  background: var(--bg-hover);
}
.routing-model[aria-selected='true'] {
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-surface));
}
.routing-model__avatar {
  display: grid;
  place-items: center;
  flex: 0 0 28px;
  width: 28px;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.routing-model__copy {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.routing-model__name {
  font-size: var(--fs-sm);
  font-weight: 550;
}
.routing-model__provider {
  font-size: var(--fs-xs);
  color: var(--text-muted);
}
.routing-model__name,
.routing-model__provider {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.routing-empty {
  padding: 20px 12px;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}
.routing-issue {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px;
  color: var(--warn);
  font-size: var(--fs-xs);
}
.routing-issue span {
  flex: 1;
}
.routing-retry {
  padding: 4px;
  border: 0;
  background: transparent;
  color: var(--accent);
  font: inherit;
  cursor: pointer;
}
button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.is-compact .new-task-model-menu {
  position: static;
  width: 100%;
}
@container model-routing-menu (max-height: 220px) {
  .routing-model-scope {
    display: none;
  }
}
@container model-routing-menu (max-height: 150px) {
  .routing-heading {
    min-height: 32px;
    padding-block: 4px;
  }
  .routing-search {
    height: 32px;
    margin-bottom: 4px;
  }
  .routing-show-all {
    min-height: 36px;
    padding-block: 8px;
  }
}
.is-drilled .routing-primary {
  display: none;
}
@keyframes routing-submenu-in {
  from {
    opacity: 0;
    transform: translateX(-4px);
  }
  to {
    opacity: 1;
    transform: translateX(0);
  }
}
@media (prefers-reduced-motion: reduce) {
  .new-task-model-menu {
    animation: none;
  }
}
</style>
