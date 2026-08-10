<template>
  <div v-if="hasError" class="error-boundary">
    <div class="error-boundary__content">
      <h2>{{ t('errorBoundary.title') }}</h2>
      <p class="error-boundary__message">{{ errorMessage || t('errorBoundary.defaultMessage') }}</p>

      <details v-if="errorDetails" class="error-boundary__details">
        <summary>{{ t('errorBoundary.detailsLabel') }}</summary>
        <p class="error-boundary__privacy-hint">{{ t('errorBoundary.privacyHint') }}</p>
        <pre class="error-boundary__stack">{{ errorDetails }}</pre>
      </details>

      <div class="error-boundary__actions">
        <button type="button" class="btn btn--primary" @click="reload">
          {{ t('errorBoundary.reload') }}
        </button>
        <button
          v-if="errorDetails"
          type="button"
          class="btn btn--ghost"
          data-testid="error-boundary-copy"
          @click="copyDetails"
        >{{ copied ? t('errorBoundary.copied') : t('errorBoundary.copyDetails') }}</button>
        <button
          v-if="canRevealLog"
          type="button"
          class="btn btn--ghost"
          data-testid="error-boundary-open-logs"
          @click="openLogs"
        >{{ t('errorBoundary.openLogs') }}</button>
        <button type="button" class="btn btn--ghost" @click="clearError">
          {{ t('errorBoundary.dismiss') }}
        </button>
      </div>
      <p
        v-if="actionError"
        class="error-boundary__action-error"
        data-testid="error-boundary-action-error"
        role="alert"
      >{{ actionError }}</p>
    </div>
  </div>
  <slot v-else />
</template>

<script setup lang="ts">
import { ref, computed, onErrorCaptured } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlatform } from '@/platform'
import { copyTextWithFallback } from '@/utils/browser'
import { errorBoundaryMessage, errorBoundaryDetails } from './errorBoundaryDetails'

// useScope:'global' so the outermost error boundary never depends on a scoped
// i18n instance being present when it has to render.
const { t } = useI18n({ useScope: 'global' })
const platform = usePlatform()

const hasError = ref(false)
const errorMessage = ref('')
const errorDetails = ref('')
const copied = ref(false)
const actionError = ref('')

// Only shown on desktop, where a real log folder exists and can be revealed.
// On the web there is no revealLog capability, so the button is hidden rather
// than dead. Mirrors the pattern in DesktopRuntimePanel.
const canRevealLog = computed(() => Boolean(platform.gateway.revealLog))

onErrorCaptured((err: unknown) => {
  hasError.value = true
  errorMessage.value = errorBoundaryMessage(err)
  errorDetails.value = errorBoundaryDetails(err)
  copied.value = false
  actionError.value = ''
  console.error('[ErrorBoundary]', errorDetails.value || errorMessage.value || 'Unknown error')
  return false // Prevent error from propagating
})

function reload() {
  window.location.reload()
}

function clearError() {
  hasError.value = false
  errorMessage.value = ''
  errorDetails.value = ''
  copied.value = false
  actionError.value = ''
}

async function copyDetails() {
  if (!errorDetails.value) return
  try {
    await copyTextWithFallback(errorDetails.value)
    copied.value = true
    actionError.value = ''
  } catch {
    copied.value = false
    actionError.value = t('errorBoundary.copyFailed')
  }
}

async function openLogs() {
  if (!platform.gateway.revealLog) return
  try {
    await platform.gateway.revealLog()
    actionError.value = ''
  } catch {
    actionError.value = t('errorBoundary.openLogsFailed')
  }
}
</script>

<style scoped>
.error-boundary {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
  padding: 2rem;
}

.error-boundary__content {
  text-align: center;
  max-width: 480px;
}

.error-boundary__content h2 {
  margin-bottom: 0.75rem;
  color: var(--text);
}

.error-boundary__message {
  color: var(--text-muted);
  margin-bottom: 1.5rem;
  font-size: var(--fs-sm);
  word-break: break-word;
}

.error-boundary__details {
  margin-bottom: 1.5rem;
  text-align: left;
}

.error-boundary__details summary {
  cursor: pointer;
  color: var(--text-muted);
  font-size: var(--fs-sm);
}

.error-boundary__stack {
  margin-top: 0.75rem;
  max-height: 220px;
  overflow: auto;
  padding: 0.75rem;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  color: var(--text-muted);
  font-size: var(--fs-xs);
  white-space: pre-wrap;
  word-break: break-word;
}

.error-boundary__privacy-hint,
.error-boundary__action-error {
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.error-boundary__privacy-hint {
  margin: 0.75rem 0 0;
}

.error-boundary__action-error {
  margin: 0.75rem 0 0;
}

.error-boundary__actions {
  display: flex;
  gap: 0.75rem;
  justify-content: center;
  flex-wrap: wrap;
}
</style>
