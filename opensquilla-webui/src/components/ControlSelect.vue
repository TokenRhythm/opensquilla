<script setup lang="ts">
import { computed, nextTick, normalizeClass, onBeforeUnmount, onMounted, onUpdated, ref, useAttrs, useId, watch, type StyleValue } from 'vue'
import Icon from '@/components/Icon.vue'

defineOptions({ inheritAttrs: false })
type SelectValue = string | number | boolean
interface Choice { value: string; label: string; disabled: boolean; group: string; index: number }
const props = withDefaults(defineProps<{
  value?: SelectValue
  modelValue?: SelectValue
  disabled?: boolean
  placeholder?: string
  id?: string
  name?: string
}>(), { value: undefined, modelValue: undefined, disabled: false })
const emit = defineEmits<{
  'update:modelValue': [value: SelectValue]
  change: [event: Event]
}>()
const attrs = useAttrs()
const uid = useId()
const triggerId = computed(() => props.id || `control-select-${uid}`)
const listId = `control-select-list-${uid}`
const root = ref<HTMLElement>()
const native = ref<HTMLSelectElement>()
const trigger = ref<HTMLButtonElement>()
const menu = ref<HTMLElement>()
const open = ref(false)
const active = ref(-1)
const choices = ref<Choice[]>([])
const inheritedDisabled = ref(false)
const disabled = computed(() => Boolean(props.disabled || inheritedDisabled.value))
const current = computed(() => props.modelValue !== undefined ? props.modelValue : props.value)
const selected = computed(() => choices.value.find(choice => choice.value === String(current.value ?? '')))
const selectedLabel = computed(() => selected.value?.label || props.placeholder || String(current.value ?? ''))
const menuStyle = ref<Record<string, string>>({})
const triggerAttrs = () => Object.fromEntries(
  Object.entries(attrs).filter(([name]) => name !== 'class' && name !== 'style'),
)
const rootClasses = () => normalizeClass(attrs.class).split(/\s+/).map(name => name === 'control-input' ? 'is-standard' : name)
const groups = computed(() => {
  const result: Array<{ label: string; choices: Choice[] }> = []
  for (const choice of choices.value) {
    if (!result.length || result[result.length - 1]!.label !== choice.group) result.push({ label: choice.group, choices: [] })
    result[result.length - 1]!.choices.push(choice)
  }
  return result
})
let typeahead = ''
let typedAt = 0
const composing = ref(false)
let resizeObserver: ResizeObserver | undefined
let fieldsetObserver: MutationObserver | undefined

// Keep the native form contract (including optgroups, names and numeric Vue
// option values). Only the visible, themed combobox participates in focus/AT.
function syncChoices() {
  const element = native.value
  if (!element) return
  // Slot children can be patched after the select's value property. Reapply
  // the controlled value after their mount/update, including disabled options.
  const expected = String(current.value ?? '')
  if (element.value !== expected) element.value = expected
  const next = Array.from(element.options).map((option, index) => ({
    value: option.value,
    label: option.label || (option.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: option.disabled || (option.parentElement instanceof HTMLOptGroupElement && option.parentElement.disabled),
    group: option.parentElement instanceof HTMLOptGroupElement ? option.parentElement.label : '',
    index,
  }))
  const previousActiveValue = choices.value[active.value]?.value
  const choicesChanged = JSON.stringify(next) !== JSON.stringify(choices.value)
  if (choicesChanged) choices.value = next
  let fieldsetDisabled = false
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (parent instanceof HTMLFieldSetElement && parent.disabled) {
      const legend = Array.from(parent.children).find(child => child.tagName === 'LEGEND')
      if (!legend?.contains(element)) fieldsetDisabled = true
    }
  }
  inheritedDisabled.value = fieldsetDisabled
  if (disabled.value || (open.value && !enabled().length)) close()
  else if (open.value) {
    const retained = next.find(choice => choice.value === previousActiveValue && !choice.disabled)
    const fallback = selected.value && !selected.value.disabled ? selected.value : enabled()[0]
    active.value = retained?.index ?? fallback?.index ?? -1
    if (choicesChanged) void nextTick(() => { positionMenu(); revealActive() })
  }
}

