<script setup lang="ts">
// Shared alert strip for a channel: the amber pending-pairing banner (approve
// or reject right here, with the first-pairing admin bootstrap default) and
// the red unhealthy banner (last error snippet + restart + jump to credential
// edit). Rendered identically on the dashboard card and at the top of the
// drill-in page so the two surfaces cannot drift.
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChannelPairing } from '@/composables/channels/useChannelMembers'

const props = withDefaults(defineProps<{
  pendingPairing?: ChannelPairing | null
  /** More pending requests than the one shown inline. */
  pendingOverflow?: number
  /** First-pairing bootstrap: no approved members and no admins yet. */
  defaultAsAdmin?: boolean
  /**
   * Host-owned "approve as admin" state. When supplied, the checkbox is fully
   * controlled — toggles emit `setAsAdmin` and the host (the drill page, which
   * single-sources the override in its members store) owns the value, so this
   * banner and the Members row can never disagree about the same pairing.
   * Leave undefined for the local fallback used by the dashboard cards.
   */
  asAdminChecked?: boolean
  errorText?: string
  /** Offer the jump into credential editing on the error banner. */
  showFixCredentials?: boolean
  busy?: boolean
}>(), { asAdminChecked: undefined })

const emit = defineEmits<{
  approve: [asAdmin: boolean]
  reject: []
  restart: []
  fixCredentials: []
  setAsAdmin: [asAdmin: boolean]
}>()

const { t } = useI18n()

// Local fallback only (no `asAdminChecked` prop): an entry here is an explicit
// operator choice overriding the bootstrap default, recorded WITH the pairing
// it was made for. Keying by pairingId means a transient blip (the pairing
// facts briefly failing and re-resolving to the SAME request) cannot wipe the
// choice, while a DIFFERENT request never inherits it.
const adminOverride = ref<{ pairingId: string; value: boolean } | null>(null)
watch(() => props.pendingPairing?.pairingId, id => {
  if (id && adminOverride.value && id !== adminOverride.value.pairingId) {
    adminOverride.value = null
  }
})
function isChecked(): boolean {
  if (props.asAdminChecked !== undefined) return props.asAdminChecked
  const pairing = props.pendingPairing
  if (pairing && adminOverride.value?.pairingId === pairing.pairingId) {
    return adminOverride.value.value
  }
  return Boolean(props.defaultAsAdmin)
}
function setChecked(value: boolean): void {
  if (props.asAdminChecked !== undefined) {
    emit('setAsAdmin', value)
    return
  }
  const pairing = props.pendingPairing
  if (pairing) adminOverride.value = { pairingId: pairing.pairingId, value }
}
</script>

<template>
  <div v-if="pendingPairing || errorText" class="chal-stack">
    <div v-if="pendingPairing" class="chal chal--pending" role="status" @click.stop>
      <span class="chal__text">
        {{ t('console.channels.home.pendingRequest', {
          sender: pendingPairing.senderName || pendingPairing.senderId,
          code: pendingPairing.pairingCode || '—',
        }) }}
        <template v-if="pendingOverflow"> · +{{ pendingOverflow }}</template>
      </span>
      <span class="chal__actions">
        <label class="chal__asadmin" :title="t('console.channels.pairings.asAdminHint')">
          <input
            type="checkbox"
            :checked="isChecked()"
            :aria-label="t('console.channels.pairings.asAdminCheckboxLabel', { sender: pendingPairing.senderName || pendingPairing.senderId })"
            @change="setChecked(($event.target as HTMLInputElement).checked)"
          />
          <span>{{ t(defaultAsAdmin ? 'console.channels.pairings.asAdminBootstrap' : 'console.channels.pairings.asAdmin') }}</span>
        </label>
        <button
          type="button"
          class="btn btn--primary chal__btn"
          :disabled="busy"
          :aria-label="t('console.channels.pairings.approveLabel', { sender: pendingPairing.senderName || pendingPairing.senderId })"
          @click.stop="emit('approve', isChecked())"
        >{{ t('console.channels.pairings.approve') }}</button>
        <button
          type="button"
          class="btn btn--ghost chal__btn"
          :disabled="busy"
          :aria-label="t('console.channels.home.rejectLabel', { sender: pendingPairing.senderName || pendingPairing.senderId })"
          @click.stop="emit('reject')"
        >{{ t('console.channels.home.reject') }}</button>
      </span>
    </div>

    <div v-if="errorText" class="chal chal--error" role="alert" @click.stop>
      <span class="chal__text">{{ errorText }}</span>
      <span class="chal__actions">
        <button type="button" class="btn btn--ghost chal__btn" :disabled="busy" @click.stop="emit('restart')">
          {{ t('console.channels.restart') }}
        </button>
        <button
          v-if="showFixCredentials"
          type="button"
          class="btn btn--primary chal__btn"
          @click.stop="emit('fixCredentials')"
        >{{ t('console.channels.home.fixCredentials') }}</button>
      </span>
    </div>
  </div>
</template>

<style scoped>
.chal-stack { display: grid; gap: var(--sp-2); min-width: 0; }
.chal {
  align-items: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  display: flex;
  flex-wrap: wrap;
  font-size: var(--fs-sm);
  gap: var(--sp-2);
  padding: 8px 12px;
}
.chal--pending {
  background: color-mix(in srgb, var(--warn) 9%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--warn) 38%, var(--border));
  color: var(--warn);
}
.chal--error {
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-surface));
  border-color: color-mix(in srgb, var(--danger) 38%, var(--border));
  color: var(--danger);
}
.chal__text { min-width: 0; overflow-wrap: anywhere; }
.chal__actions { align-items: center; display: flex; flex-wrap: wrap; gap: 6px; margin-left: auto; }
.chal__btn { min-height: 26px; padding: 2px 10px; font-size: var(--fs-xs); }
.chal__asadmin { align-items: center; color: var(--text-dim); cursor: pointer; display: inline-flex; font-size: var(--fs-xs); gap: 5px; user-select: none; white-space: nowrap; }
.chal__asadmin input { accent-color: var(--accent); cursor: pointer; margin: 0; }
</style>
