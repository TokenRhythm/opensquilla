<script setup lang="ts">
// Members tab body: pairing requests, approved access, channel admins. All
// state and mutations live in useChannelMembers (owned by the view so drafts
// and loads survive tab switches); this component is presentation only.
// Members mutations commit live — visibly unlike the configuration editor's
// draft/save model.
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import LoadingSpinner from '@/components/LoadingSpinner.vue'
import type { ChannelMembersApi, ChannelPairing } from '@/composables/channels/useChannelMembers'

const props = defineProps<{
  members: ChannelMembersApi
  channelName: string
}>()

const { t, locale } = useI18n()

function reload() {
  void props.members.load(props.channelName)
}

function pairingInitial(pairing: ChannelPairing): string {
  return String(pairing.senderName || pairing.senderId || '?').slice(0, 1).toUpperCase()
}
function senderInitial(senderId: string): string {
  return String(senderId || '?').slice(0, 1).toUpperCase()
}
// Timestamps follow the UI locale, not the browser default.
function formatSince(since?: string | number | null): string {
  if (!since) return '—'
  const date = new Date(since)
  return Number.isNaN(date.getTime()) ? String(since) : date.toLocaleString(locale.value)
}
</script>

<template>
  <section class="ch-panel ch-pairings" :aria-busy="members.loading.value">
    <div class="ch-panel__heading">
      <div>
        <h3>{{ t('console.channels.pairings.title') }}</h3>
        <p>{{ t('console.channels.pairings.description') }}</p>
      </div>
      <button
        class="btn btn--ghost"
        type="button"
        :disabled="members.loading.value"
        @click="reload"
      >
        <Icon name="refresh" :size="14" aria-hidden="true" />
        {{ t('console.channels.pairings.refresh') }}
      </button>
    </div>

    <div class="ch-pairing-summary" :aria-label="t('console.channels.pairings.summaryLabel')">
      <span :class="{ 'is-zero': members.pendingPairings.value.length === 0 }"><strong>{{ members.pendingPairings.value.length }}</strong> {{ t('console.channels.pairings.pending') }}</span>
      <span :class="{ 'is-zero': members.approvedPairings.value.length === 0 }"><strong>{{ members.approvedPairings.value.length }}</strong> {{ t('console.channels.pairings.approved') }}</span>
      <span v-if="members.revokedPairings.value.length"><strong>{{ members.revokedPairings.value.length }}</strong> {{ t('console.channels.pairings.revoked') }}</span>
    </div>
    <label v-if="members.pairings.value.length > 0" class="ch-pairing-search">
      <span class="ch-members-sr-only">{{ t('console.channels.pairings.searchLabel') }}</span>
      <Icon name="search" :size="15" aria-hidden="true" />
      <input v-model="members.search.value" type="search" :placeholder="t('console.channels.pairings.searchPlaceholder')" />
    </label>

    <div v-if="members.loading.value && members.pairings.value.length === 0" class="ch-pairing-state" role="status">
      <LoadingSpinner />
      <span>{{ t('console.channels.pairings.loading') }}</span>
    </div>
    <div v-else-if="members.error.value" class="ch-pairing-state is-error" role="alert">
      <Icon name="info" :size="17" aria-hidden="true" />
      <span>{{ members.error.value }}</span>
      <button class="btn btn--ghost" type="button" @click="reload">
        {{ t('console.channels.pairings.tryAgain') }}
      </button>
    </div>
    <div v-else-if="members.pairings.value.length === 0 && members.adminOnlySenders.value.length === 0" class="ch-pairing-state">
      <Icon name="shield" :size="15" aria-hidden="true" />
      <span><strong>{{ t('console.channels.pairings.emptyTitle') }}</strong> — {{ t('console.channels.pairings.emptyDescription') }}</span>
    </div>
    <div v-else class="ch-pairing-groups">
      <section v-if="members.pendingPairings.value.length" :aria-label="t('console.channels.pairings.pendingRequests')">
        <h4>{{ t('console.channels.pairings.pendingRequests') }}</h4>
        <article v-for="pairing in members.pendingPairings.value" :key="pairing.pairingId" class="ch-pairing-row">
          <div class="ch-pairing-avatar" aria-hidden="true">{{ pairingInitial(pairing) }}</div>
          <div class="ch-pairing-identity">
            <strong>{{ pairing.senderName || pairing.senderId }}</strong>
            <span v-if="pairing.senderName" class="ch-members-mono">{{ pairing.senderId }}</span>
            <span v-if="pairing.pairingCode" class="ch-members-mono ch-pairing-code">{{ t('console.channels.pairings.requestCode', { code: pairing.pairingCode }) }}</span>
            <time v-if="pairing.createdAt" :datetime="pairing.createdAt">{{ t('console.channels.pairings.requestedAt', { time: formatSince(pairing.createdAt) }) }}</time>
          </div>
          <span class="ch-pairing-status is-pending">{{ t('console.channels.pairings.pending') }}</span>
          <div class="ch-pairing-actions">
            <label class="ch-pairing-asadmin" :title="t('console.channels.pairings.asAdminHint')">
              <input
                type="checkbox"
                :checked="members.asAdminChecked(pairing)"
                :aria-label="t('console.channels.pairings.asAdminCheckboxLabel', { sender: pairing.senderName || pairing.senderId })"
                @change="members.setAsAdminChecked(pairing, ($event.target as HTMLInputElement).checked)"
              />
              <span>{{ t(members.isBootstrapPairing(pairing) ? 'console.channels.pairings.asAdminBootstrap' : 'console.channels.pairings.asAdmin') }}</span>
            </label>
            <button
              class="btn btn--primary"
              type="button"
              :disabled="members.actionPending(pairing, 'approve')"
              :aria-label="t('console.channels.pairings.approveLabel', { sender: pairing.senderName || pairing.senderId })"
              @click="members.approve(pairing, members.asAdminChecked(pairing))"
            >
              {{ members.actionPending(pairing, 'approve') ? t('console.channels.pairings.approving') : t('console.channels.pairings.approve') }}
            </button>
          </div>
        </article>
      </section>

      <section v-if="members.approvedPairings.value.length" :aria-label="t('console.channels.pairings.approvedAccess')">
        <h4>{{ t('console.channels.pairings.approvedAccess') }}</h4>
        <article v-for="pairing in members.approvedPairings.value" :key="pairing.pairingId" class="ch-pairing-row">
          <div class="ch-pairing-avatar" aria-hidden="true">{{ pairingInitial(pairing) }}</div>
          <div class="ch-pairing-identity">
            <strong>{{ pairing.senderName || pairing.senderId }}</strong>
            <span v-if="pairing.senderName" class="ch-members-mono">{{ pairing.senderId }}</span>
            <time v-if="pairing.approvedAt" :datetime="pairing.approvedAt">{{ t('console.channels.pairings.approvedAt', { time: formatSince(pairing.approvedAt) }) }}</time>
          </div>
          <span
            class="ch-pairing-status"
            :class="members.isChannelAdmin(pairing.senderId) ? 'is-admin' : 'is-approved'"
          >
            {{ members.isChannelAdmin(pairing.senderId) ? t('console.channels.pairings.adminPill') : t('console.channels.pairings.approved') }}
          </span>
          <div class="ch-pairing-actions">
            <button
              v-if="members.isChannelAdmin(pairing.senderId)"
              class="btn btn--ghost"
              type="button"
              :disabled="members.actionPending(pairing, 'admin')"
              :aria-label="t('console.channels.pairings.removeAdminLabel', { sender: pairing.senderName || pairing.senderId })"
              @click="members.setAdmin(pairing, false)"
            >
              {{ members.actionPending(pairing, 'admin') ? t('console.channels.pairings.updatingAdmin') : t('console.channels.pairings.removeAdmin') }}
            </button>
            <button
              v-else
              class="btn btn--ghost"
              type="button"
              :disabled="members.actionPending(pairing, 'admin')"
              :aria-label="t('console.channels.pairings.setAsAdminLabel', { sender: pairing.senderName || pairing.senderId })"
              @click="members.setAdmin(pairing, true)"
            >
              {{ members.actionPending(pairing, 'admin') ? t('console.channels.pairings.updatingAdmin') : t('console.channels.pairings.setAsAdmin') }}
            </button>
            <button
              class="btn btn--ghost ch-pairing-revoke"
              type="button"
              :disabled="members.actionPending(pairing, 'revoke')"
              :aria-label="t('console.channels.pairings.revokeLabel', { sender: pairing.senderName || pairing.senderId })"
              @click="members.revoke(pairing)"
            >
              {{ members.actionPending(pairing, 'revoke') ? t('console.channels.pairings.revoking') : t('console.channels.pairings.revoke') }}
            </button>
          </div>
        </article>
      </section>

      <section v-if="members.adminOnlySenders.value.length" :aria-label="t('console.channels.pairings.adminsSubtitle')">
        <h4>{{ t('console.channels.pairings.adminsSubtitle') }}</h4>
        <article v-for="senderId in members.adminOnlySenders.value" :key="senderId" class="ch-pairing-row">
          <div class="ch-pairing-avatar" aria-hidden="true">{{ senderInitial(senderId) }}</div>
          <div class="ch-pairing-identity">
            <strong>{{ senderId }}</strong>
            <span class="ch-members-mono">{{ t('console.channels.pairings.adminOnlyHint') }}</span>
          </div>
          <span class="ch-pairing-status is-admin">{{ t('console.channels.pairings.adminPill') }}</span>
          <div class="ch-pairing-actions">
            <button
              class="btn btn--ghost ch-pairing-revoke"
              type="button"
              :disabled="members.adminOnlyActionPending(senderId)"
              :aria-label="t('console.channels.pairings.removeAdminLabel', { sender: senderId })"
              @click="members.removeAdminOnly(senderId)"
            >
              {{ members.adminOnlyActionPending(senderId) ? t('console.channels.pairings.updatingAdmin') : t('console.channels.pairings.removeAdmin') }}
            </button>
          </div>
        </article>
      </section>

      <section v-if="members.revokedPairings.value.length" :aria-label="t('console.channels.pairings.revokedAccess')">
        <h4>{{ t('console.channels.pairings.revokedAccess') }}</h4>
        <article v-for="pairing in members.revokedPairings.value" :key="pairing.pairingId" class="ch-pairing-row">
          <div class="ch-pairing-avatar" aria-hidden="true">{{ pairingInitial(pairing) }}</div>
          <div class="ch-pairing-identity">
            <strong>{{ pairing.senderName || pairing.senderId }}</strong>
            <span v-if="pairing.senderName" class="ch-members-mono">{{ pairing.senderId }}</span>
          </div>
          <span class="ch-pairing-status is-revoked">{{ t('console.channels.pairings.revoked') }}</span>
          <button
            class="btn btn--ghost"
            type="button"
            :disabled="members.actionPending(pairing, 'reapprove')"
            :aria-label="t('console.channels.pairings.reapproveLabel', { sender: pairing.senderName || pairing.senderId })"
            @click="members.approve(pairing)"
          >
            {{ members.actionPending(pairing, 'reapprove') ? t('console.channels.pairings.approving') : t('console.channels.pairings.reapprove') }}
          </button>
        </article>
      </section>
    </div>
    <p v-if="members.pairings.value.length > 0 || members.adminOnlySenders.value.length" class="ch-pairing-hint">{{ t('console.channels.pairings.membersHint') }}</p>
    <p class="ch-members-sr-only" role="status" aria-live="polite">{{ members.announcement.value }}</p>
  </section>