function enabled() { return choices.value.filter(choice => !choice.disabled) }

function positionMenu() {
  if (!open.value || !trigger.value || !menu.value) return
  const anchor = trigger.value.getBoundingClientRect()
  const viewport = window.visualViewport
  const leftEdge = (viewport?.offsetLeft ?? 0) + 8
  const topEdge = (viewport?.offsetTop ?? 0) + 8
  const rightEdge = (viewport?.offsetLeft ?? 0) + (viewport?.width ?? window.innerWidth) - 8
  const bottomEdge = (viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight) - 8
  const below = Math.max(0, bottomEdge - Math.max(topEdge, anchor.bottom) - 4)
  const above = Math.max(0, Math.min(bottomEdge, anchor.top) - topEdge - 4)
  const desiredHeight = Math.min(320, menu.value.scrollHeight || 320)
  const aboveAnchor = below < desiredHeight && above > below
  const availableHeight = Math.max(0, aboveAnchor ? above : below)
  const height = Math.min(desiredHeight, availableHeight)
  const width = Math.min(Math.max(anchor.width, 240), Math.max(0, rightEdge - leftEdge))
  const left = Math.max(leftEdge, Math.min(anchor.left, rightEdge - width))
  const top = Math.max(topEdge, Math.min(bottomEdge - height,
    aboveAnchor ? anchor.top - 4 - height : anchor.bottom + 4))
  menuStyle.value = { left: `${left}px`, top: `${top}px`, width: `${width}px`, maxHeight: `${Math.min(320, availableHeight)}px` }
}

function revealActive() {
  void nextTick(() => {
    const list = menu.value
    const option = list?.querySelector<HTMLElement>(`[data-index="${active.value}"]`)
    if (!list || !option) return
    const optionRect = option.getBoundingClientRect()
    const listRect = list.getBoundingClientRect()
    // Scroll only the list: scrollIntoView can also move the settings panel.
    if (optionRect.top < listRect.top) list.scrollTop -= listRect.top - optionRect.top
    else if (optionRect.bottom > listRect.bottom) list.scrollTop += optionRect.bottom - listRect.bottom
  })
}

async function show(edge?: 'first' | 'last') {
  syncChoices()
  if (disabled.value || !enabled().length) return
  active.value = edge === 'last' ? enabled()[enabled().length - 1]!.index
    : edge === 'first' ? enabled()[0]!.index
    : selected.value && !selected.value.disabled ? selected.value.index : enabled()[0]!.index
  open.value = true
  await nextTick()
  // A manual popover stays in this DOM subtree (and the settings focus trap),
  // while the browser top layer prevents a scrolling panel clipping its menu.
  if (menu.value?.showPopover) menu.value.showPopover()
  positionMenu()
  revealActive()
}

function close() {
  if (menu.value?.matches(':popover-open')) menu.value.hidePopover?.()
  open.value = false
  typeahead = ''
}

function onChange(event: Event) {
  const option = native.value?.selectedOptions[0] as (HTMLOptionElement & { _value?: SelectValue }) | undefined
  emit('update:modelValue', option && '_value' in option ? option._value! : native.value?.value ?? '')
  emit('change', event)
}

function choose(choice: Choice) {
  if (disabled.value || choice.disabled || !native.value) return
  const changed = native.value.value !== choice.value
  native.value.value = choice.value
  if (changed) native.value.dispatchEvent(new Event('change', { bubbles: true }))
  close()
  trigger.value?.focus({ preventScroll: true })
}

function move(direction: number) {
  const values = enabled()
  if (!values.length) return
  const index = values.findIndex(choice => choice.index === active.value)
  active.value = values[Math.max(0, Math.min(values.length - 1, index + direction))]!.index
  revealActive()
}

