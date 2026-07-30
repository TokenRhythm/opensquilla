<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import GatewayStatusBlock from '@/components/settings/GatewayStatusBlock.vue'
import SettingsUpdatePanel from '@/components/settings/SettingsUpdatePanel.vue'
import {
  usePlatform,
  type DesktopMainWindowCloseBehavior,
  type DesktopPreferences,
  type GatewayStatus,
  type WorkbenchPreviewMode,
} from '@/platform'
import { useToasts } from '@/composables/useToasts'

const { t } = useI18n()

// Desktop-only runtime operations stay deliberately narrow here. Profile
// migration and cleanup live under Settings → Advanced → Data maintenance.
const platform = usePlatform()
const { pushToast } = useToasts()

const loading = ref(true)
const busy = ref(false)
const gateway = shallowRef<GatewayStatus | null>(null)
const desktopPreferences = shallowRef<DesktopPreferences | null>(null)
const closeBehavior = ref<DesktopMainWindowCloseBehavior>('quit')
const previewMode = ref<WorkbenchPreviewMode>('full')
const preferencesSaving = ref(false)
// Read-failure state, tracked separately from the loaded value: a failed read
// keeps the preference row visible with an inline error and Retry, while an
// older shell without the preference bridge still hides the row entirely.
const preferencesLoadError = ref('')
const preferencesLoading = ref(false)

const STATUS_KEYS: Record<string, string> = {
  starting: 'setup.runtime.statusStarting',
  ready: 'setup.runtime.statusReady',
  stopped: 'setup.runtime.statusStopped',
  error: 'setup.runtime.statusError',
}

const statusLabel = computed(() => {
  const key = STATUS_KEYS[gateway.value?.status ?? '']
  return key ? t(key) : t('setup.runtime.statusUnknown')
})
const gatewayError = computed(() => gateway.value?.error || '')
const url = computed(() => gateway.value?.url || t('setup.runtime.noActiveGateway'))
const logAvailable = computed(() => Boolean(gateway.value?.logPath))
const logHint = computed(() => gateway.value?.logPath || t('setup.runtime.noLogPath'))
const canRevealLog = computed(() => Boolean(platform.gateway.revealLog))
const canRestart = computed(() => Boolean(platform.gateway.retryStartup))
const canManageDesktopPreferences = computed(() => (
  typeof platform.settings.getDesktopPreferences === 'function'
  && typeof platform.settings.saveDesktopPreferences === 'function'
))

const CLOSE_BEHAVIORS = new Set<DesktopMainWindowCloseBehavior>([
  'background',
  'quit',
  'ask',
])

// The shell owns the stored preference, so a value that needs background
// support this platform lacks is never rewritten from here — the row calls
// the mismatch out next to the select instead.
const closeBehaviorUnavailable = computed(() => (
  desktopPreferences.value !== null
  && !desktopPreferences.value.canRunInBackground
  && closeBehavior.value !== 'quit'
))

async function loadDesktopPreferences() {
  if (!canManageDesktopPreferences.value || !platform.settings.getDesktopPreferences) return
  preferencesLoading.value = true
  try {
    const preferences = await platform.settings.getDesktopPreferences()
    desktopPreferences.value = preferences
    closeBehavior.value = preferences.mainWindowCloseBehavior
    previewMode.value = preferences.workbenchPreviewMode ?? 'full'
    preferencesLoadError.value = ''
  } catch (err) {
    preferencesLoadError.value = err instanceof Error ? err.message : String(err)
  } finally {
    preferencesLoading.value = false
  }
}

async function savePreviewMode(event: Event) {
  const value = (event.target as HTMLSelectElement).value as WorkbenchPreviewMode
  const previous = previewMode.value
  if (
    (value !== 'full' && value !== 'offline')
    || !platform.settings.saveDesktopPreferences
  ) return

  previewMode.value = value
  preferencesSaving.value = true
  try {
    const saved = await platform.settings.saveDesktopPreferences({
      workbenchPreviewMode: value,
    })
    desktopPreferences.value = saved
    closeBehavior.value = saved.mainWindowCloseBehavior
    previewMode.value = saved.workbenchPreviewMode ?? value
  } catch (err) {
    previewMode.value = previous
    pushToast(t('setup.runtime.previewModeSaveFailed', {
      error: err instanceof Error ? err.message : String(err),
    }), { tone: 'danger' })
  } finally {
    preferencesSaving.value = false
  }
}

