<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import Icon from '@/components/Icon.vue'
import AccountLogoutDialog from '@/components/AccountLogoutDialog.vue'
import { useTodayUsage } from '@/composables/usage/useTodayUsage'
import { useAccountStore } from '@/stores/account'
import { formatAccountMoney } from '@/utils/accountMoney'
import accountAvatarUrl from '@/assets/account/default-avatar.png'

const props = defineProps<{ collapsed?: boolean }>()
const emit = defineEmits<{ settings: [] }>()
const { t, te, locale } = useI18n()
const router = useRouter()
const account = useAccountStore()
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const logoutOpen = ref(false)
const menuId = useId()
const summaryId = useId()
const usageOpen = computed(() => open.value && account.loggedIn && !props.collapsed)
const { tokens, loading, error: usageError, approximate } = useTodayUsage(usageOpen)
const wallet = computed(() => account.state?.wallet)
const walletLoading = computed(() => account.refreshing || account.state?.walletStatus === 'loading')
const walletUnavailable = computed(() => ['error', 'stale'].includes(account.state?.walletStatus || ''))
const balance = computed(() => formatAccountMoney(wallet.value?.balances.available, wallet.value?.currency))
const tokenCount = computed(() => tokens.value === null ? '—' : tokens.value.toLocaleString(locale.value, { notation: 'compact', maximumFractionDigits: 2 }))
const failure = computed(() => account.error
  ? te(`account.errors.${account.error}`) ? t(`account.errors.${account.error}`) : t('account.errors.generic')
  : '')

function close(focus = false) {
  open.value = false
  if (focus) trigger.value?.focus()
}
function items() {
  return [...(root.value?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') || [])]
}
async function openMenu(last = false) {
  open.value = true
  await nextTick()
  const enabled = items()
  enabled[last ? enabled.length - 1 : 0]?.focus()
}
function toggle() { if (open.value) close(true); else void openMenu() }
function outside(event: PointerEvent) { if (!root.value?.contains(event.target as Node)) close() }
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); close(true) }
  if (event.key === 'Tab') { close(true); return }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const enabled = items()
  const index = enabled.indexOf(document.activeElement as HTMLButtonElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? enabled.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + enabled.length) % enabled.length
  enabled[next]?.focus()
}
function openSettings() { close(true); emit('settings') }
function openUsage() { close(true); void router.push('/usage?range=today') }
async function login() {
  close(true)
  void router.push('/settings/account')
  await account.action('login').catch(() => {})
}
function requestLogout() { close(true); logoutOpen.value = true }
watch(() => props.collapsed, collapsed => { if (collapsed) close() })
watch(() => account.loggedIn, () => close())
onMounted(() => { void account.init(); document.addEventListener('pointerdown', outside) })
onUnmounted(() => document.removeEventListener('pointerdown', outside))
</script>

