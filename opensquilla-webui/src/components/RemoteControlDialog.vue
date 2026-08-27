<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useRpcStore } from '@/stores/rpc'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()

const rpc = useRpcStore()
const { t } = useI18n()

interface PairingDevice { publicId: string; expiresAt: number; createdAt: number; claimedAt: number | null; lastUsedAt: number | null; lastPeer: string | null; sessionKey: string | null; allowHostExecute: boolean }

const qrSvg = ref('')
const pairingUrl = ref('')
const expiresAtMs = ref(0)
const devices = ref<PairingDevice[]>([])
const creating = ref(false)
const revoking = ref(false)
const error = ref('')
const countdownSec = ref(0)
const deviceName = ref('')
const currentQrPublicId = ref('')
// Granting host command execution is a per-pairing decision, so it always
// starts from the server-side safe default. Persisting a previous opt-in would
// let one past confirmation silently escalate every later pairing, including
// after a revoke or a regenerate.
const allowHostExecute = ref(false)
const tunnelFailed = ref(false)
const safeModeUnavailable = ref(false)
let timer: ReturnType<typeof setInterval> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null

const hasActiveQr = computed(() => qrSvg.value.length > 0 && expiresAtMs.value > Date.now())
// The switch reflects reality (a live QR or any paired device), never the
// history of who created what.
const remoteActive = computed(() => hasActiveQr.value || devices.value.length > 0)
const switchOn = computed(() => remoteActive.value || creating.value)
const countdownLabel = computed(() => {
  const s = Math.max(0, countdownSec.value); const m = Math.floor(s / 60); const r = s % 60
  return `${m}:${r.toString().padStart(2, '0')}`
})

async function loadDeviceName(): Promise<void> {
  try {
    const payload = await rpc.call<{ machine_name?: string }>('gateway.identity.get')
    deviceName.value = payload?.machine_name ?? ''
  } catch { /* best-effort */ }
}

async function refreshDevices(): Promise<void> {
  try {
    const payload = await rpc.call<{ pairings: PairingDevice[] }>('gateway.pairing.list')
    const nowSec = Math.floor(Date.now() / 1000)
    devices.value = (payload?.pairings ?? []).filter(d => d.expiresAt > nowSec)
  } catch { /* best-effort */ }
}

/** The gateway reports epoch seconds; normalise to the milliseconds Date.now() uses. */
function toEpochMs(expiresAt: number, expiresAtMs?: number): number {
  if (typeof expiresAtMs === 'number' && Number.isFinite(expiresAtMs)) return expiresAtMs
  if (!Number.isFinite(expiresAt)) return 0
  // Seconds-based timestamps are ~1e9; millisecond ones are ~1e12.
  return expiresAt < 1e11 ? expiresAt * 1000 : expiresAt
}

async function createPairing(): Promise<void> {
  creating.value = true; error.value = ''; tunnelFailed.value = false; safeModeUnavailable.value = false;
  try {
    const payload = await rpc.call<{ pairingUrl: string; qrCodeData: string; expiresAt: number; expiresAtMs?: number; publicId: string; safeModeUnavailableReason?: string | null }>('gateway.pairing.create', { allowHostExecute: allowHostExecute.value })
    // Without host.execute the phone is confined to Safe mode. If this host
    // cannot run Safe, every send from that phone is rejected server-side, so
    // say so up front instead of letting the owner debug a silent composer.
    safeModeUnavailable.value = Boolean(payload.safeModeUnavailableReason)
    const deadlineMs = toEpochMs(payload.expiresAt, payload.expiresAtMs)
    qrSvg.value = payload.qrCodeData; pairingUrl.value = payload.pairingUrl; expiresAtMs.value = deadlineMs;
    currentQrPublicId.value = payload.publicId; startCountdown(deadlineMs); await refreshDevices()
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e)
    error.value = message
    // Remote control is tunnel-only. The most common local cause is a proxy
    // in TUN mode hijacking cloudflared's outbound connection, so surface the
    // remedy instead of a bare failure string.
    tunnelFailed.value = /tunnel|cloudflared/i.test(message)
    clearQr()
  } finally { creating.value = false }
}

function onToggle(): void {
  if (creating.value || revoking.value) return
  if (remoteActive.value) { void revokeAll() } else { void loadDeviceName(); void createPairing() }
}

async function revokeDevice(publicId: string): Promise<void> {
  revoking.value = true;
  try {
    await rpc.call('gateway.pairing.revoke', { publicId })
    devices.value = devices.value.filter(d => d.publicId !== publicId)
    if (publicId === currentQrPublicId.value) clearQr()
    // Revoking any device withdraws the grant, so the pending opt-in for the
    // next code resets even when a different device held the live QR.
    else allowHostExecute.value = false
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) } finally { revoking.value = false }
}

async function revokeAll(): Promise<void> {
  if (!devices.value.length && !currentQrPublicId.value) return
  revoking.value = true;
  try {
    const ids = new Set(devices.value.map(d => d.publicId)); if (currentQrPublicId.value) ids.add(currentQrPublicId.value)
    await Promise.all([...ids].map(id => rpc.call('gateway.pairing.revoke', { publicId: id })))
    devices.value = []; clearQr()
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) } finally { revoking.value = false }
}

