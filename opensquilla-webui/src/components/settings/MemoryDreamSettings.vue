<script setup lang="ts">
import { inject, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import { useMemoryDreamSettings } from '@/composables/setup/useMemoryDreamSettings'
import { APP_SETTINGS_KEY } from '@/modules/appSettings'

const { t } = useI18n()
const appSettings = inject(APP_SETTINGS_KEY)
if (!appSettings) throw new Error('AppSettings was not provided')
const settings = useMemoryDreamSettings(appSettings)
onMounted(() => { void settings.load().catch(() => {}) })
</script>

<template>
  <div data-testid="memory-dream-settings">
    <label class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">
          {{ t('setup.memoryDream.dreamLabel') }}
          <span class="dream-token-badge">{{ t('setup.memoryDream.tokenBadge') }}</span>
        </span>
        <span class="control-row__desc">{{ t('setup.memoryDream.dreamDesc') }}</span>
        <span v-if="settings.restartRequired.value" class="control-row__desc" role="status">
          {{ t('setup.memoryDream.restartRequired') }}
        </span>
      </div>
      <div class="control-row__control">
        <ControlSwitch
          name="memory_dream_enabled"
          :checked="settings.dreamEnabled.value"
          :busy="settings.busy.value || !settings.loaded.value"
          :aria-label="t('setup.memoryDream.dreamLabel')"
          @change="settings.setDream"
        />
      </div>
    </label>
  </div>
</template>

<style scoped>
.dream-token-badge {
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  color: var(--text-dim);
  font-size: 0.6875rem;
  font-weight: 400;
  margin-left: var(--sp-2);
  padding: 1px var(--sp-2);
}
</style>
