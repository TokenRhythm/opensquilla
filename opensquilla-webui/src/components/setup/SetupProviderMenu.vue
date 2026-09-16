<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import Icon from '@/components/Icon.vue'
import type { IconName } from '@/utils/icons'
import { useDocumentEvent } from '@/composables/useDocumentEvent'

export interface ProviderMenuItem {
  id: string
  label: string
  ariaLabel?: string
  describedBy?: string
  icon: IconName
  disabled?: boolean
  hint?: string
  danger?: boolean
  separatorBefore?: boolean
  className?: string
}

const props = defineProps<{
  label: string
  open: boolean
  disabled?: boolean
  items: readonly ProviderMenuItem[]
}>()
const emit = defineEmits<{
  'update:open': [open: boolean]
  action: [id: string]
}>()
const menuId = `provider-actions-${useId()}`
const trigger = ref<HTMLButtonElement | null>(null)
const menu = ref<HTMLElement | null>(null)
const menuStyle = ref<Record<string, string>>({ visibility: 'hidden' })
let initialPosition: 'first' | 'last' = 'first'
let focusEpoch = 0

function close(restoreFocus = false) {
  focusEpoch += 1
  if (!props.open) return
  emit('update:open', false)
  if (restoreFocus && !props.disabled) trigger.value?.focus({ preventScroll: true })
}

function openMenu(position: 'first' | 'last' = 'first') {
  if (props.disabled) return
  initialPosition = position
  emit('update:open', true)
}

function enabledItems(): HTMLButtonElement[] {
  return Array.from(menu.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not([disabled])') || [])
}

function positionMenu() {
  if (!trigger.value || !menu.value) return
  const margin = 8
  const gap = 4
  const bounds = trigger.value.getBoundingClientRect()
  const width = Math.min(240, window.innerWidth - margin * 2)
  const availableHeight = Math.max(0, window.innerHeight - margin * 2)
  const height = Math.min(menu.value.getBoundingClientRect().height, availableHeight)
  const below = bounds.bottom + gap
  const top = below + height <= window.innerHeight - margin
    ? below : Math.max(margin, bounds.top - gap - height)
  menuStyle.value = {
    left: `${Math.max(margin, Math.min(bounds.right - width, window.innerWidth - width - margin))}px`,
    top: `${top}px`,
    width: `${width}px`,
    maxHeight: `${availableHeight}px`,
    visibility: 'visible',
  }
}

watch(() => props.open, async opened => {
  const epoch = ++focusEpoch
  if (!opened) {
    menuStyle.value = { visibility: 'hidden' }
    return
  }
  if (props.disabled) {
    close()
    return
  }
  await nextTick()
  if (epoch !== focusEpoch || !props.open || !menu.value) return
  positionMenu()
  await nextTick()
  if (epoch !== focusEpoch || !props.open || props.disabled) return
  const items = enabledItems()
  ;(initialPosition === 'last' ? items[items.length - 1] : items[0])?.focus({ preventScroll: true })
})
watch(() => props.disabled, disabled => { if (disabled) close() }, { flush: 'sync' })

function choose(item: ProviderMenuItem) {
  if (props.disabled || item.disabled) return
  // The editor/confirmation must capture a persistent invoker, not a menu item
  // that disappears as soon as the action begins.
  close(true)
  emit('action', item.id)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    close(true)
    return
  }
  if (event.key === 'Tab') {
    // Continue native tab order from the persistent trigger inside Settings.
    close(true)
    return
  }
  const items = enabledItems()
  if (!items.length) return
  const current = items.indexOf(document.activeElement as HTMLButtonElement)
  let index: number | undefined
  if (event.key === 'ArrowDown') index = (current + 1) % items.length
  if (event.key === 'ArrowUp') index = current <= 0 ? items.length - 1 : current - 1
  if (event.key === 'Home') index = 0
  if (event.key === 'End') index = items.length - 1
  if (index === undefined) return
  event.preventDefault()
  event.stopPropagation()
  items[index]?.focus()
}

useDocumentEvent('pointerdown', event => {
  if (!props.open || !(event.target instanceof Node)) return
  if (!trigger.value?.contains(event.target) && !menu.value?.contains(event.target)) close()
})
useDocumentEvent('scroll', event => {
  if (!(event.target instanceof Node) || !menu.value?.contains(event.target)) close()
}, true)
const onResize = () => close()
onMounted(() => window.addEventListener('resize', onResize))
onBeforeUnmount(() => window.removeEventListener('resize', onResize))
</script>

<template>
  <button
    ref="trigger"
    type="button"
    class="btn btn--ghost setup-provider-more"
    :disabled="disabled"
    :aria-label="label"
    aria-haspopup="menu"
    :aria-expanded="open"
    :aria-controls="menuId"
    @click.stop="open ? close(true) : openMenu()"
    @keydown.down.prevent="openMenu('first')"
    @keydown.up.prevent="openMenu('last')"
  ><Icon name="moreHorizontal" :size="18" aria-hidden="true" /></button>
  <Teleport to="body">
    <div
      v-if="open"
      :id="menuId"
      ref="menu"
      class="theme-menu setup-provider-menu"
      :style="menuStyle"
      role="menu"
      :aria-label="label"
      @keydown="onKeydown"
    >
      <template v-for="item in items" :key="item.id">
        <div v-if="item.separatorBefore" class="setup-provider-menu__separator" role="separator"></div>
        <button
          type="button"
          role="menuitem"
          class="theme-menu__item setup-provider-menu__item"
          :class="[item.className, { 'is-danger': item.danger }]"
          :disabled="disabled || item.disabled"
          :aria-label="item.ariaLabel || item.label"
          :aria-describedby="item.describedBy"
          :title="item.hint || undefined"
          @click="choose(item)"
        >
          <Icon
            class="setup-provider-menu__icon"
            :name="item.icon"
            :size="15"
            aria-hidden="true"
          />
          <span>{{ item.label }}<small v-if="item.hint">{{ item.hint }}</small></span>
        </button>
      </template>
    </div>
  </Teleport>
</template>

<style scoped>
.setup-provider-more {
  flex: 0 0 32px !important;
  min-height: 32px;
  padding: var(--sp-1);
  width: 32px;
}
.setup-provider-menu {
  position: fixed;
  right: auto;
  z-index: 440;
  overflow-y: auto;
  max-width: calc(100vw - 16px);
}
.setup-provider-menu__item {
  align-items: flex-start;
  color: var(--text);
  min-height: 36px;
  white-space: normal;
}
.setup-provider-menu__icon {
  align-items: center;
  display: inline-flex;
  flex: 0 0 16px;
  height: 16px;
  justify-content: center;
  margin-top: 1px;
  width: 16px;
}
.setup-provider-menu__icon :deep(svg) { display: block; }
.setup-provider-menu__item span { min-width: 0; overflow-wrap: anywhere; }
.setup-provider-menu__item small {
  color: var(--text-muted);
  display: block;
  font-size: var(--fs-xs);
  margin-top: var(--sp-1);
}
.setup-provider-menu__item:disabled { cursor: default; opacity: 0.65; }
.setup-provider-menu__item.is-danger { color: var(--danger); }
.setup-provider-menu__separator { border-top: 1px solid var(--border); margin: var(--sp-1); }
@media (pointer: coarse) {
  .setup-provider-more { flex-basis: 44px !important; min-height: 44px; width: 44px; }
  .setup-provider-menu__item { min-height: 44px; }
}
</style>
