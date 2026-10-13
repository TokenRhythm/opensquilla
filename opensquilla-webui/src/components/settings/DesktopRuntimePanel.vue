<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
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
const statusReadError = ref('')

const STATUS_KEYS: Record<string, string> = {
  starting: 'setup.runtime.statusStarting',
  ready: 'setup.runtime.statusReady',
  stopped: 'setup.runtime.statusStopped',
  error: 'setup.runtime.statusError',
}

const statusLabel = computed(() => {
  if (statusReadError.value) return t('setup.runtime.statusUnknown')
  const key = STATUS_KEYS[gateway.value?.status ?? '']
  return key ? t(key) : t('setup.runtime.statusUnknown')
})
const gatewayError = computed(() => statusReadError.value || gateway.value?.error || '')
const url = computed(() => gateway.value?.url || t('setup.runtime.noActiveGateway'))
const logHint = computed(() => gateway.value?.logPath || t('setup.runtime.noLogPath'))
const canRestart = computed(() => Boolean(platform.gateway.retryStartup))

async function loadStatus(): Promise<GatewayStatus | null> {
  loading.value = true
  try {
    const status = await platform.gateway.getStatus()
    gateway.value = status
    statusReadError.value = ''
    return status
  } catch (err) {
    statusReadError.value = t('setup.runtime.statusReadFailed', {
      error: err instanceof Error ? err.message : String(err),
    })
    pushToast(statusReadError.value, { tone: 'danger' })
    return null
  } finally {
    loading.value = false
  }
}

async function restartGateway(): Promise<GatewayStatus | null> {
  if (!platform.gateway.retryStartup || busy.value) return null
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
      <h3 class="control-section__title">{{ t('setup.runtime.localGatewayTitle') }}</h3>
      <p class="control-section__desc">{{ t('setup.runtime.localGatewayDescription') }}</p>
    </div>

    <div class="runtime-summary">
      <p class="runtime-status" role="status" :aria-busy="loading">
        <span>{{ t('setup.runtime.processStatus') }}</span>
        <strong :class="{ 'runtime-status--error': gatewayError || gateway?.status === 'error' }">
          {{ loading ? t('setup.runtime.loading') : statusLabel }}
        </strong>
      </p>
      <div class="runtime-actions">
        <button type="button" class="btn btn--ghost" data-testid="runtime-refresh-status" :disabled="loading || busy" @click="loadStatus">
          <Icon name="refresh" :size="15" aria-hidden="true" />
          <span>{{ t('setup.runtime.refreshStatus') }}</span>
        </button>
        <button v-if="canRestart" type="button" class="btn btn--ghost" data-testid="runtime-restart-gateway" :disabled="busy" @click="restartGateway">
          <Icon name="refresh" :size="15" aria-hidden="true" />
          <span>{{ t('setup.runtime.restartLocalGateway') }}</span>
        </button>
      </div>
    </div>
    <p v-if="gatewayError" class="runtime-error" role="alert">{{ gatewayError }}</p>
    <details class="runtime-details" data-testid="runtime-details">
      <summary>{{ t('setup.runtime.details') }}</summary>
      <dl>
        <div>
          <dt>{{ t('setup.runtime.address') }}</dt>
          <dd>{{ url }}</dd>
        </div>
        <div>
          <dt>{{ t('setup.runtime.gatewayLog') }}</dt>
          <dd>{{ logHint }}</dd>
        </div>
      </dl>
    </details>
  </section>
</template>

<style scoped>
.runtime-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--sp-3);
  justify-content: space-between;
}

.runtime-status {
  display: flex;
  align-items: baseline;
  gap: var(--sp-2);
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--text-muted);
}

.runtime-status strong { color: var(--text); }
.runtime-status strong.runtime-status--error,
.runtime-error { color: var(--danger); }

.runtime-error {
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.runtime-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

.runtime-details {
  margin-top: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--text-muted);
}

.runtime-details summary {
  cursor: pointer;
  width: fit-content;
}

.runtime-details summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.runtime-details dl {
  display: grid;
  gap: var(--sp-3);
  margin: var(--sp-3) 0 0;
}

.runtime-details dt { font-weight: 600; }
.runtime-details dd { margin: var(--sp-1) 0 0; overflow-wrap: anywhere; }

</style>