function clearQr(): void {
  // Revoking or letting a code expire ends the privilege grant, so the next
  // pairing has to be confirmed again from scratch.
  safeModeUnavailable.value = false; allowHostExecute.value = false;
  qrSvg.value = ''; pairingUrl.value = ''; expiresAtMs.value = 0; countdownSec.value = 0; currentQrPublicId.value = ''; if (timer) { clearInterval(timer); timer = null }
}

function startCountdown(expiresAt: number): void {
  if (timer) clearInterval(timer)
  const tick = (): void => {
    const left = Math.floor((expiresAt - Date.now()) / 1000); countdownSec.value = left;
    // Expiry only clears the QR; paired devices keep the switch on and a
    // regenerate affordance appears instead.
    if (left <= 0) clearQr()
  }; tick(); timer = setInterval(tick, 1000)
}

async function copyLink(): Promise<void> {
  try { await navigator.clipboard.writeText(pairingUrl.value) } catch { /* unavailable */ }
}

function onKeydown(e: KeyboardEvent): void { if (e.key === 'Escape') emit('close') }

watch(() => props.open, (open) => {
  if (open) {
    // Each time the dialog opens, host execution starts from the safe default
    // so a stale opt-in can never be inherited by a new pairing.
    allowHostExecute.value = false
    void loadDeviceName(); void refreshDevices()
    if (!pollTimer) pollTimer = setInterval(() => { void refreshDevices() }, 5000)
  } else {
    clearQr()
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  }
})
onMounted(() => { window.addEventListener('keydown', onKeydown) })
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown); if (timer) clearInterval(timer)
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="rc-overlay" @click.self="emit('close')">
      <section class="rc-modal" role="dialog" aria-modal="true" aria-label="Mobile remote control">
        <header class="rc-modal__head">
          <h2 class="rc-modal__title">{{ t('remoteControl.title') }}</h2>
          <button type="button" class="btn btn--icon btn--ghost" :aria-label="t('common.close')" @click="emit('close')">
            <Icon name="x" :size="16" />
          </button>
        </header>
        <div class="rc-body">
          <div class="rc-toggle-row" :class="{ 'is-on': switchOn }">
            <div class="rc-toggle-text">
              <span class="rc-toggle-label">{{ t('remoteControl.enable') }}</span>
              <span class="rc-toggle-sub">{{ t('remoteControl.enableDesc') }}</span>
            </div>
            <button
              type="button"
              class="rc-toggle"
              :class="{ 'is-active': switchOn }"
              :disabled="creating || revoking"
              :aria-checked="switchOn"
              role="switch"
              @click="onToggle"
            >
              <span class="rc-toggle__thumb"></span>
            </button>
          </div>
          <!-- The error lives outside the enabled-only block: a failed enable
               turns the toggle back off, and the reason must stay on screen. -->
          <p v-if="error" class="rc-error" role="alert">{{ error }}</p>
          <p v-if="tunnelFailed" class="rc-error-hint">{{ t('remoteControl.tunnelHint') }}</p>
          <p v-if="safeModeUnavailable" class="rc-error-hint" role="alert">{{ t('remoteControl.safeModeUnavailable') }}</p>
          <label class="rc-hostexec-row">
            <input
              v-model="allowHostExecute"
              type="checkbox"
              class="rc-hostexec-check"
              :disabled="creating || revoking"
            />
            <span>
              <span class="rc-hostexec-label">{{ t('remoteControl.hostExecute') }}</span>
              <span class="rc-hostexec-warn">{{ t('remoteControl.hostExecuteWarn') }}</span>
              <span v-if="switchOn" class="rc-hostexec-warn">{{ t('remoteControl.hostExecuteAppliesNext') }}</span>
            </span>
          </label>
          <div v-if="creating || remoteActive" class="rc-pair-section">
            <template v-if="creating && !hasActiveQr">
              <div class="rc-loading">{{ t('remoteControl.creating') }}</div>
            </template>
            <template v-else-if="hasActiveQr">
              <label class="rc-device-label">{{ t('remoteControl.deviceName') }}</label>
              <input class="rc-device-name" :value="deviceName" readonly tabindex="-1" />
              <p class="rc-scan-hint">
                {{ t('remoteControl.scanHint') }}
                <button type="button" class="rc-copy-link" @click="copyLink">{{ t('remoteControl.copyLink') }}</button>
              </p>
              <div class="rc-qr-wrap">
                <div class="rc-qr__svg" v-html="qrSvg"></div>
                <span class="rc-qr__countdown" :class="{ 'is-expiring': countdownSec <= 60 }">{{ countdownLabel }}</span>
              </div>
            </template>
            <template v-else>
              <button type="button" class="btn btn--ghost btn--sm" :disabled="creating || revoking" @click="createPairing">
                {{ t('remoteControl.create') }}
              </button>
            </template>
          </div>

          <div v-if="devices.length > 0" class="rc-devices">
            <div class="rc-devices__head">
              <h3>{{ t('remoteControl.devices') }}</h3>
              <button type="button" class="btn btn--ghost btn--sm" :disabled="revoking" @click="revokeAll">
                {{ t('remoteControl.revokeAll') }}
              </button>
            </div>
            <ul class="rc-devices__list">
              <li v-for="device in devices" :key="device.publicId" class="rc-devices__item">
                <span class="rc-devices__peer">{{ device.lastPeer || device.publicId.slice(0, 8) }}</span>
                <span class="rc-devices__state">
                  {{ device.claimedAt ? t('remoteControl.connected') : t('remoteControl.pending') }}
                </span>
                <button type="button" class="btn btn--ghost btn--sm" :disabled="revoking" @click="revokeDevice(device.publicId)">
                  {{ t('remoteControl.revoke') }}
                </button>
              </li>
            </ul>
          </div>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.rc-overlay { align-items: center; background: var(--scrim); display: flex; inset: 0; justify-content: center; padding: var(--sp-6); position: fixed; z-index: 300; }
