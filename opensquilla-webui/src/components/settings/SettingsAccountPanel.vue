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
const failure = computed(() => {
  const errorCode = account.error || account.state?.errorCode
  return errorCode === 'ACCOUNT_LOGIN_CANCELLED' ? null : errorCode
})
const recoveryRequired = computed(() => account.state?.status === 'storage_unavailable'
  && ['ACCOUNT_LOCAL_KEY_MISSING', 'ACCOUNT_LOCAL_KEY_INVALID', 'ACCOUNT_LOCAL_DATA_INVALID'].includes(account.state?.errorCode || ''))
const errorText = computed(() => failure.value && te(`account.errors.${failure.value}`) ? t(`account.errors.${failure.value}`) : t('account.errors.generic'))
const wallet = computed(() => account.state?.wallet)
const walletUpdatedAt = computed(() => {
  const date = new Date(wallet.value?.asOf || '')
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
})
// Match TokenRhythm's two-decimal ceiling display without losing source precision.
function money(value: string | undefined) {
  if (value === undefined) return '—'
  const negative = value.startsWith('-')
  const [whole, fraction = ''] = (negative ? value.slice(1) : value).split('.')
  let cents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0').slice(0, 2))
  if (!negative && /[1-9]/.test(fraction.slice(2))) cents += 1n
  const amount = `${cents / 100n}.${String(cents % 100n).padStart(2, '0')}`
  const currency = wallet.value?.currency === 'CNY' ? '¥' : (wallet.value?.currency || '') + ' '
  return `${negative && cents > 0n ? '-' : ''}${currency}${amount}`
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
      <span class="account-avatar" aria-hidden="true">{{ account.loggedIn ? account.initial.toUpperCase() : 'T' }}</span>
      <div class="account-identity__copy">
        <div class="account-identity__heading">
          <strong>{{ account.loggedIn ? account.name : t('account.loginTitle') }}</strong>
          <span class="account-identity__status" role="status">{{ recoveryRequired ? t('account.recoveryRequired') : t(`account.status.${account.state?.status || 'signed_out'}`) }}</span>
        </div>
        <p v-if="account.state?.profile?.maskedPhone">{{ account.state.profile.maskedPhone }}</p>
      </div>
      <div v-if="account.loggedIn" class="account-identity__actions">
        <button class="account-more-info" @click="account.openLink('account')">{{ t('account.moreInfo') }}<Icon name="arrowUpRight" :size="14" /></button>
        <button class="btn account-secondary-action" :disabled="account.busy" @click="logoutOpen = true">{{ t('account.logout') }}</button>
      </div>
      <button v-else-if="!authorizing" class="btn btn--primary" :disabled="account.busy" @click="act('login')">{{ t(recoveryRequired ? 'account.recoveryLogin' : 'account.login') }}</button>
      <div v-else class="account-actions">
        <button class="btn" :disabled="account.busy" @click="act('reopen')">{{ t('account.reopen') }}</button>
        <button class="btn btn--ghost" :disabled="account.busy" @click="act('cancel')">{{ t('common.cancel') }}</button>
      </div>
    </div>
    <p v-if="failure" class="account-error" role="alert">{{ errorText }}</p>
    <p v-if="account.state?.pendingRevocation" class="control-section__desc" role="status">{{ t('account.pendingLogout') }}</p>
    <AccountMediaRecovery />
    <template v-if="account.loggedIn">
      <div class="account-card account-wallet">
        <div class="account-balance account-balance--total"><span>{{ t('account.available') }}</span><strong>{{ money(wallet?.balances.available) }}</strong></div>
        <div class="account-balance account-balance--recharge"><span>{{ t('account.rechargeBalance') }}</span><strong>{{ money(wallet?.balances.recharge) }}</strong></div>
        <div class="account-balance"><span>{{ t('account.bonus') }}</span><strong>{{ money(wallet?.balances.bonus) }}</strong></div>
        <p v-if="account.state?.walletStatus === 'error' || account.state?.walletStatus === 'stale'" class="account-error" role="status">{{ t('account.walletUnavailable') }}</p>
        <div class="account-balance__footer">
          <div class="account-balance__updated">
            <p v-if="wallet" class="control-section__desc">{{ t('account.asOf') }} {{ walletUpdatedAt }}</p>
            <button type="button" class="account-refresh" :disabled="account.busy" :aria-label="t('account.refresh')" :title="t('account.refresh')" @click="act('refresh')">
              <Icon name="refresh" :size="14" aria-hidden="true" />
            </button>
          </div>
          <div class="account-actions account-actions--wallet">
            <button class="btn account-secondary-action" @click="account.openLink('usage')">{{ t('account.usage') }}</button>
            <button class="btn btn--primary" @click="account.openLink('recharge')">{{ t('account.recharge') }}</button>
          </div>
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
.account-panel .control-section__head { flex-direction: column; align-items: flex-start; gap: 2px; margin-bottom: 0; }
.account-panel .control-section__desc { flex-basis: auto; max-width: none; }
.account-card { border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 16px 18px; }
.account-wallet { padding-top: 10px; }
.account-identity + .account-wallet { margin-top: -4px; }
.account-identity { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.account-avatar { display: grid; place-items: center; flex-shrink: 0; width: 48px; height: 48px; border-radius: 50%; background: var(--bg-hover); color: var(--text); font-size: 20px; font-weight: 600; }
.account-identity__copy { flex: 1; min-width: 160px; overflow-wrap: anywhere; }
.account-identity__heading { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 10px; }
.account-identity__copy strong { font-size: var(--fs-md); line-height: 1.4; }
.account-identity__status { display: inline-flex; align-items: center; padding: 1px 6px; border-radius: var(--radius-pill); background: var(--bg-hover); color: var(--text-dim); font-size: 11px; font-weight: 400; line-height: 1.4; }
.account-identity__copy p { color: var(--text-dim); font-size: var(--fs-sm); margin: 6px 0 0; }
.account-identity__actions { display: flex; align-items: center; gap: 16px; margin-left: auto; }
.account-more-info { display: inline-flex; align-items: center; gap: 3px; min-height: 36px; padding: 0; border: 0; background: transparent; box-shadow: none; color: var(--text-muted); cursor: pointer; font-family: var(--font-sans); font-size: var(--fs-sm); font-weight: 500; line-height: 1.4; white-space: nowrap; }
.account-more-info:hover { color: var(--text); }
.account-secondary-action:not(:disabled):hover { background: var(--bg-hover); }
.account-balance { display: flex; align-items: baseline; justify-content: space-between; gap: 20px; padding: 4px 0; font-size: var(--fs-sm); }
.account-balance--recharge { margin-bottom: 6px; }
.account-balance strong { overflow-wrap: anywhere; font-variant-numeric: tabular-nums; font-weight: 500; }
.account-balance--total { align-items: center; padding: 8px 0; font-size: var(--fs-md); }
.account-balance--total span { font-weight: 500; }
.account-balance--total strong { font-size: var(--fs-md); font-weight: 600; }
.account-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.account-balance__footer { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px 20px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); }
.account-balance__updated { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; min-width: 0; }
.account-balance__updated .control-section__desc { color: var(--text-dim); font-size: var(--fs-sm); }
.account-refresh { position: relative; display: inline-grid; place-items: center; width: 16px; height: 16px; padding: 0; border: 0; background: transparent; color: var(--text-muted); cursor: pointer; }
.account-refresh::before { content: ''; position: absolute; inset: -4px; }
.account-refresh:hover { color: var(--text); }
.account-refresh:disabled { cursor: not-allowed; opacity: var(--state-disabled-opacity); }
.account-more-info:focus-visible, .account-refresh:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }
.account-actions--wallet { margin-left: auto; }
.account-error { color: var(--danger); font-size: var(--fs-sm); }
@media (max-width: 600px) {
  .account-identity__actions { flex-basis: 100%; flex-wrap: wrap; margin-left: 0; }
  .account-identity__actions .btn { min-height: 44px; }
  .account-more-info { min-height: 44px; }
  .account-refresh { height: 44px; min-width: 44px; width: 44px; }
}
</style>
