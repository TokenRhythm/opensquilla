<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import { useToasts } from '@/composables/useToasts'
import {
  usePlatform,
  type DesktopMainWindowCloseBehavior,
  type DesktopPreferences,
} from '@/platform'

const { t } = useI18n()
const { pushToast } = useToasts()
const platform = usePlatform()

const preferences = shallowRef<DesktopPreferences | null>(null)
const value = ref<DesktopMainWindowCloseBehavior>('quit')
const saving = ref(false)
const loading = ref(false)
const loadError = ref('')

const available = computed(() => (
  typeof platform.settings.getDesktopPreferences === 'function'
  && typeof platform.settings.saveDesktopPreferences === 'function'
))
const unavailableSelection = computed(() => (
  preferences.value !== null
  && !preferences.value.canRunInBackground
  && value.value !== 'quit'
))

async function load() {
  if (!available.value || !platform.settings.getDesktopPreferences) return
  loading.value = true
  try {
    const next = await platform.settings.getDesktopPreferences()
    preferences.value = next
    value.value = next.mainWindowCloseBehavior
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

async function save(event: Event) {
  const next = (event.target as HTMLSelectElement).value as DesktopMainWindowCloseBehavior
  const previous = preferences.value?.mainWindowCloseBehavior
  if (!previous || !platform.settings.saveDesktopPreferences) return
  value.value = next
  saving.value = true
  try {
    const saved = await platform.settings.saveDesktopPreferences({
      mainWindowCloseBehavior: next,
    })
    preferences.value = saved
    value.value = saved.mainWindowCloseBehavior
  } catch (error) {
    value.value = previous
    pushToast(t('setup.runtime.closeBehaviorSaveFailed', {
      error: error instanceof Error ? error.message : String(error),
    }), { tone: 'danger' })
  } finally {
    saving.value = false
  }
}

onMounted(() => { void load() })
</script>

<template>
  <div
    v-if="preferences || loadError"
    class="control-row desktop-preferences"
    data-testid="desktop-close-behavior"
  >
    <div class="control-row__label-block">
      <label
        :for="preferences ? 'desktop-close-behavior-select' : undefined"
        class="control-row__label"
      >{{ t('setup.runtime.closeBehaviorLabel') }}</label>
      <span id="desktop-close-behavior-description" class="control-row__desc">
        {{ t('setup.runtime.closeBehaviorDesc') }}
        <template v-if="preferences && !preferences.canRunInBackground && !unavailableSelection">
          {{ t('setup.runtime.closeBehaviorBackgroundUnavailable') }}
        </template>
      </span>
    </div>
    <div v-if="preferences" class="control-row__control">
      <span v-if="saving" class="desktop-preferences__saving" role="status">
        {{ t('setup.runtime.closeBehaviorSaving') }}
      </span>
      <span
        v-else-if="unavailableSelection"
        id="desktop-close-behavior-mismatch"
        class="desktop-preferences__error"
        role="alert"
        data-testid="desktop-close-behavior-mismatch"
      >{{ t('setup.runtime.closeBehaviorFallbackQuit') }}</span>
      <select
        id="desktop-close-behavior-select"
        class="control-input desktop-preferences__select"
        data-testid="desktop-close-behavior-select"
        :value="value"
        :disabled="saving"
        :aria-describedby="unavailableSelection
          ? 'desktop-close-behavior-description desktop-close-behavior-mismatch'
          : 'desktop-close-behavior-description'"
        @change="save"
      >
        <option value="background" :disabled="!preferences.canRunInBackground">
          {{ t('setup.runtime.closeBehaviorBackground') }}
        </option>
        <option value="quit">{{ t('setup.runtime.closeBehaviorQuit') }}</option>
        <option value="ask" :disabled="!preferences.canRunInBackground">
          {{ t('setup.runtime.closeBehaviorAsk') }}
        </option>
      </select>
    </div>
    <div v-else class="control-row__control">
      <span class="desktop-preferences__error" role="alert" data-testid="desktop-close-behavior-error">
        {{ t('setup.runtime.closeBehaviorReadFailed', { error: loadError }) }}
      </span>
      <button
        type="button"
        class="btn btn--ghost"
        data-testid="desktop-close-behavior-retry"
        :disabled="loading"
        @click="load"
      >{{ t('setup.runtime.closeBehaviorRetry') }}</button>
    </div>
  </div>
</template>

<style scoped>
.desktop-preferences__select { min-width: 220px; }
.desktop-preferences__saving { color: var(--text-dim); font-size: var(--fs-xs); }
.desktop-preferences__error { color: var(--danger); font-size: var(--fs-xs); }
</style>