.rc-modal { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-modal); box-shadow: var(--shadow-xl); display: flex; flex-direction: column; max-height: 85vh; overflow: hidden; width: min(520px, 100%); }
.rc-modal__head { align-items: center; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; padding: var(--sp-4) var(--sp-5); }
.rc-modal__title { font-size: var(--font-lg); margin: 0; }
.rc-body { display: flex; flex-direction: column; gap: var(--sp-4); overflow-y: auto; padding: var(--sp-5); }
.rc-toggle-row { align-items: center; background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius-lg); display: flex; justify-content: space-between; padding: var(--sp-4); transition: border-color var(--dur-fast), background var(--dur-fast); }
.rc-toggle-row.is-on { border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); }
.rc-toggle-text { display: flex; flex-direction: column; gap: 2px; }
.rc-toggle-label { font-weight: 500; }
.rc-toggle-sub { color: var(--text-secondary); font-size: var(--font-sm); }
.rc-toggle { align-items: center; background: var(--border); border: none; border-radius: 999px; cursor: pointer; display: inline-flex; height: 24px; padding: 2px; transition: background var(--dur-fast); width: 44px; flex-shrink: 0; }
.rc-toggle.is-active { background: var(--accent); }
.rc-toggle__thumb { background: var(--bg-surface); border-radius: 50%; height: 20px; transition: transform var(--dur-fast); width: 20px; }
.rc-toggle.is-active .rc-toggle__thumb { transform: translateX(20px); }
.rc-toggle:disabled { opacity: .6; cursor: not-allowed; }
.rc-pair-section { display: flex; flex-direction: column; gap: var(--sp-3); }
.rc-device-label { color: var(--text-secondary); font-size: var(--font-sm); }
.rc-device-name { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-sm); font-family: var(--font-mono); font-size: var(--font-sm); padding: var(--sp-2) var(--sp-3); width: 100%; }
.rc-scan-hint { color: var(--text-secondary); font-size: var(--font-sm); margin: 0; }
.rc-copy-link { background: none; border: none; color: var(--accent); cursor: pointer; font-size: inherit; text-decoration: underline; padding: 0; }
.rc-qr-wrap { align-items: flex-start; display: flex; gap: var(--sp-4); }
.rc-qr__svg :deep(svg) { display: block; height: 160px; width: 160px; }
.rc-qr__countdown { color: var(--text-secondary); font-size: var(--font-sm); font-variant-numeric: tabular-nums; }
.rc-qr__countdown.is-expiring { color: var(--danger); }
.rc-error { color: var(--danger); font-size: var(--font-sm); margin: 0; }
.rc-error-hint { color: var(--text-secondary); font-size: var(--font-sm); line-height: 1.5; margin: 0; }
.rc-loading { color: var(--text-secondary); font-size: var(--font-sm); }
.rc-hostexec-row { align-items: flex-start; cursor: pointer; display: flex; gap: var(--sp-3); padding: 0 var(--sp-1); }
.rc-hostexec-check { accent-color: var(--accent); margin-top: 2px; }
.rc-hostexec-label { display: block; font-size: var(--font-sm); }
.rc-hostexec-warn { color: var(--text-secondary); display: block; font-size: var(--font-sm); }

.rc-devices__head { align-items: center; display: flex; justify-content: space-between; }
.rc-devices__head h3 { font-size: var(--font-md); margin: 0; }
.rc-devices__list { list-style: none; margin: 0; padding: 0; }
.rc-devices__item { align-items: center; border-top: 1px solid var(--border); display: flex; gap: var(--sp-3); justify-content: space-between; padding: var(--sp-3) 0; }
.rc-devices__peer { font-family: var(--font-mono); font-size: var(--font-sm); overflow: hidden; text-overflow: ellipsis; }
.rc-devices__state { color: var(--text-secondary); font-size: var(--font-sm); }
</style>
