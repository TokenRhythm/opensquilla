<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { usePlatform } from '@/platform'
import { useToasts } from '@/composables/useToasts'

const { t } = useI18n()
const platform = usePlatform()
const { pushToast } = useToasts()
const busy = ref(false)

async function revealLog() {
  if (!platform.gateway.revealLog || busy.value) return
  busy.value = true
  try {
    const ok = await platform.gateway.revealLog()
    if (!ok) pushToast(t('setup.runtime.noLogToReveal'), { tone: 'danger' })
  } catch (err) {
    pushToast(t('setup.runtime.revealFailed', {
      error: err instanceof Error ? err.message : String(err),
    }), { tone: 'danger' })
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div
    v-if="platform.gateway.revealLog"
    id="settings-gateway-local-logs"
    tabindex="-1"
    :aria-label="t('setup.runtime.openLocalLogLocation')"
  >
    <button
      type="button"
      class="btn"
      data-testid="support-open-local-logs"
      :disabled="busy"
      :aria-busy="busy"
      @click="revealLog"
    >
      <Icon name="folder" :size="16" aria-hidden="true" />
      <span>{{ t('setup.runtime.openLocalLogLocation') }}</span>
    </button>
  </div>
</template>

<style scoped>
.btn { min-height: 36px; }
#settings-gateway-local-logs:focus { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
