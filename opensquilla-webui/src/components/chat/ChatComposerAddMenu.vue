<template>
  <section
    ref="rootRef"
    tabindex="-1"
    class="composer-add-menu"
    :class="{ 'is-positioned': positioned }"
    role="menu"
    :aria-label="t('chat.add')"
    @keydown.esc.stop="$emit('close')"
  >
    <div class="composer-add-menu__heading">{{ t('chat.add') }}</div>
    <button
      type="button"
      class="composer-add-menu__item"
      role="menuitem"
      :disabled="attachmentsDisabled"
      @click="attachFiles"
    >
      <span class="composer-add-menu__icon" aria-hidden="true">
        <Icon name="paperclip" :size="17" />
      </span>
      <span class="composer-add-menu__copy">
        <strong>{{ t('chat.attachFiles') }}</strong>
      </span>
    </button>
    <button
      v-if="planModeAvailable"
      type="button"
      class="composer-add-menu__item"
      role="menuitem"
      :disabled="planModeBusy || planModeActive"
      :aria-pressed="planModeActive"
      @click="activatePlanMode"
    >
      <span class="composer-add-menu__icon" aria-hidden="true">
        <Icon name="listChecks" :size="17" />
      </span>
      <span class="composer-add-menu__copy">
        <strong>{{ t('chat.planMode.label') }}</strong>
        <span>
          {{ planModeActive
            ? t('chat.planMode.readOnly')
            : t('chat.planMode.turnOn') }}
        </span>
      </span>
    </button>
    <button
      v-if="goalModeAvailable"
      type="button"
      class="composer-add-menu__item"
      role="menuitem"
      :disabled="goalModeBusy || goalModeActive || goalModeExisting"
      :aria-pressed="goalModeActive"
      @click="activateGoalMode"
    >
      <span class="composer-add-menu__icon" aria-hidden="true">
        <Icon name="target" :size="17" />
      </span>
      <span class="composer-add-menu__copy">
        <strong>{{ t('chat.goal.modeLabel') }}</strong>
        <span>
          {{ goalModeActive
            ? t('chat.goal.modeReady')
            : goalModeExisting
              ? t('chat.goal.activeTitle')
              : t('chat.goal.modeDescription') }}
        </span>
      </span>
    </button>
  </section>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { resolveComposerAddMenuPlacement } from '@/utils/chat/composerAddMenuPlacement'

const props = defineProps<{
  avoidElement?: HTMLElement | null
  attachmentsDisabled?: boolean
  goalModeActive: boolean
  goalModeAvailable: boolean
  goalModeBusy: boolean
  goalModeExisting: boolean
  planModeActive: boolean
  planModeAvailable: boolean
  planModeBusy: boolean
}>()

const emit = defineEmits<{
  activateGoalMode: []
  activatePlanMode: []
  attachFiles: []
  close: []
}>()

const { t } = useI18n()
const rootRef = ref<HTMLElement | null>(null)
const positioned = ref(false)
let placementFrame: number | null = null
let currentLift = 0
let currentMaxHeight: number | null = null

function updatePlacement() {
  const root = rootRef.value
  if (!root) return

  const boundaryTop = props.avoidElement?.getBoundingClientRect().top ?? null
  if (boundaryTop === null) {
    if (currentMaxHeight !== null) {
      currentLift = 0
      currentMaxHeight = null
      root.style.removeProperty('--composer-add-menu-lift')
      root.style.removeProperty('max-height')
    }
  } else {
    const placement = resolveComposerAddMenuPlacement({
      menuBottom: root.getBoundingClientRect().bottom,
      currentLift,
      boundaryTop,
    })
    if (placement.lift !== currentLift) {
      currentLift = placement.lift
      root.style.setProperty('--composer-add-menu-lift', `${placement.lift}px`)
    }
    if (placement.maxHeight !== currentMaxHeight) {
      currentMaxHeight = placement.maxHeight
      root.style.setProperty('max-height', `${placement.maxHeight}px`)
    }
  }
  positioned.value = true
}

function trackPlacement() {
  updatePlacement()
  placementFrame = window.requestAnimationFrame(trackPlacement)
}

function attachFiles() {
  emit('attachFiles')
  emit('close')
}

function activatePlanMode() {
  emit('activatePlanMode')
  emit('close')
}

function activateGoalMode() {
  emit('activateGoalMode')
  emit('close')
}

onMounted(async () => {
  updatePlacement()
  await nextTick()
  rootRef.value?.focus()
  if (typeof window.requestAnimationFrame === 'function') {
    placementFrame = window.requestAnimationFrame(trackPlacement)
  }
})

onBeforeUnmount(() => {
  if (placementFrame !== null) {
    window.cancelAnimationFrame(placementFrame)
    placementFrame = null
  }
})
</script>

<style scoped>
.composer-add-menu {
  position: absolute;
  bottom: calc(100% + 8px + var(--composer-add-menu-lift, 0px));
  left: 0;
  z-index: 30;
  display: grid;
  width: min(320px, calc(100vw - 48px));
  padding: var(--sp-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xl);
  overflow-y: auto;
  visibility: hidden;
}

.composer-add-menu.is-positioned {
  visibility: visible;
}

.composer-add-menu__heading {
  padding: var(--sp-1) var(--sp-2) var(--sp-2);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 650;
}

.composer-add-menu__item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  min-height: 44px;
  padding: var(--sp-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  cursor: pointer;
  font: inherit;
  text-align: left;
}

.composer-add-menu__item:hover:not(:disabled),
.composer-add-menu__item:focus-visible {
  background: var(--bg-hover);
}

.composer-add-menu__item:disabled {
  cursor: default;
  opacity: var(--state-disabled-opacity);
}

.composer-add-menu__icon {
  display: inline-flex;
  color: var(--text-muted);
}

.composer-add-menu__copy {
  display: grid;
  min-width: 0;
}

.composer-add-menu__copy strong {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.composer-add-menu__copy span {
  overflow: hidden;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 520px) {
  .composer-add-menu {
    left: -0.5rem;
    width: calc(100vw - 32px);
  }
}
</style>
