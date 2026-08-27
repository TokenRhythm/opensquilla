<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import GatewayStatusBlock from '@/components/settings/GatewayStatusBlock.vue'
import SettingsUpdatePanel from '@/components/settings/SettingsUpdatePanel.vue'
import {
  usePlatform,
  type GatewayStatus,
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

</style>
