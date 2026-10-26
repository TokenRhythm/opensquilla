<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import Icon from '@/components/Icon.vue'
import AccountLogoutDialog from '@/components/AccountLogoutDialog.vue'
import { useAccountStore } from '@/stores/account'
defineProps<{ collapsed?: boolean }>()
const emit = defineEmits<{ settings: [] }>()
const { t } = useI18n()
const router = useRouter()
const account = useAccountStore()
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const logoutOpen = ref(false)
function close(focus = false) { open.value = false; if (focus) trigger.value?.focus() }
async function toggle() { open.value = !open.value; if (open.value) { await nextTick(); root.value?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus() } }
function outside(event: PointerEvent) { if (!root.value?.contains(event.target as Node)) close() }
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); close(true) }
  if (event.key === 'Tab') close()
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  event.preventDefault()
  const items = [...root.value!.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')]
  const index = items.indexOf(document.activeElement as HTMLButtonElement)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
  items[next]?.focus()
}
function navigate(section: string) { close(); void router.push(`/settings/${section}`) }
async function login() { navigate('account'); await account.action('login').catch(() => {}) }
function requestLogout() { close(true); logoutOpen.value = true }
onMounted(() => { void account.init(); document.addEventListener('pointerdown', outside) })
onUnmounted(() => document.removeEventListener('pointerdown', outside))
</script>

<template>
  <div ref="root" class="sidebar-account" :class="{ 'is-collapsed': collapsed }">
    <button ref="trigger" class="sidebar-fn-item" type="button" :aria-label="account.loggedIn ? account.name : t('account.loginSettings')" :title="account.loggedIn ? account.name : t('account.loginSettings')" aria-haspopup="menu" :aria-expanded="open" @click="toggle" @keydown.down.prevent="toggle">
      <span class="sidebar-account__avatar" aria-hidden="true"><template v-if="account.loggedIn">{{ account.initial }}</template><Icon v-else name="user" :size="17" /></span>
      <span class="sidebar-fn-label sidebar-account__name">{{ account.loggedIn ? account.name : t('account.loginSettings') }}</span>
      <Icon class="sidebar-fn-label" name="chevronDown" :size="14" />
    </button>
    <div v-if="open" class="sidebar-account__menu" role="menu" :aria-label="t('account.title')" @keydown="keydown">
      <button v-if="!account.loggedIn" role="menuitem" :disabled="account.busy" @click="login">{{ t('account.login') }}</button>
      <button role="menuitem" @click="close(); emit('settings')">{{ t('chrome.settings') }}</button>
      <button v-if="account.loggedIn" role="menuitem" :disabled="account.busy" @click="requestLogout">{{ t('account.logout') }}</button>
    </div>
    <AccountLogoutDialog :open="logoutOpen" @close="logoutOpen = false" />
  </div>
</template>

<style scoped>
.sidebar-account { position: relative; }
.sidebar-fn-item { display: flex; align-items: center; gap: 12px; width: 100%; padding: 10px 12px; border: 0; background: transparent; color: var(--text); cursor: pointer; font: inherit; border-radius: var(--radius-md); }
.sidebar-fn-item:hover { background: var(--bg-hover); }
.is-collapsed .sidebar-fn-label { display: none; }
.sidebar-account__avatar { flex: 0 0 26px; height: 26px; display: grid; place-items: center; border-radius: 50%; background: var(--bg-hover); font-size: var(--fs-sm); font-weight: 600; }
.sidebar-account__name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; }
.sidebar-account__menu { position: absolute; bottom: calc(100% + 8px); left: 0; min-width: 220px; padding: 6px; background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow-lg); z-index: 50; }
.sidebar-account__menu button { display: block; width: 100%; text-align: left; border: 0; background: transparent; color: var(--text); padding: 10px 12px; border-radius: var(--radius-sm); cursor: pointer; font: inherit; }
.sidebar-account__menu button:hover, .sidebar-account__menu button:focus-visible { background: var(--bg-hover); }
</style>
