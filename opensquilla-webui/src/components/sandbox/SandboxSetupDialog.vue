<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="sandbox-setup-overlay"
      data-testid="sandbox-setup-confirm"
      @click.self="cancel"
    >
      <div
        class="sandbox-setup-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="sandbox-setup-dialog-title"
        aria-describedby="sandbox-setup-dialog-description"
      >
        <h4 id="sandbox-setup-dialog-title">{{ t('settings.sandbox.setup.title') }}</h4>
        <p id="sandbox-setup-dialog-description">
          {{ t('settings.sandbox.setup.descriptionWithDuration') }}
        </p>
        <p
          v-if="progressMessage"
          class="sandbox-setup-progress"
          data-testid="sandbox-setup-progress"
          role="status"
        >
          {{ progressMessage }}
        </p>
        <p v-else-if="outcomeMessage" class="sandbox-setup-result" role="status">
          {{ outcomeMessage }}
        </p>
        <div class="sandbox-setup-dialog__actions">
          <button
            v-if="pending"
            type="button"
            class="btn"
            data-testid="sandbox-setup-background"
            @click="$emit('background')"
          >
            {{ t('settings.sandbox.setup.runInBackground') }}
          </button>
          <button v-else type="button" class="btn" @click="cancel">
            {{ t('common.cancel') }}
          </button>
          <button
            type="button"
            class="btn btn--primary"
            data-testid="sandbox-setup-continue"
            :disabled="pending"
            @click="$emit('confirm')"
          >
            {{ confirmLabel }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { SandboxSetupOutcome } from '@/composables/sandboxSetupCoordinator'

const props = defineProps<{
  open: boolean
  pending: boolean
  outcome: SandboxSetupOutcome
}>()

const emit = defineEmits<{
  cancel: []
  confirm: []
  background: []
}>()

const { t } = useI18n()
const elapsedSeconds = ref(0)
let progressInterval: ReturnType<typeof setInterval> | null = null

function clearProgress(): void {
  if (progressInterval !== null) clearInterval(progressInterval)
  progressInterval = null
  elapsedSeconds.value = 0
}

function startProgress(): void {
  clearProgress()
  progressInterval = setInterval(() => {
    elapsedSeconds.value += 1
  }, 1_000)
}

watch(
  () => props.open && props.pending,
  active => active ? startProgress() : clearProgress(),
  { immediate: true },
)

const progressMessage = computed(() => {
  if (!props.pending) return ''
  const phase = elapsedSeconds.value >= 15
    ? t('settings.sandbox.setup.takingLonger')
    : elapsedSeconds.value >= 5
      ? t('settings.sandbox.setup.configuringProtection')
      : t('settings.sandbox.setup.requestingApproval')
  return `${phase} ${t('settings.sandbox.setup.elapsed', { seconds: elapsedSeconds.value })}`
})

const outcomeMessage = computed(() => {
  if (props.outcome === 'cancelled') return t('settings.sandbox.setup.cancelled')
  if (props.outcome === 'failed') return t('settings.sandbox.setup.failed')
  if (props.outcome === 'verification_failed') return t('settings.sandbox.setup.verificationFailed')
  return ''
})

const confirmLabel = computed(() => {
  if (props.pending) return t('settings.sandbox.setup.configuring')
  if (outcomeMessage.value) return t('settings.sandbox.actions.retry')
  return t('settings.sandbox.setup.continue')
})

function cancel(): void {
  if (!props.pending) emit('cancel')
}

onUnmounted(clearProgress)
</script>

<style scoped>
.sandbox-setup-overlay {
  position: fixed;
  z-index: 1200;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 1.5rem;
  background: var(--scrim);
}

.sandbox-setup-dialog {
  width: min(420px, 100%);
  padding: 1.25rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  background: var(--bg-surface);
  box-shadow: 0 18px 48px color-mix(in srgb, var(--scrim) 26%, transparent);
}

.sandbox-setup-dialog h4,
.sandbox-setup-dialog p {
  margin: 0;
}

.sandbox-setup-dialog p {
  margin-top: 0.6rem;
  color: var(--text-muted);
  font-size: 0.82rem;
  line-height: 1.55;
}

.sandbox-setup-progress {
  min-height: 1.25rem;
}

.sandbox-setup-dialog__actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.65rem;
  margin-top: 1.15rem;
}
</style>
