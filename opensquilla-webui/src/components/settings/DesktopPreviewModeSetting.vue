<script setup lang="ts">
import { onMounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import { useToasts } from '@/composables/useToasts'
import { usePlatform, type DesktopPreferences, type WorkbenchPreviewMode } from '@/platform'

const { t } = useI18n()
const { pushToast } = useToasts()
const platform = usePlatform()
const preferences = shallowRef<DesktopPreferences | null>(null)
const value = ref<WorkbenchPreviewMode>('full')
const saving = ref(false)

async function load() {
  if (!platform.settings.getDesktopPreferences || !platform.settings.saveDesktopPreferences) return
  try {
    const next = await platform.settings.getDesktopPreferences()
    preferences.value = next
    value.value = next.workbenchPreviewMode ?? 'full'
  } catch {
    // This optional desktop preference stays hidden when an older shell or an
    // unavailable bridge cannot provide it.
  }
}

async function save(event: Event) {
  const next = (event.target as HTMLSelectElement).value as WorkbenchPreviewMode
  const previous = value.value
  if (!platform.settings.saveDesktopPreferences) return
  value.value = next
  saving.value = true
  try {
    const saved = await platform.settings.saveDesktopPreferences({ workbenchPreviewMode: next })
    preferences.value = saved
    value.value = saved.workbenchPreviewMode ?? next
  } catch {
    value.value = previous
    pushToast(t('setup.runtime.previewModeSaveFailed'), { tone: 'danger' })
  } finally {
    saving.value = false
  }
}

onMounted(() => { void load() })
</script>

<template>
  <div v-if="preferences" class="control-row" data-testid="desktop-preview-mode">
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
        v-if="preferences.workbenchPreviewForcedOffline"
        id="desktop-preview-mode-forced"
        class="desktop-preferences__error"
        role="status"
        data-testid="desktop-preview-mode-forced"
      >{{ t('setup.runtime.previewModeForcedOffline') }}</span>
      <span v-else-if="saving" class="desktop-preferences__saving" role="status">
        {{ t('setup.runtime.previewModeSaving') }}
      </span>
      <select
        id="desktop-preview-mode-select"
        class="control-input desktop-preferences__select"
        data-testid="desktop-preview-mode-select"
        :value="value"
        :disabled="saving"
        :aria-describedby="preferences.workbenchPreviewForcedOffline
          ? 'desktop-preview-mode-description desktop-preview-mode-forced'
          : 'desktop-preview-mode-description'"
        @change="save"
      >
        <option value="full">{{ t('setup.runtime.previewModeFull') }}</option>
        <option value="offline">{{ t('setup.runtime.previewModeOffline') }}</option>
      </select>
    </div>
  </div>
</template>

<style scoped>
.desktop-preferences__select { min-width: 220px; }
.desktop-preferences__saving { color: var(--text-dim); font-size: var(--fs-xs); }
.desktop-preferences__error { color: var(--danger); font-size: var(--fs-xs); }
</style>