<template>
  <div v-if="!collapsed" ref="root" class="sidebar-account">
    <button
      ref="trigger" class="sidebar-fn-item" data-testid="sidebar-account-trigger" type="button"
      :aria-label="account.loggedIn ? account.name : t('account.loginSettings')"
      :title="account.loggedIn ? account.name : t('account.loginSettings')"
      aria-haspopup="menu" :aria-expanded="open" :aria-controls="open ? menuId : undefined"
      @click="toggle" @keydown.down.prevent="openMenu()" @keydown.up.prevent="openMenu(true)"
    >
      <span class="sidebar-account__avatar" aria-hidden="true"><img v-if="account.loggedIn" :src="accountAvatarUrl" alt="" width="28" height="28" draggable="false" /><Icon v-else name="user" :size="17" /></span>
      <span class="sidebar-account__name">{{ account.loggedIn ? account.name : t('account.loginSettings') }}</span>
      <Icon name="chevronDown" :size="14" aria-hidden="true" />
    </button>
    <div
      v-if="open" :id="menuId" class="sidebar-account__menu" role="menu"
      :aria-label="t('account.title')" :aria-describedby="account.loggedIn ? summaryId : undefined"
      @keydown="keydown"
    >
      <template v-if="account.loggedIn">
        <div class="sidebar-account__identity" role="presentation">
          <span class="sidebar-account__avatar" aria-hidden="true"><img :src="accountAvatarUrl" alt="" width="36" height="36" draggable="false" /></span>
          <strong class="sidebar-account__name" :title="account.name">{{ account.name }}</strong>
        </div>
        <div :id="summaryId" role="presentation" class="sidebar-account__summary">
          <span class="sidebar-account__sr-only">{{ t('account.menu.todayUsageHint') }}</span>
          <div class="sidebar-account__wallet">
            <div class="sidebar-account__wallet-row">
              <div class="sidebar-account__wallet-balance">
                <span class="sidebar-account__caption">{{ t('account.menu.balance') }}</span>
                <strong data-testid="sidebar-account-balance" :aria-busy="walletLoading">{{ balance }}</strong>
              </div>
              <button type="button" role="menuitem" tabindex="-1" class="sidebar-account__recharge" data-testid="sidebar-account-recharge" @click="account.openLink('recharge')">
                {{ t('account.menu.recharge') }}<Icon name="arrowUpRight" :size="14" aria-hidden="true" />
              </button>
            </div>
            <span v-if="walletLoading" class="sidebar-account__hint" role="status">{{ t('shared.loading') }}</span>
            <span v-else-if="walletUnavailable" class="sidebar-account__hint sidebar-account__hint--warning" role="status">{{ t(wallet ? 'account.menu.balanceStale' : 'account.menu.balanceUnavailable') }}</span>
          </div>
          <div class="sidebar-account__usage" data-testid="sidebar-account-today-usage" :aria-busy="loading">
            <span class="sidebar-account__caption sidebar-account__usage-label" :title="t('account.menu.todayUsageHint')">
              {{ t('account.menu.todayUsage') }}<Icon name="info" :size="13" aria-hidden="true" />
            </span>
            <span class="sidebar-account__tokens" :title="tokens === null ? undefined : tokens.toLocaleString(locale)">{{ approximate && tokens !== null ? '≈ ' : '' }}{{ tokenCount }}<small v-if="tokens !== null"> Tokens</small></span>
            <span v-if="loading" class="sidebar-account__hint" role="status">{{ t('shared.loading') }}</span>
            <span v-else-if="usageError" class="sidebar-account__hint" role="status">{{ t('account.menu.usageUnavailable') }}</span>
            <span v-else-if="approximate" class="sidebar-account__hint">{{ t('account.menu.usageApproximate') }}</span>
          </div>
        </div>
        <div class="sidebar-account__divider" role="separator" />
        <button type="button" role="menuitem" tabindex="-1" class="sidebar-account__item" data-testid="sidebar-account-usage" @click="openUsage"><Icon name="chart-no-axes-column" :size="17" aria-hidden="true" />{{ t('account.menu.viewUsage') }}</button>
      </template>
      <button v-else type="button" role="menuitem" tabindex="-1" class="sidebar-account__item" :disabled="account.busy" @click="login"><Icon name="user" :size="17" aria-hidden="true" />{{ t('account.login') }}</button>
      <button type="button" role="menuitem" tabindex="-1" class="sidebar-account__item" data-testid="sidebar-account-settings" @click="openSettings"><Icon name="settings-2" :size="17" aria-hidden="true" />{{ t('chrome.settings') }}</button>
      <button v-if="account.loggedIn" type="button" role="menuitem" tabindex="-1" class="sidebar-account__item" :disabled="account.busy" @click="requestLogout"><Icon name="log-out" :size="17" aria-hidden="true" />{{ t('account.logout') }}</button>
      <p v-if="failure" class="sidebar-account__error" role="alert">{{ failure }}</p>
    </div>
  </div>
  <AccountLogoutDialog :open="logoutOpen" @close="logoutOpen = false" />