function onKeydown(event: KeyboardEvent) {
  if (disabled.value || composing.value || event.isComposing || event.keyCode === 229) return
  if (event.key === 'Tab') { close(); return }
  if (event.key === 'Escape') {
    if (open.value) { event.preventDefault(); event.stopPropagation(); close() }
    return
  }
  if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' '].includes(event.key)) {
    event.preventDefault()
    if (!open.value) {
      void show(event.key === 'ArrowUp' || event.key === 'End' ? 'last' : event.key === 'Home' ? 'first' : undefined)
    } else if (event.key === 'Enter' || event.key === ' ') {
      const choice = choices.value[active.value]
      if (choice) choose(choice)
    } else if (event.key === 'Home' || event.key === 'End') {
      active.value = (event.key === 'Home' ? enabled()[0] : enabled()[enabled().length - 1])?.index ?? -1
      revealActive()
    } else move(event.key === 'ArrowDown' ? 1 : -1)
    return
  }
  if (event.key.length !== 1 || event.ctrlKey || event.metaKey || event.altKey) return
  event.preventDefault()
  const now = Date.now()
  typeahead = now - typedAt < 700 ? typeahead + event.key : event.key
  typedAt = now
  const query = /^(.)\1+$/u.test(typeahead) ? event.key : typeahead
  const values = enabled()
  const start = values.findIndex(choice => choice.index === active.value)
  const ordered = [...values.slice(start + 1), ...values.slice(0, start + 1)]
  const match = ordered.find(choice => choice.label.toLocaleLowerCase().startsWith(query.toLocaleLowerCase()))
  if (!open.value) void show()
  if (match) { active.value = match.index; revealActive() }
}

function outside(event: Event) { if (!root.value?.contains(event.target as Node)) close() }
function focusout(event: FocusEvent) { if (!root.value?.contains(event.relatedTarget as Node)) close() }

watch(() => props.disabled, value => { if (value) close() })
watch(current, () => { if (open.value) active.value = selected.value?.index ?? enabled()[0]?.index ?? -1 })
onUpdated(syncChoices)
onMounted(() => {
  syncChoices()
  document.addEventListener('pointerdown', outside)
  window.addEventListener('resize', positionMenu)
  window.addEventListener('scroll', positionMenu, true)
  window.visualViewport?.addEventListener('resize', positionMenu)
  window.visualViewport?.addEventListener('scroll', positionMenu)
  if (typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(positionMenu)
    if (trigger.value) resizeObserver.observe(trigger.value)
  }
  fieldsetObserver = new MutationObserver(syncChoices)
  for (let parent = root.value?.parentElement; parent; parent = parent.parentElement) {
    if (parent instanceof HTMLFieldSetElement) fieldsetObserver.observe(parent, { attributes: true, attributeFilter: ['disabled'] })
  }
})
onBeforeUnmount(() => {
  close()
  resizeObserver?.disconnect()
  fieldsetObserver?.disconnect()
  document.removeEventListener('pointerdown', outside)
  window.removeEventListener('resize', positionMenu)
  window.removeEventListener('scroll', positionMenu, true)
  window.visualViewport?.removeEventListener('resize', positionMenu)
  window.visualViewport?.removeEventListener('scroll', positionMenu)
})
</script>

