<template>
  <div ref="root" class="cron-select" :class="{ 'is-open': open, 'is-disabled': disabled, 'is-embedded': embedded }" @focusout="onFocusOut">
    <button
      ref="trigger"
      :id="id"
      type="button"
      class="cron-select__trigger"
      role="combobox"
      aria-haspopup="listbox"
      :aria-expanded="open"
      :aria-label="ariaLabel"
      :disabled="disabled"
      @click="toggle"
      @keydown="onTriggerKeydown"
    >
      <span class="cron-select__value">{{ selectedLabel }}</span>
      <Icon :name="open ? 'chevronDown' : 'chevronLeft'" :size="16" />
    </button>
    <Transition name="cron-select-menu">
      <div v-if="open" class="cron-select__menu" role="listbox" :aria-label="ariaLabel">
        <button
          v-for="option in options"
          :key="option.value"
          type="button"
          class="cron-select__option"
          :class="{ 'is-selected': option.value === modelValue }"
          role="option"
          :aria-selected="option.value === modelValue"
          :disabled="option.disabled"
          tabindex="-1"
          @click="choose(option.value)"
          @keydown="onOptionKeydown"
        >
          <span>{{ option.label }}</span>
          <Icon v-if="option.value === modelValue" name="check" :size="15" />
        </button>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import Icon from '@/components/Icon.vue'

export interface CronSelectOption {
  value: string
  label: string
  disabled?: boolean
}

const props = withDefaults(defineProps<{
  id?: string
  modelValue: string
  options: CronSelectOption[]
  ariaLabel?: string
  disabled?: boolean
  embedded?: boolean
}>(), {
  id: undefined,
  ariaLabel: undefined,
  disabled: false,
  embedded: false,
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  change: [value: string]
}>()

const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const selectedLabel = computed(() => props.options.find(option => option.value === props.modelValue)?.label || '')

function optionButtons() {
  return Array.from(root.value?.querySelectorAll<HTMLButtonElement>('.cron-select__option:not(:disabled)') || [])
}

function openAndFocus(last = false) {
  if (props.disabled) return
  open.value = true
  void nextTick(() => {
    const options = optionButtons()
    const selected = root.value?.querySelector<HTMLButtonElement>('.cron-select__option.is-selected:not(:disabled)')
    const target = last ? options[options.length - 1] : selected || options[0]
    target?.focus()
  })
}

function closeAndFocus() {
  open.value = false
  void nextTick(() => trigger.value?.focus())
}

function toggle() {
  if (props.disabled) return
  if (open.value) open.value = false
  else openAndFocus()
}

function choose(value: string) {
  emit('update:modelValue', value)
  emit('change', value)
  closeAndFocus()
}

function onTriggerKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    if (!open.value) return
    event.preventDefault()
    event.stopPropagation()
    closeAndFocus()
  } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    if (!open.value) openAndFocus(event.key === 'ArrowUp')
  } else if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    if (!open.value) openAndFocus()
  }
}

function onOptionKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closeAndFocus()
    return
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
  event.preventDefault()
  const options = optionButtons()
  const current = options.indexOf(event.currentTarget as HTMLButtonElement)
  const next = (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length
  options[next]?.focus()
}

function onDocumentPointerDown(event: PointerEvent) {
  if (!root.value?.contains(event.target as Node)) open.value = false
}

function onFocusOut(event: FocusEvent) {
  const nextTarget = event.relatedTarget
  if (nextTarget instanceof Node && root.value?.contains(nextTarget)) return
  open.value = false
}

onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown))
</script>

<style scoped>
.cron-select {
  position: relative;
  width: 100%;
}

.cron-select__trigger {
  align-items: center;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text);
  display: flex;
  font: inherit;
  font-size: var(--fs-sm);
  gap: 10px;
  justify-content: space-between;
  min-height: 40px;
  padding: 8px 12px;
  text-align: left;
  transition: border-color var(--dur-fast), box-shadow var(--dur-fast), background var(--dur-fast);
  width: 100%;
}

.cron-select__trigger:hover:not(:disabled) {
  background: var(--bg-elevated);
}

.cron-select__trigger:focus-visible,
.cron-select.is-open .cron-select__trigger {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
  outline: none;
}

.cron-select__value {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cron-select__menu {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--elev-2);
  left: 0;
  margin-top: 6px;
  max-height: 240px;
  overflow-y: auto;
  padding: 5px;
  position: absolute;
  right: 0;
  top: 100%;
  z-index: 20;
}

.cron-select__option {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text);
  display: flex;
  font: inherit;
  font-size: var(--fs-sm);
  justify-content: space-between;
  min-height: 36px;
  padding: 7px 10px;
  text-align: left;
  width: 100%;
}

.cron-select__option:hover:not(:disabled),
.cron-select__option.is-selected {
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-elevated));
  color: var(--accent);
}

.cron-select__option:disabled,
.cron-select.is-disabled {
  opacity: 0.55;
}

.cron-select.is-embedded .cron-select__trigger,
.cron-select.is-embedded .cron-select__trigger:hover:not(:disabled),
.cron-select.is-embedded.is-open .cron-select__trigger,
.cron-select.is-embedded .cron-select__trigger:focus-visible {
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  min-height: 38px;
  padding: 7px 10px;
}

.cron-select-menu-enter-active,
.cron-select-menu-leave-active {
  transition: opacity var(--dur-fast), transform var(--dur-fast);
  transform-origin: top;
}

.cron-select-menu-enter-from,
.cron-select-menu-leave-to {
  opacity: 0;
  transform: translateY(-4px) scale(0.99);
}
</style>
