<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAccountStore } from '@/stores/account'
import Icon from '@/components/Icon.vue'
import AccountLogoutDialog from '@/components/AccountLogoutDialog.vue'
import AccountMediaRecovery from '@/components/settings/AccountMediaRecovery.vue'
import type { AccountAction } from '../../../../contracts/desktop/tokenrhythm-account'

const account = useAccountStore()
const { t, te } = useI18n()
const logoutOpen = ref(false)
const authorizing = computed(() => ['authorizing', 'verifying'].includes(account.state?.status || ''))
const failure = computed(() => account.error || account.state?.errorCode)
const errorText = computed(() => failure.value && te(`account.errors.${failure.value}`) ? t(`account.errors.${failure.value}`) : t('account.errors.generic'))
const wallet = computed(() => account.state?.wallet)
const walletUpdatedAt = computed(() => {
  const date = new Date(wallet.value?.asOf || '')
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
})
// Preserve decimal precision; never round a small positive balance to zero.
function money(value: string | undefined) {
  if (value === undefined) return '—'
  const [whole, fraction = ''] = value.split('.')
  return `${wallet.value?.currency === 'CNY' ? '¥' : (wallet.value?.currency || '') + ' '}${whole}.${fraction.replace(/0+$/, '').padEnd(2, '0')}`
}
async function act(value: AccountAction) { await account.action(value).catch(() => {}) }
onMounted(() => { void account.init() })
</script>

<template>
  <section class="control-section account-panel" data-testid="account-panel">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('account.title') }}</h3>
      <p class="control-section__desc">{{ t('account.description') }}</p>
    </div>
    <div class="account-card account-identity">
      <span class="account-avatar" aria-hidden="true">{{ account.loggedIn ? account.initial : 'T' }}</span>
      <div class="account-identity__copy">
        <strong>{{ account.loggedIn ? account.name : t('account.loginTitle') }}</strong>
        <p v-if="account.state?.profile?.maskedPhone">{{ account.state.profile.maskedPhone }}</p>
        <p role="status">{{ t(`account.status.${account.state?.status || 'signed_out'}`) }}</p>
      </div>
      <div v-if="account.loggedIn" class="account-identity__actions">
        <button class="btn btn--ghost" @click="account.openLink('account')">{{ t('account.moreInfo') }} <Icon name="externalLink" :size="14" /></button>
        <button class="btn" :disabled="account.busy" @click="logoutOpen = true">{{ t('account.logout') }}</button>
      </div>
      <button v-else-if="!authorizing" class="btn btn--primary" :disabled="account.busy" @click="act('login')">{{ t('account.login') }}</button>
      <div v-else class="account-actions">
        <button class="btn" :disabled="account.busy" @click="act('reopen')">{{ t('account.reopen') }}</button>
        <button class="btn btn--ghost" :disabled="account.busy" @click="act('cancel')">{{ t('common.cancel') }}</button>
      </div>
    </div>
    <p v-if="failure" class="account-error" role="alert">{{ errorText }}</p>
    <p v-if="account.state?.pendingRevocation" class="control-section__desc" role="status">{{ t('account.pendingLogout') }}</p>
    <AccountMediaRecovery />
    <template v-if="account.loggedIn">
      <div class="account-card">
        <div class="account-balance account-balance--total"><span>{{ t('account.available') }}</span><strong>{{ money(wallet?.balances.available) }}</strong></div>
        <div class="account-balance"><span>{{ t('account.rechargeBalance') }}</span><strong>{{ money(wallet?.balances.recharge) }}</strong></div>
        <div class="account-balance"><span>{{ t('account.bonus') }}</span><strong>{{ money(wallet?.balances.bonus) }}</strong></div>
        <details v-if="wallet" class="account-details">
          <summary>{{ t('account.details') }}</summary>
          <div class="account-balance"><span>{{ t('account.debt') }}</span><span>{{ money(wallet.balances.debt) }}</span></div>
          <div class="account-balance"><span>{{ t('account.frozen') }}</span><span>{{ money(wallet.balances.frozen) }}</span></div>
        </details>
        <p v-if="account.state?.walletStatus === 'error' || account.state?.walletStatus === 'stale'" class="account-error" role="status">{{ t('account.walletUnavailable') }}</p>
        <p v-if="wallet" class="control-section__desc">{{ t('account.asOf') }} {{ walletUpdatedAt }}</p>
        <div class="account-actions">
          <button class="btn btn--ghost" :disabled="account.busy" @click="act('refresh')">{{ t('account.refresh') }}</button>
          <span class="account-spacer" />
          <button class="btn" @click="account.openLink('usage')">{{ t('account.usage') }}</button>
          <button class="btn btn--primary" @click="account.openLink('recharge')">{{ t('account.recharge') }}</button>
        </div>
      </div>
      <div v-if="account.state?.keyStatus !== 'ready'" class="account-card account-actions" role="status">
        <span>{{ t('account.modelUnavailable') }}</span>
        <button class="btn" :disabled="account.busy" @click="act('retry-key')">{{ t('account.retry') }}</button>
      </div>
    </template>
    <AccountLogoutDialog :open="logoutOpen" @close="logoutOpen = false" />
  </section>
</template>

<style scoped>
.account-panel { display: flex; flex-direction: column; gap: 20px; }
.account-card { border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 24px; }
.account-identity { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.account-avatar { display: grid; place-items: center; flex-shrink: 0; width: 48px; height: 48px; border-radius: 50%; background: var(--bg-hover); color: var(--text); font-weight: 600; }
.account-identity__copy { flex: 1; min-width: 160px; overflow-wrap: anywhere; }
.account-identity__copy p { color: var(--text-dim); font-size: var(--fs-sm); margin: 6px 0 0; }
.account-identity__actions { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; margin-left: auto; }
.account-balance { display: flex; align-items: baseline; justify-content: space-between; gap: 20px; padding: 18px 0; border-bottom: 1px solid var(--border); }
.account-balance strong { overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.account-balance--total strong { font-size: var(--fs-xl); }
.account-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.account-spacer { flex: 1; }
.account-details { margin: 16px 0; color: var(--text-dim); }
.account-details summary { cursor: pointer; }
.account-error { color: var(--danger); font-size: var(--fs-sm); }
@media (max-width: 600px) {
  .account-identity__actions { flex-basis: 100%; align-items: stretch; margin-left: 0; }
}
</style>
