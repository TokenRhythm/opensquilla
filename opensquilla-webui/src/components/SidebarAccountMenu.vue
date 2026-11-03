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
const usageHintId = useId()
const usageHintHovered = ref(false)
const usageHintFocused = ref(false)
const usageHintPinned = ref(false)
const usageHintDismissed = ref(false)
const usageHintVisible = computed(() => usageHintPinned.value
  || (!usageHintDismissed.value && (usageHintHovered.value || usageHintFocused.value)))
const usageOpen = computed(() => open.value && account.loggedIn && !props.collapsed)
const { tokens, loading, error: usageError, approximate } = useTodayUsage(usageOpen)
const wallet = computed(() => account.state?.wallet)
const walletLoading = computed(() => account.refreshing || account.state?.walletStatus === 'loading')
const walletUnavailable = computed(() => ['error', 'stale'].includes(account.state?.walletStatus || ''))
const balance = computed(() => formatAccountMoney(wallet.value?.balances.available, wallet.value?.currency))
const walletStatus = computed(() => walletLoading.value ? t('shared.loading')
  : failure.value || (walletUnavailable.value ? t(wallet.value ? 'account.menu.balanceStale' : 'account.menu.balanceUnavailable') : ''))
const usageStatus = computed(() => loading.value ? t('shared.loading')
  : usageError.value ? t('account.menu.usageUnavailable') : '')
const tokenCount = computed(() => tokens.value === null ? '—' : tokens.value.toLocaleString(locale.value, { notation: 'compact', maximumFractionDigits: 2 }))
const failure = computed(() => account.error
  ? te(`account.errors.${account.error}`) ? t(`account.errors.${account.error}`) : t('account.errors.generic')
  : '')

