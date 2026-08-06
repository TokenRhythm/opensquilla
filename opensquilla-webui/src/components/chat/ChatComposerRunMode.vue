<template>
  <section
    ref="rootRef"
    tabindex="-1"
    class="composer-run-mode"
    role="dialog"
    :aria-label="t('chat.composer.runMode')"
    @keydown.esc.stop="$emit('close')"
  >
    <div class="composer-run-mode__head">
      <span>{{ t('chat.composer.runMode') }}</span>
      <button type="button" class="composer-run-mode__close" :aria-label="t('chat.closeComposerSettings')" @click="$emit('close')">
        <Icon name="x" :size="14" />
      </button>
    </div>

    <div class="composer-run-mode__list" role="radiogroup" :aria-label="t('chat.composer.runMode')">
      <button
        v-for="option in runModeOptions"
        :key="option.value"
        type="button"
        class="composer-run-mode__option"
        :class="{ 'is-active': selectedRunMode === option.value }"
        :disabled="isDisabled(option.value)"
        role="radio"
        :aria-checked="selectedRunMode === option.value ? 'true' : 'false'"
        @click="selectRunMode(option.value)"
      >
        <span class="composer-run-mode__option-main">
          <span class="composer-run-mode__option-label">{{ option.label }}</span>
          <Icon v-if="selectedRunMode === option.value" name="check" :size="14" />
        </span>
        <span class="composer-run-mode__option-desc">{{ option.description }}</span>
      </button>
    </div>

  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { SandboxRunMode } from '@/types/sandbox'
import { SANDBOX_RUN_MODES, normalizeSandboxRunMode } from '@/types/sandbox'

const { t } = useI18n()

const props = defineProps<{
  runMode: SandboxRunMode
  allowedRunModes: SandboxRunMode[]
  safeSetupAvailable?: boolean
}>()

const emit = defineEmits<{
  close: []
  setRunMode: [mode: SandboxRunMode]
}>()

const runModeOptions = computed(() => [
  {
    value: 'safe',
    label: t('chat.composer.runModeSafe'),
    description: t('chat.composer.runModeSafeDesc'),
  },
  {
    value: 'full',
    label: t('chat.composer.runModeFull'),
    description: t('chat.composer.runModeFullDesc'),
  },
] satisfies Array<{ value: SandboxRunMode; label: string; description: string }>)

const selectedRunMode = computed(() => normalizeSandboxRunMode(props.runMode))

const allowedRunModes = computed(() => {
  const allowed = props.allowedRunModes.filter(mode => SANDBOX_RUN_MODES.includes(mode))
  return allowed.length > 0 ? allowed : [...SANDBOX_RUN_MODES]
})

function isDisabled(mode: SandboxRunMode): boolean {
  if (mode === 'safe' && props.safeSetupAvailable) return false
  return !allowedRunModes.value.includes(mode)
}

function selectRunMode(mode: SandboxRunMode) {
  if (isDisabled(mode)) return
  emit('setRunMode', mode)
  emit('close')
}

const rootRef = ref<HTMLElement | null>(null)
onMounted(() => rootRef.value?.focus())
</script>

<style scoped>
.composer-run-mode {
  position: absolute;
  left: 0;
  bottom: calc(100% + 8px);
  width: min(340px, calc(100vw - 48px));
  padding: 0.75rem;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-xl);
  z-index: 30;
}

.composer-run-mode__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.625rem;
  font-size: 0.8125rem;
  font-weight: 700;
  color: var(--text);
}

.composer-run-mode__close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid transparent;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.composer-run-mode__close:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.composer-run-mode__list {
  display: grid;
  gap: 0.375rem;
}

.composer-run-mode__option {
  display: grid;
  gap: 0.25rem;
  width: 100%;
  min-height: 58px;
  padding: 0.625rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}

.composer-run-mode__option:hover {
  background: var(--bg-hover);
}

.composer-run-mode__option:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--accent) 45%, transparent);
  outline-offset: 2px;
}

.composer-run-mode__option.is-active {
  border-color: color-mix(in srgb, var(--ok) 55%, var(--border));
  background: color-mix(in srgb, var(--ok) 10%, var(--bg-surface));
}

.composer-run-mode__option:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.composer-run-mode__option:disabled:hover {
  background: transparent;
}

.composer-run-mode__option-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.composer-run-mode__option-label {
  font-size: 0.8125rem;
  font-weight: 700;
}

.composer-run-mode__option-desc {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 0.75rem;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 520px) {
  .composer-run-mode {
    left: -2.75rem;
    width: calc(100vw - 32px);
  }
}
</style>