</template>

<style scoped>
.sidebar-account { position: relative; }
.sidebar-account__sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0; }
.sidebar-fn-item { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 12px; border: 0; background: transparent; color: var(--text); cursor: pointer; font: inherit; border-radius: var(--radius-md); }
.sidebar-fn-item:hover, .sidebar-fn-item[aria-expanded="true"] { background: var(--bg-hover); }
.sidebar-account__avatar { flex: 0 0 28px; height: 28px; display: grid; place-items: center; border-radius: var(--radius-full); background: var(--bg-hover); font-size: var(--fs-sm); font-weight: 600; }
.sidebar-account__avatar img { display: block; width: 100%; height: 100%; object-fit: contain; border-radius: inherit; }
.sidebar-account__name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.sidebar-account__menu { position: absolute; bottom: calc(100% + 8px); left: 0; box-sizing: border-box; width: 100%; max-height: calc(100dvh - 80px); overflow-y: auto; padding: 6px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); z-index: 50; }
.sidebar-account__identity { display: flex; align-items: center; gap: 10px; padding: 8px 10px 12px; font-size: var(--fs-md); }
.sidebar-account__identity .sidebar-account__avatar { flex-basis: 36px; height: 36px; }
.sidebar-account__identity strong { font-weight: 600; }
.sidebar-account__wallet { margin: 0 4px; padding: 10px; background: var(--bg-hover); border-radius: var(--radius-md); }
.sidebar-account__caption { color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.5; }
.sidebar-account__wallet-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.sidebar-account__wallet-balance { display: flex; flex-direction: column; gap: 4px; min-width: 0; overflow-wrap: anywhere; }
.sidebar-account__wallet-balance strong { font-size: var(--fs-md); font-weight: 600; font-variant-numeric: tabular-nums; }
.sidebar-account__menu button { display: flex; align-items: center; border: 0; background: transparent; color: var(--text); border-radius: var(--radius-sm); cursor: pointer; font: inherit; text-align: left; }
.sidebar-account__menu button:disabled { opacity: .5; cursor: default; }
.sidebar-account__menu button:not(:disabled):hover { background: var(--bg-hover); }
.sidebar-account__menu button:focus-visible, .sidebar-fn-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.sidebar-account__menu .sidebar-account__recharge { flex-shrink: 0; gap: 2px; max-width: 50%; min-height: 32px; padding: 4px 8px; border: 1px solid var(--border); font-size: var(--fs-xs); }
.sidebar-account__recharge :deep(svg) { flex-shrink: 0; }
.sidebar-account__usage { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px; padding: 12px 14px; }
.sidebar-account__usage-label { display: inline-flex; align-items: center; gap: 4px; }
.sidebar-account__tokens { font-size: var(--fs-sm); font-weight: 600; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.sidebar-account__tokens small { font-size: var(--fs-xs); font-weight: 400; color: var(--text-muted); }
.sidebar-account__hint { display: block; flex-basis: 100%; margin-top: 2px; font-size: var(--fs-xs); line-height: 1.5; color: var(--text-muted); }
.sidebar-account__hint--warning { color: var(--warn); }
.sidebar-account__divider { height: 1px; margin: 0 4px 4px; background: var(--border); }
.sidebar-account__menu .sidebar-account__item { gap: 10px; width: 100%; min-height: 38px; padding: 8px 10px; font-size: var(--fs-sm); }
.sidebar-account__item :deep(svg) { flex-shrink: 0; color: var(--text-muted); }
.sidebar-account__error { padding: 4px 10px; margin: 0; color: var(--danger); font-size: var(--fs-xs); overflow-wrap: anywhere; }
@media (pointer: coarse) {
  .sidebar-account__menu .sidebar-account__item, .sidebar-account__menu .sidebar-account__recharge { min-height: 44px; }
}
</style>
