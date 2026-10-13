<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { GATEWAY_ACCESS_KEY } from '@/modules/gatewayAccess'

const props = withDefaults(defineProps<{ managed?: boolean }>(), { managed: false })
const { t } = useI18n()

// Connection recovery must work without catalog/readiness RPCs.
const injectedGatewayAccess = inject(GATEWAY_ACCESS_KEY)
if (!injectedGatewayAccess) throw new Error('GatewayAccess was not provided')
const gatewayAccess = injectedGatewayAccess

const wsUrl = ref('')
const wsToken = ref('')
const tokenInput = ref<HTMLInputElement | null>(null)
const connectionEditorOpen = ref(false)
const connectionEditorInteracted = ref(false)
const requiresCredential = computed(() => !props.managed && gatewayAccess.requiresCredential)

watch(requiresCredential, required => {
  if (required) tokenInput.value?.focus()
}, { flush: 'post' })

onMounted(() => {
  if (!props.managed) wsUrl.value = gatewayAccess.loadConnectionEndpoint()
  if (requiresCredential.value) tokenInput.value?.focus()
})

const statusState = computed(() => {
  if (gatewayAccess.availability === 'preparing') return 'connecting'
  if (gatewayAccess.availability === 'available') {
    return gatewayAccess.connectionPhase !== undefined
      ? gatewayAccess.connectionPhase !== 'healthy' ? 'connecting' : 'connected'
      : gatewayAccess.connectionHealth === 'suspect' ? 'connecting' : 'connected'
  }
  return 'disconnected'
})

const transportPhase = computed(() => {
  if (gatewayAccess.availability === 'available') {
    return gatewayAccess.connectionPhase
      || (gatewayAccess.isResuming || gatewayAccess.connectionHealth === 'suspect' ? 'suspect' : 'healthy')
  }
  return gatewayAccess.availability === 'preparing' ? 'checking' : 'disconnected'
})

// Show recovery controls immediately; preserve an editor the user has touched.
const needsRecovery = computed(() => transportPhase.value !== 'healthy'
  || requiresCredential.value || !!gatewayAccess.connectionError)

watch([transportPhase, requiresCredential, () => gatewayAccess.connectionError], () => {
  if (needsRecovery.value) {
    connectionEditorOpen.value = true
  } else if (!connectionEditorInteracted.value) {
    connectionEditorOpen.value = false
  }
}, { immediate: true })

function syncConnectionEditor(event: Event) {
  connectionEditorOpen.value = (event.currentTarget as HTMLDetailsElement).open
}

const statusPillClass = computed(() => {
  if (statusState.value === 'connected') return 'ok'
  if (statusState.value === 'connecting') return 'warn'
  return 'err'
})

const statusLabel = computed(() => {
  if (requiresCredential.value) return t('setup.connection.tokenRequired')
  if (transportPhase.value !== 'healthy' && transportPhase.value !== 'disconnected') {
    return t(`chrome.connectionState.${transportPhase.value}`)
  }
  if (statusState.value === 'connected') return t('setup.connection.connected')
  if (statusState.value === 'connecting') return t('setup.connection.connecting')
  return t('setup.connection.disconnected')
})

const statusReason = computed(() => {
  if (requiresCredential.value) return t('setup.connection.reasonTokenRequired')
  if (gatewayAccess.connectionError) {
    return t('setup.connection.reasonFailed', { error: gatewayAccess.connectionError })
  }
  if (statusState.value === 'connected') {
    return (!props.managed && gatewayAccess.connectedGatewayHost)
      || t('setup.connection.reasonConnected')
  }
  if (statusState.value === 'connecting') return t('setup.connection.reasonConnecting')
  return t(props.managed ? 'setup.connection.reasonManagedDisconnected' : 'setup.connection.reasonDisconnected')
})

function connect() {
  const url = props.managed ? '' : wsUrl.value.trim()
  const token = props.managed ? '' : wsToken.value.trim()
  void gatewayAccess.connect({ endpoint: url, credential: token || undefined })
}