</template>

<style scoped>
.ch-members-sr-only { height: 1px; margin: -1px; overflow: hidden; padding: 0; position: absolute; width: 1px; clip: rect(0, 0, 0, 0); white-space: nowrap; }
.ch-members-mono { font-family: var(--font-mono); font-size: 11px; }
.ch-pairings { background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; }
.ch-panel__heading { align-items: flex-start; border-bottom: 1px solid var(--border); display: flex; gap: var(--sp-3); justify-content: space-between; margin: 0; padding: 12px 14px; }
.ch-panel__heading h3 { font-size: var(--fs-sm); margin: 0; }
.ch-panel__heading > div { min-width: 0; }
.ch-panel__heading p { color: var(--text-dim); font-size: 11px; line-height: 1.45; margin: 4px 0 0; }
.ch-pairing-summary { align-items: center; border-bottom: 1px solid var(--border); color: var(--text-muted); display: flex; flex-wrap: wrap; font-size: 11px; gap: 6px var(--sp-4); padding: 9px 14px; }
.ch-pairing-summary strong { color: var(--text); font-family: var(--font-mono); }
.ch-pairing-state { align-items: center; color: var(--text-dim); display: flex; flex-direction: column; gap: 8px; justify-content: center; min-height: 180px; padding: var(--sp-4); text-align: center; }
.ch-pairing-state.is-error { color: var(--danger); }
.ch-pairing-groups > section + section { border-top: 1px solid var(--border); }
.ch-pairing-groups h4 { color: var(--text-dim); font-size: 11px; letter-spacing: .04em; margin: 0; padding: 11px 14px 7px; text-transform: uppercase; }
.ch-pairing-row { align-items: center; display: grid; gap: 10px; grid-template-columns: 32px minmax(0, 1fr) auto auto; padding: 10px 14px; }
.ch-pairing-row + .ch-pairing-row { border-top: 1px solid var(--border); }
.ch-pairing-avatar { align-items: center; background: var(--bg-surface-2); border: 1px solid var(--border); border-radius: 50%; color: var(--text-muted); display: flex; font-size: 12px; font-weight: 800; height: 32px; justify-content: center; width: 32px; }
.ch-pairing-identity { display: grid; gap: 2px; min-width: 0; }
.ch-pairing-identity strong, .ch-pairing-identity span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ch-pairing-identity strong { font-size: var(--fs-sm); }
.ch-pairing-identity .ch-pairing-code { color: var(--warn); font-size: 10px; }
.ch-pairing-identity time { color: var(--text-dim); font-size: 10px; }
.ch-pairing-status { border: 1px solid var(--border); border-radius: var(--radius-full); font-size: 10px; font-weight: 700; padding: 3px 8px; text-transform: uppercase; }
.ch-pairing-status.is-pending { border-color: color-mix(in srgb, var(--warn) 42%, var(--border)); color: var(--warn); }
.ch-pairing-status.is-approved { border-color: color-mix(in srgb, var(--ok) 42%, var(--border)); color: var(--ok); }
.ch-pairing-status.is-revoked { border-color: color-mix(in srgb, var(--danger) 42%, var(--border)); color: var(--danger); }
.ch-pairing-status.is-admin { background: color-mix(in srgb, var(--accent) 14%, transparent); border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); color: var(--accent); }
.ch-pairing-actions { align-items: center; display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }
.ch-pairing-asadmin { align-items: center; color: var(--text-dim); cursor: pointer; display: inline-flex; font-size: 11px; gap: 5px; user-select: none; white-space: nowrap; }
.ch-pairing-asadmin input { accent-color: var(--accent); cursor: pointer; margin: 0; }
.ch-pairing-hint { color: var(--text-dim); font-size: 11px; line-height: 1.45; margin: 0; padding: 10px 14px 12px; }
.ch-pairing-search { align-items: center; border-bottom: 1px solid var(--border); color: var(--text-dim); display: flex; gap: 8px; padding: 8px 14px; }
.ch-pairing-search input { background: transparent; border: 0; color: var(--text); font: inherit; outline: 0; width: 100%; }
.ch-pairing-revoke { color: var(--danger); }

@media (max-width: 768px) {
  .ch-pairing-row { grid-template-columns: 32px minmax(0, 1fr) auto; }
  .ch-pairing-row > .btn { grid-column: 2 / -1; justify-self: start; }
  .ch-pairing-actions { grid-column: 1 / -1; justify-content: flex-start; }
}
</style>