<template>
  <span ref="root" class="control-select" :class="rootClasses()" :style="attrs.style as StyleValue" @focusout="focusout">
    <select ref="native" class="control-select__native" hidden :name="name" :value="current" :disabled="props.disabled"
      :aria-label="attrs['aria-label'] as string" :aria-invalid="attrs['aria-invalid'] as boolean | 'true' | 'false' | undefined"
      aria-hidden="true" tabindex="-1" @change="onChange"><slot /></select>
    <button ref="trigger" v-bind="triggerAttrs()" :id="triggerId" type="button" class="control-select__trigger"
      role="combobox" aria-haspopup="listbox" :aria-expanded="open" :aria-controls="open ? listId : undefined"
      :aria-activedescendant="open && active >= 0 ? `${listId}-${active}` : undefined" :disabled="disabled"
      :title="attrs.title as string || selectedLabel" @click="open ? close() : show()" @keydown="onKeydown"
      @compositionstart="composing = true" @compositionend="composing = false">
      <span class="control-select__value">{{ selectedLabel }}</span>
      <Icon name="chevronDown" :size="16" />
    </button>
    <div v-if="open" :id="listId" ref="menu" class="control-select__menu" popover="manual" :style="menuStyle"
      role="listbox" :aria-labelledby="triggerId">
      <div v-for="(group, groupIndex) in groups" :key="groupIndex" :role="group.label ? 'group' : 'presentation'" :aria-label="group.label || undefined">
        <div v-if="group.label" class="control-select__group" aria-hidden="true">{{ group.label }}</div>
        <div v-for="choice in group.choices" :key="choice.index" :id="`${listId}-${choice.index}`" class="control-select__option" role="option" :data-index="choice.index"
          :class="{ 'is-active': choice.index === active, 'is-selected': choice.value === String(current ?? '') }"
          :aria-selected="choice.value === String(current ?? '')" :aria-disabled="choice.disabled || undefined"
          @pointermove="!choice.disabled && (active = choice.index)" @pointerdown.prevent
          @click.stop.prevent="choose(choice)">
          <span>{{ choice.label }}</span><Icon v-if="choice.value === String(current ?? '')" name="check" :size="15" />
        </div>
      </div>
    </div>
  </span>
</template>

<style scoped>
.control-select { display: inline-flex; min-width: 0; width: 100%; vertical-align: middle; }
.control-select.is-standard { max-width: 280px; }
.control-select.control-input--narrow { max-width: 96px; }
.control-select__native { display: none; }
.control-select__trigger { align-items: center; background: var(--bg-surface-2); border: 1px solid transparent; border-radius: var(--radius-control); color: var(--text); cursor: pointer; display: flex; font: inherit; font-size: var(--fs-sm); gap: 12px; justify-content: space-between; line-height: 1.5; min-height: 38px; min-width: 0; padding: 7px 10px; text-align: start; width: 100%; transition: border-color var(--dur-fast), background var(--dur-fast); }
.control-select__trigger:hover:not(:disabled) { background: var(--bg-elevated); border-color: var(--border-focus); }
.control-select__trigger:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.control-select__trigger[aria-expanded="true"] { border-color: var(--border-focus); }
.control-select__trigger:disabled { cursor: not-allowed; opacity: .55; }
.control-select__value { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.control-select__trigger :deep(svg) { color: var(--text-muted); flex: none; }
.control-select__menu { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-control); box-shadow: var(--elev-2); box-sizing: border-box; color: var(--text); inset: auto; margin: 0; max-width: none; overflow: auto; overscroll-behavior: contain; padding: 4px; position: fixed; z-index: 1300; }
.control-select__group { color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.5; padding: 8px 9px 4px; }
.control-select__option { align-items: center; border-radius: var(--radius-sm); cursor: pointer; display: flex; font-size: var(--fs-sm); gap: 12px; justify-content: space-between; line-height: 1.5; min-height: 34px; padding: 6px 9px; }
.control-select__option > span { min-width: 0; overflow-wrap: anywhere; }
.control-select__option :deep(svg) { color: var(--accent); flex: none; }
.control-select__option.is-selected { background: color-mix(in srgb, var(--accent) 8%, var(--bg-surface)); }
.control-select__option.is-active { background: var(--bg-elevated); outline: 1px solid var(--border-focus); outline-offset: -1px; }
.control-select__option[aria-disabled="true"] { color: var(--text-muted); cursor: not-allowed; opacity: .65; }
@media (prefers-reduced-motion: reduce) { .control-select__trigger { transition: none; } }
</style>