function disconnect() {
  gatewayAccess.disconnect()
}
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('setup.connection.title') }}</h3>
      <p v-if="!managed" class="control-section__desc">{{ t('setup.connection.desc') }}</p>
    </div>

    <div class="conn-status" :class="statusPillClass" role="status" aria-live="polite">
      <span class="conn-status__pill" :class="statusPillClass">{{ statusLabel }}</span>
      <span class="conn-status__reason">{{ statusReason }}</span>
    </div>

    <details
      id="settings-connection-details"
      :open="connectionEditorOpen"
      class="conn-editor"
      @toggle="syncConnectionEditor"
      @input="connectionEditorInteracted = true"
      @focusin="connectionEditorInteracted = true"
    >
      <summary @click="connectionEditorInteracted = true">{{ t(managed ? 'setup.connection.actions' : 'setup.connection.editConnection') }}</summary>

      <div v-if="!managed" class="control-row control-row--stack">
        <div class="control-row__label-block">
          <label class="control-row__label" for="conn-ws-url">{{ t('setup.connection.wsUrlLabel') }}</label>
          <span class="control-row__desc">{{ t('setup.connection.wsUrlDesc') }} <code>ws://host:port/ws</code></span>
        </div>
        <div class="control-row__control">
          <input
            id="conn-ws-url"
            v-model="wsUrl"
            class="control-input conn-input--mono"
            type="text"
            placeholder="ws://..."
            autocomplete="off"
            spellcheck="false"
          >
        </div>
      </div>

      <div v-if="!managed" class="control-row control-row--stack">
        <div class="control-row__label-block">
          <label class="control-row__label" for="conn-ws-token">{{ t('setup.connection.tokenLabel') }} <span v-if="!requiresCredential" class="conn-optional">{{ t('setup.connection.optional') }}</span></label>
          <span class="control-row__desc">{{ t('setup.connection.tokenDesc') }}</span>
        </div>
        <div class="control-row__control">
          <input
            id="conn-ws-token"
            ref="tokenInput"
            v-model="wsToken"
            :data-settings-initial-focus="requiresCredential ? '' : undefined"
            class="control-input"
            type="password"
            placeholder="&mdash;"
            autocomplete="off"
            @keydown.enter.prevent="connect"
          >
        </div>
      </div>

      <div class="conn-actions">
        <button type="button" class="btn" :class="{ 'btn--primary': needsRecovery }" @click="connect">
          {{ statusState === 'connected' ? t('setup.connection.reconnect') : t('setup.connection.connect') }}
        </button>
        <button type="button" class="btn" @click="disconnect">{{ t('setup.connection.disconnect') }}</button>
      </div>
    </details>
  </section>
</template>

<style scoped>
.conn-editor > summary {
  cursor: pointer;
  font-size: var(--fs-sm);
  font-weight: 600;
  min-height: 44px;
  padding: var(--sp-3) 0;
}

.conn-editor > summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.conn-status {
  align-items: baseline;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-bottom: var(--sp-4);
  padding: var(--sp-3);
}

.conn-status.ok {
  background: color-mix(in srgb, var(--ok) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--ok) 35%, var(--border));
  padding: var(--sp-2) var(--sp-3);
}

.conn-status.ok .conn-status__reason { min-height: 0; }

.conn-status.warn {
  background: color-mix(in srgb, var(--warn) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--warn) 35%, var(--border));
}

.conn-status.err {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 35%, var(--border));
}

.conn-status__pill {
  border-radius: var(--radius-full);
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  padding: 3px 10px;
  text-transform: uppercase;
}

.conn-status__pill.ok { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
.conn-status__pill.warn { background: color-mix(in srgb, var(--warn) 16%, transparent); color: var(--warn); }
.conn-status__pill.err { background: color-mix(in srgb, var(--danger) 16%, transparent); color: var(--danger); }

.conn-status__reason {
  color: var(--text-muted);
  flex: 1 1 220px;
  font-size: var(--fs-sm);
  /* Reserve recovery space without clipping longer errors. */
  line-height: 1.3;
  min-height: 2.6em;
  min-width: 0;
  overflow-wrap: anywhere;
}

.conn-input--mono {
  font-family: var(--font-mono);
}

.conn-optional {
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 400;
}

.conn-actions {
  display: flex;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
}
</style>