async function saveCloseBehavior(event: Event) {
  const value = (event.target as HTMLSelectElement).value as DesktopMainWindowCloseBehavior
  const previous = desktopPreferences.value?.mainWindowCloseBehavior
  if (
    !previous
    || !CLOSE_BEHAVIORS.has(value)
    || !platform.settings.saveDesktopPreferences
  ) return

  closeBehavior.value = value
  preferencesSaving.value = true
  try {
    const saved = await platform.settings.saveDesktopPreferences({
      mainWindowCloseBehavior: value,
    })
    desktopPreferences.value = saved
    closeBehavior.value = saved.mainWindowCloseBehavior
  } catch (err) {
    closeBehavior.value = previous
    pushToast(t('setup.runtime.closeBehaviorSaveFailed', {
      error: err instanceof Error ? err.message : String(err),
    }), { tone: 'danger' })
  } finally {
    preferencesSaving.value = false
  }
}

async function loadStatus(): Promise<GatewayStatus | null> {
  loading.value = true
  try {
    const status = await platform.gateway.getStatus()
    gateway.value = status
    return status
  } catch (err) {
    pushToast(t('setup.runtime.statusReadFailed', {
      error: err instanceof Error ? err.message : String(err),
    }), { tone: 'danger' })
    return null
  } finally {
    loading.value = false
  }
}

async function revealLog() {
  if (!platform.gateway.revealLog) return
  try {
    const ok = await platform.gateway.revealLog()
    if (!ok) pushToast(t('setup.runtime.noLogToReveal'), { tone: 'danger' })
  } catch (err) {
    pushToast(t('setup.runtime.revealFailed', {
      error: err instanceof Error ? err.message : String(err),
    }), { tone: 'danger' })
  }
}