function close(focus = false) {
  open.value = false
  resetUsageHint()
  if (focus) trigger.value?.focus()
}
function resetUsageHint() {
  usageHintHovered.value = false
  usageHintFocused.value = false
  usageHintPinned.value = false
  usageHintDismissed.value = false
}
function dismissUsageHint() {
  usageHintPinned.value = false
  usageHintDismissed.value = true
}
function toggleUsageHint() {
  if (usageHintPinned.value) dismissUsageHint()
  else { usageHintPinned.value = true; usageHintDismissed.value = false }
}
function hoverUsageHint(event: PointerEvent) {
  if (event.pointerType === 'touch') return
  usageHintHovered.value = true
  usageHintDismissed.value = false
}
function focusUsageHint() {
  usageHintFocused.value = true
  usageHintDismissed.value = false
}
function blurUsageHint() {
  usageHintFocused.value = false
  usageHintPinned.value = false
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
function outside(event: PointerEvent) {
  const target = event.target as HTMLElement
  if (!root.value?.contains(target)) close()
  else if (!target.closest('.sidebar-account__usage-help')) dismissUsageHint()
}
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    if (usageHintVisible.value) dismissUsageHint()
    else close(true)
    return
  }
  if (event.key === 'Tab') { close(true); return }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const enabled = items()
  const index = enabled.indexOf(document.activeElement as HTMLButtonElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? enabled.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + enabled.length) % enabled.length
  enabled[next]?.focus()
}
function openSettings() { close(); emit('settings') }
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
      v-if="account.loggedIn"
      ref="trigger" class="sidebar-fn-item" data-testid="sidebar-account-trigger" type="button"
      :aria-label="account.name"
      :title="account.name"
      aria-haspopup="menu" :aria-expanded="open" :aria-controls="open ? menuId : undefined"
      @click="toggle" @keydown.down.prevent="openMenu()" @keydown.up.prevent="openMenu(true)"
    >
      <span class="sidebar-account__avatar" aria-hidden="true"><img :src="accountAvatarUrl" alt="" width="28" height="28" draggable="false" /></span>
      <span class="sidebar-account__trigger-label">
        <span class="sidebar-account__name">{{ account.name }}</span>
        <Icon name="chevronDown" :size="14" class="sidebar-account__chevron" :class="{ 'is-open': open }" data-testid="sidebar-account-chevron" aria-hidden="true" />
      </span>
    </button>
    <button
      v-else type="button" class="sidebar-account__login" data-testid="sidebar-account-login"
      :disabled="account.busy" @click="login"
    >{{ t('account.menu.loginRegister') }}</button>
    <button
      type="button" class="sidebar-account__settings" data-icon="settings" data-testid="sidebar-footer-settings"
      :aria-label="t('chrome.settings')" :title="t('chrome.settings')" @click="openSettings"
    >
      <Icon name="settings-2" :size="17" aria-hidden="true" />
    </button>
    <div
      v-if="open && account.loggedIn" :id="menuId" class="sidebar-account__menu" role="menu"
      :aria-label="t('account.title')" :aria-describedby="summaryId"
      @keydown="keydown"
    >
      <div class="sidebar-account__identity" role="presentation">
        <span class="sidebar-account__avatar" aria-hidden="true"><img :src="accountAvatarUrl" alt="" width="36" height="36" draggable="false" /></span>
        <strong class="sidebar-account__name" :title="account.name">{{ account.name }}</strong>
      </div>
      <div :id="summaryId" role="presentation" class="sidebar-account__summary">
        <div class="sidebar-account__wallet">
          <div class="sidebar-account__wallet-row">
            <div class="sidebar-account__wallet-balance">
              <div class="sidebar-account__balance-label">
                <span class="sidebar-account__caption" :class="{ 'is-hidden': walletStatus }">{{ t('account.available') }}</span>
                <span class="sidebar-account__hint" :class="{ 'sidebar-account__hint--warning': (walletUnavailable || failure) && !walletLoading }" data-testid="sidebar-account-wallet-status" :role="failure && !walletLoading ? 'alert' : 'status'" :title="walletStatus || undefined">{{ walletStatus }}</span>
              </div>
              <strong data-testid="sidebar-account-balance" :aria-busy="walletLoading" :title="balance">{{ balance }}</strong>
            </div>
            <button type="button" role="menuitem" tabindex="-1" class="sidebar-account__recharge" data-testid="sidebar-account-recharge" @click="account.openLink('recharge')">
              {{ t('account.menu.recharge') }}<span class="sidebar-account__recharge-arrow" aria-hidden="true" />
            </button>
          </div>
        </div>
        <div class="sidebar-account__usage" data-testid="sidebar-account-today-usage" :aria-busy="loading">
          <span class="sidebar-account__caption sidebar-account__usage-label">
            <span class="sidebar-account__usage-title" :title="t('account.menu.todayUsage')">{{ t('account.menu.todayUsage') }}</span>
            <span class="sidebar-account__usage-help" @pointerenter="hoverUsageHint" @pointerleave="usageHintHovered = false">
              <button type="button" role="menuitem" tabindex="-1" class="sidebar-account__info" data-testid="sidebar-account-usage-info" :aria-label="t('account.menu.todayUsage')" :aria-describedby="usageHintId" @focus="focusUsageHint" @blur="blurUsageHint" @click.stop="toggleUsageHint"><Icon name="info" :size="13" aria-hidden="true" /></button>
              <span :id="usageHintId" class="sidebar-account__tooltip" :class="{ 'is-visible': usageHintVisible }" role="tooltip" :aria-hidden="!usageHintVisible">{{ t('account.menu.todayUsageHint') }}</span>
            </span>
          </span>
          <span class="sidebar-account__usage-value">
            <span v-if="!usageStatus" class="sidebar-account__tokens" :title="tokens?.toLocaleString(locale)">{{ approximate && tokens !== null ? '≈ ' : '' }}{{ tokenCount }}<small v-if="tokens !== null"> Tokens</small></span>
            <span class="sidebar-account__usage-status" role="status" :title="usageStatus || undefined">{{ usageStatus }}</span>
          </span>
        </div>
      </div>
      <div class="sidebar-account__divider" role="separator" />
      <button type="button" role="menuitem" tabindex="-1" class="sidebar-account__item" data-testid="sidebar-account-usage" @click="openUsage"><Icon name="chart-no-axes-column" :size="14" aria-hidden="true" />{{ t('account.menu.viewUsage') }}</button>
      <button type="button" role="menuitem" tabindex="-1" class="sidebar-account__item" :disabled="account.busy" @click="requestLogout"><Icon name="log-out" :size="14" aria-hidden="true" />{{ t('account.logout') }}</button>
    </div>
  </div>
  <AccountLogoutDialog :open="logoutOpen" @close="logoutOpen = false" />
