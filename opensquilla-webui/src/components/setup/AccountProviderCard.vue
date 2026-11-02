<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import Icon from '@/components/Icon.vue'
import SetupProviderMenu, { type ProviderMenuItem } from '@/components/setup/SetupProviderMenu.vue'
import { useAccountStore } from '@/stores/account'

const props = defineProps<{ active?: boolean; busy?: boolean }>()
const emit = defineEmits<{ activate: []; edit: [] }>()
const { t, te } = useI18n()
const router = useRouter()
const account = useAccountStore()
const menuOpen = ref(false)
const testing = ref(false)
const test = ref<'idle' | 'ok' | 'error'>('idle')
const busy = computed(() => props.busy || account.busy || testing.value)
const label = computed(() => t('account.modelAccount', { name: account.name }))
const unavailableReason = computed(() => {
  const code = account.state?.errorCode
  return code && te(`account.errors.${code}`) ? t(`account.errors.${code}`) : t('account.modelUnavailable')
})
const menuItems = computed<ProviderMenuItem[]>(() => {
  const items: ProviderMenuItem[] = []
  if (!props.active) items.push({ id: 'edit', label: t('common.edit'), icon: 'edit' })
  items.push(
    { id: 'test', label: t('account.test'), icon: 'check', disabled: !account.ready },
    { id: 'account', label: t('account.viewAccount'), icon: 'user' },
  )
  if (!account.ready) items.push({ id: 'retry', label: t('account.retry'), icon: 'refresh' })
  return items
})

async function menuAction(action: string) {
  if (busy.value) return
  if (action === 'edit') emit('edit')
  else if (action === 'account') await router.push('/settings/account')
  else if (action === 'retry') await account.action('retry-key').catch(() => {})
  else if (action === 'test' && account.ready) {
    testing.value = true
    test.value = 'idle'
    try { await account.action('test'); test.value = 'ok' }
    catch { test.value = 'error' }
    finally { testing.value = false }
  }
}
</script>

<template>
  <div v-if="account.loggedIn" class="account-provider" :data-testid="account.loggedIn ? 'account-provider' : 'account-media-recovery'" data-connection-id="tokenrhythm:account" :class="{ 'is-primary': props.active && account.loggedIn }">
    <div class="account-provider__row">
      <div class="account-provider__copy">
        <strong>{{ label }}</strong>
        <p>{{ account.loggedIn && props.active ? t('setup.provider.currentPrimary') : account.ready ? t('account.status.signed_in') : unavailableReason }}</p>
        <p v-if="account.loggedIn && props.active && !account.ready">{{ unavailableReason }}</p>
        <p v-if="test !== 'idle'" role="status">{{ t(test === 'ok' ? 'account.testOk' : 'account.testFailed') }}</p>
      </div>
      <div class="account-provider__actions">
        <button v-if="account.loggedIn && !props.active" type="button" class="btn account-provider__activate" :disabled="busy || !account.ready" @click="emit('activate')">{{ t('account.useAccount') }}</button>
        <button v-else type="button" class="btn btn--ghost" :disabled="busy" :aria-label="t('setup.provider.editProvider', { provider: label })" @click="emit('edit')"><Icon name="edit" :size="14" aria-hidden="true" />{{ t('common.edit') }}</button>
        <SetupProviderMenu v-if="account.loggedIn" :label="t('setup.provider.moreActions', { provider: label })" :open="menuOpen" :disabled="busy" :items="menuItems" @update:open="menuOpen = $event" @action="menuAction" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.account-provider { background: color-mix(in srgb, var(--bg-surface-2) 76%, var(--bg-surface)); border: 1px solid transparent; border-radius: var(--radius-sm); margin-bottom: var(--sp-2); min-height: 72px; padding: var(--sp-2) var(--sp-3); position: relative; }
.account-provider.is-primary { background: color-mix(in srgb, var(--accent) 5%, var(--bg-surface)); }
.account-provider.is-primary::before { background: var(--accent); border-radius: 0 var(--radius-xs) var(--radius-xs) 0; content: ''; inset-block: 10px; inset-inline-start: 0; position: absolute; width: 2px; }
.account-provider__row { display: flex; align-items: center; gap: var(--sp-3); min-height: 54px; }
.account-provider__copy { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.account-provider p { margin: var(--sp-1) 0 0; color: var(--text-muted); font-size: var(--fs-sm); }
.account-provider__actions { display: flex; align-items: center; gap: var(--sp-2); flex: 0 0 auto; }
.account-provider__actions .btn { display: inline-flex; align-items: center; gap: var(--sp-1); min-height: 32px; padding: var(--sp-1) var(--sp-2); }
.account-provider__activate { color: var(--accent); }
@media (max-width: 600px) { .account-provider__row { flex-wrap: wrap; } .account-provider__copy { flex-basis: 100%; } .account-provider__actions { justify-content: flex-end; width: 100%; } }
</style>