async function restartGateway(): Promise<GatewayStatus | null> {
  if (!platform.gateway.retryStartup) return null
  busy.value = true
  try {
    const result = await platform.gateway.retryStartup()
    if (!result.ok) {
      pushToast(t('setup.runtime.restartFailed', {
        error: result.error || t('errorBoundary.defaultMessage'),
      }), { tone: 'danger' })
      return null
    }
    pushToast(t('setup.runtime.restarting'))
    return await loadStatus()
  } catch (err) {
    pushToast(t('setup.runtime.restartFailed', {
      error: err instanceof Error ? err.message : String(err),
    }), { tone: 'danger' })
    return null
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  void loadStatus()
  void loadDesktopPreferences()
})
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('setup.runtime.title') }}</h3>
      <p class="control-section__desc">{{ t('setup.runtime.desc') }}</p>
    </div>

    <div class="runtime-grid">
      <GatewayStatusBlock
        :label="t('setup.runtime.gateway')"
        :value="loading ? t('setup.runtime.loading') : statusLabel"
        :hint="gatewayError || url"
      />
      <GatewayStatusBlock
        :label="t('setup.runtime.title')"
        :value="t('setup.runtime.local')"
        :hint="t('setup.runtime.localProcess')"
      />
      <GatewayStatusBlock
        :label="t('setup.runtime.gatewayLog')"
        :value="logAvailable ? t('setup.runtime.available') : t('setup.runtime.unavailable')"
        :hint="logHint"
      />
    </div>

    <div class="runtime-actions">
      <button type="button" class="btn btn--ghost" :disabled="loading || busy" @click="loadStatus">
        <Icon name="refresh" :size="15" aria-hidden="true" />
        <span>{{ t('setup.runtime.refresh') }}</span>
      </button>
      <button v-if="canRevealLog" type="button" class="btn btn--ghost" :disabled="!logAvailable" @click="revealLog">
        <Icon name="logs" :size="15" aria-hidden="true" />
        <span>{{ t('setup.runtime.revealLog') }}</span>
      </button>
      <button v-if="canRestart" type="button" class="btn btn--ghost" :disabled="busy" @click="restartGateway">
        <Icon name="refresh" :size="15" aria-hidden="true" />
        <span>{{ t('setup.runtime.restartRuntime') }}</span>
      </button>
    </div>

    <div
      v-if="desktopPreferences || preferencesLoadError"
      class="control-row desktop-preferences"
      data-testid="desktop-close-behavior"
    >
      <div class="control-row__label-block">
        <!-- The select only exists once preferences load, so the association is
             dropped in the read-failure branch — a `for` pointing at a missing
             id resolves to nothing for assistive tech. -->
        <label
          :for="desktopPreferences ? 'desktop-close-behavior-select' : undefined"
          class="control-row__label"
        >
          {{ t('setup.runtime.closeBehaviorLabel') }}
        </label>
        <span id="desktop-close-behavior-description" class="control-row__desc">
          {{ t('setup.runtime.closeBehaviorDesc') }}
          <template
            v-if="desktopPreferences && !desktopPreferences.canRunInBackground
              && !closeBehaviorUnavailable"
          >
            {{ t('setup.runtime.closeBehaviorBackgroundUnavailable') }}
          </template>
        </span>
      </div>
      <div v-if="desktopPreferences" class="control-row__control">
        <span
          v-if="preferencesSaving"
          class="desktop-preferences__saving"
          role="status"
        >
          {{ t('setup.runtime.closeBehaviorSaving') }}
        </span>
        <span
          v-else-if="closeBehaviorUnavailable"
          id="desktop-close-behavior-mismatch"
          class="desktop-preferences__error"
          role="alert"
          data-testid="desktop-close-behavior-mismatch"
        >
          {{ t('setup.runtime.closeBehaviorFallbackQuit') }}
        </span>
        <select
          id="desktop-close-behavior-select"
          class="control-input desktop-preferences__select"
          data-testid="desktop-close-behavior-select"
          :value="closeBehavior"
          :disabled="preferencesSaving"
          :aria-describedby="closeBehaviorUnavailable
            ? 'desktop-close-behavior-description desktop-close-behavior-mismatch'
            : 'desktop-close-behavior-description'"
          @change="saveCloseBehavior"
        >
          <option value="background" :disabled="!desktopPreferences.canRunInBackground">
            {{ t('setup.runtime.closeBehaviorBackground') }}
          </option>
          <option value="quit">{{ t('setup.runtime.closeBehaviorQuit') }}</option>
          <option value="ask" :disabled="!desktopPreferences.canRunInBackground">
            {{ t('setup.runtime.closeBehaviorAsk') }}
          </option>
        </select>
      </div>
      <div v-else class="control-row__control">
        <span
          class="desktop-preferences__error"
          role="alert"
          data-testid="desktop-close-behavior-error"
        >
          {{ t('setup.runtime.closeBehaviorReadFailed', { error: preferencesLoadError }) }}
        </span>
        <button
          type="button"
          class="btn btn--ghost"
          data-testid="desktop-close-behavior-retry"
          :disabled="preferencesLoading"
          @click="loadDesktopPreferences"
        >
          {{ t('setup.runtime.closeBehaviorRetry') }}
        </button>
      </div>
    </div>

    <div
      v-if="desktopPreferences"
      class="control-row desktop-preferences"
      data-testid="desktop-preview-mode"
    >
      <div class="control-row__label-block">
        <label for="desktop-preview-mode-select" class="control-row__label">
          {{ t('setup.runtime.previewModeLabel') }}
        </label>
        <span id="desktop-preview-mode-description" class="control-row__desc">
          {{ t('setup.runtime.previewModeDesc') }}
        </span>
      </div>
      <div class="control-row__control">
        <span
          v-if="desktopPreferences.workbenchPreviewForcedOffline"
          id="desktop-preview-mode-forced"
          class="desktop-preferences__error"
          role="status"
          data-testid="desktop-preview-mode-forced"
        >
          {{ t('setup.runtime.previewModeForcedOffline') }}
        </span>
        <span
          v-else-if="preferencesSaving"
          class="desktop-preferences__saving"
          role="status"
        >
          {{ t('setup.runtime.previewModeSaving') }}
        </span>
        <select
          id="desktop-preview-mode-select"
          class="control-input desktop-preferences__select"
          data-testid="desktop-preview-mode-select"
          :value="previewMode"
          :disabled="preferencesSaving"
          :aria-describedby="desktopPreferences.workbenchPreviewForcedOffline
            ? 'desktop-preview-mode-description desktop-preview-mode-forced'
            : 'desktop-preview-mode-description'"
          @change="savePreviewMode"
        >
          <option value="full">{{ t('setup.runtime.previewModeFull') }}</option>
          <option value="offline">{{ t('setup.runtime.previewModeOffline') }}</option>
        </select>
      </div>
    </div>

    <SettingsUpdatePanel />
  </section>
</template>

<style scoped>
.runtime-grid {
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}

.runtime-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.desktop-preferences {
  margin-top: var(--sp-2);
}

.desktop-preferences__select {
  min-width: 220px;
}

.desktop-preferences__saving {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.desktop-preferences__error {
  color: var(--danger);
  font-size: var(--fs-xs);
}
</style>