</template>

<style scoped>
.sidebar-account { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: var(--sp-1); }
.sidebar-account__settings { display: grid; place-items: center; width: 36px; height: 36px; padding: 0; border: 0; border-radius: var(--radius-md); background: transparent; color: var(--text); cursor: pointer; }
.sidebar-account__settings:hover { background: var(--bg-hover); }
.sidebar-fn-item { display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 12px; border: 0; background: transparent; color: var(--text); cursor: pointer; font: inherit; border-radius: var(--radius-md); }
.sidebar-fn-item:hover, .sidebar-fn-item[aria-expanded="true"] { background: var(--bg-hover); }
.sidebar-account__trigger-label { display: flex; align-items: center; min-width: 0; gap: var(--sp-1); }
.sidebar-account__chevron { flex-shrink: 0; transition: transform var(--dur-fast) var(--ease-standard); }
.sidebar-account__chevron.is-open { transform: rotate(180deg); }
.sidebar-account__login { display: block; box-sizing: border-box; width: 100%; min-height: 36px; margin: 6px 0; padding: 6px 12px; border: 1px solid var(--border); border-radius: var(--radius-full); background: transparent; color: var(--text); cursor: pointer; font: inherit; font-size: var(--fs-xs); }
.sidebar-account__login:disabled { opacity: .5; cursor: default; }
.sidebar-account__login:not(:disabled):hover { background: var(--bg-hover); }
.sidebar-account__login:not(:disabled):active { background: var(--bg-active, var(--bg-hover)); }
.sidebar-account__avatar { flex: 0 0 28px; height: 28px; display: grid; place-items: center; border-radius: var(--radius-full); background: var(--bg-hover); font-size: var(--fs-sm); font-weight: 600; }
.sidebar-account__avatar img { display: block; width: 100%; height: 100%; object-fit: contain; border-radius: inherit; }
.sidebar-account__name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.sidebar-account__menu { position: absolute; bottom: calc(100% + 8px); left: 0; box-sizing: border-box; width: 100%; max-height: calc(100dvh - 80px); overflow-y: auto; padding: 8px 0; background: var(--bg); border: 0; border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); z-index: 50; }
.sidebar-account__identity { display: flex; align-items: center; gap: 10px; padding: 8px 16px 12px; font-size: var(--fs-md); }
.sidebar-account__identity .sidebar-account__avatar { flex-basis: 36px; height: 36px; }
.sidebar-account__identity strong { font-weight: 600; }
.sidebar-account__wallet { margin: 0 12px; padding: 8px 12px; background: var(--account-wallet-bg, var(--bg-surface-2)); border-radius: var(--radius-md); }
.sidebar-account__caption { color: var(--text-muted); font-size: var(--fs-xs); line-height: 1.5; }
.sidebar-account__wallet-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }
.sidebar-account__wallet-balance { display: flex; flex: 1 1 6rem; flex-direction: column; gap: 4px; min-width: 0; }
/* Keep room for the translated caption while refresh status occupies its place. */
.sidebar-account__balance-label { display: grid; grid-template-columns: minmax(0, 1fr); line-height: 18px; }
.sidebar-account__balance-label > span { grid-area: 1 / 1; align-self: start; }
.sidebar-account__balance-label .is-hidden { visibility: hidden; }
.sidebar-account__wallet-balance strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-md); line-height: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.sidebar-account__menu button { display: flex; align-items: center; border: 0; background: transparent; color: var(--text); border-radius: var(--radius-sm); cursor: pointer; font: inherit; text-align: left; }
.sidebar-account__menu button:disabled { opacity: .5; cursor: default; }
.sidebar-account__menu button:not(:disabled):hover { background: var(--bg-hover); }
.sidebar-account__menu button:focus-visible, .sidebar-fn-item:focus-visible, .sidebar-account__login:focus-visible, .sidebar-account__settings:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.sidebar-account__menu .sidebar-account__recharge { flex-shrink: 0; gap: 4px; max-width: 100%; margin-inline-start: auto; height: 28px; padding: 0 8px; border: 1px solid var(--border); font-size: var(--fs-xs); line-height: 18px; white-space: nowrap; transition: background-color var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard); }
.sidebar-account__menu .sidebar-account__recharge:not(:disabled):hover { background: var(--bg-hover); border-color: var(--text-faint, var(--text-muted)); }
.sidebar-account__menu .sidebar-account__recharge:not(:disabled):active { background: color-mix(in srgb, var(--text) 12%, var(--account-wallet-bg, var(--bg-surface-2))); border-color: var(--text-muted); }
.sidebar-account__recharge-arrow { flex: 0 0 12px; height: 12px; background: currentColor; mask: url('@/assets/account/recharge-arrow.png') center / contain no-repeat; }
/* Refresh messages share the caption and value lines to keep the menu still. */
.sidebar-account__usage { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, auto); align-items: center; column-gap: 8px; margin: 0 12px; padding: 11px 10px; }
.sidebar-account__usage-label { display: inline-flex; align-items: center; min-width: 0; gap: 4px; white-space: nowrap; }
.sidebar-account__usage-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.sidebar-account__usage-help { display: inline-flex; flex-shrink: 0; }
.sidebar-account__menu .sidebar-account__info { justify-content: center; width: 20px; height: 24px; padding: 0; color: var(--text-muted); }
/* The identity and wallet above the help button leave 126px before the menu edge. */
.sidebar-account__tooltip { position: absolute; top: 10px; transform: translateY(max(-100%, -126px)); left: 0; right: 0; z-index: 1; padding: 10px 12px; border-radius: var(--radius-md); background: var(--text); color: var(--bg); box-shadow: var(--shadow-md); font-size: var(--fs-xs); font-weight: 400; line-height: 1.5; white-space: normal; overflow-wrap: anywhere; visibility: hidden; opacity: 0; pointer-events: none; }
.sidebar-account__tooltip.is-visible { visibility: visible; opacity: 1; pointer-events: auto; }
.sidebar-account__usage-value { display: flex; align-items: center; justify-content: flex-end; min-width: 0; min-height: 24px; white-space: nowrap; }
.sidebar-account__usage-status { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; font-size: var(--fs-xs); line-height: 20px; color: var(--text-muted); }
.sidebar-account__tokens { overflow: hidden; text-overflow: ellipsis; font-size: var(--fs-sm); line-height: 1.5; font-weight: 600; font-variant-numeric: tabular-nums; }
.sidebar-account__tokens small { font-size: var(--fs-xs); font-weight: 400; color: var(--text-muted); }
.sidebar-account__hint { display: block;  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-xs); line-height: 18px; color: var(--text-muted); }
.sidebar-account__hint--warning { color: var(--warn); }
.sidebar-account__divider { height: .5px; margin: 0 0 4px; background: var(--account-menu-divider); }
.sidebar-account__menu .sidebar-account__item { box-sizing: border-box; gap: 10px; width: calc(100% - 24px); min-height: 38px; margin: 0 12px; padding: 8px 10px; font-size: var(--fs-xs); }
.sidebar-account__item :deep(svg) { flex-shrink: 0; color: var(--text-muted); }
@media (pointer: coarse) {
  .sidebar-account__menu .sidebar-account__item, .sidebar-account__menu .sidebar-account__recharge { min-height: 44px; }
  .sidebar-account__login, .sidebar-account__settings, .sidebar-account__menu .sidebar-account__info { min-width: 44px; min-height: 44px; }
}
@media (prefers-reduced-motion: reduce) {
  .sidebar-account__chevron, .sidebar-account__menu .sidebar-account__recharge { transition: none; }
}
</style>
