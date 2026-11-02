<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { useAccountStore } from '@/stores/account'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()
const { t, te } = useI18n()
const account = useAccountStore()
const dialog = ref<HTMLElement | null>(null)
const cancelButton = ref<HTMLButtonElement | null>(null)
const submitting = ref(false)
const error = ref('')
const isOpen = computed(() => props.open)

function close() {
  if (!submitting.value) emit('close')
}

async function logout() {
  if (submitting.value || account.busy) return
  submitting.value = true
  error.value = ''
  try {
    await account.action('logout')
    emit('close')
  } catch {
    error.value = account.error && te(`account.errors.${account.error}`)
      ? t(`account.errors.${account.error}`) : t('account.logoutFailed')
  } finally {
    submitting.value = false
  }
}

watch(isOpen, open => { if (open) error.value = '' })
useDialogA11y(dialog, isOpen, close, { initialFocus: cancelButton })
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="logout-overlay" @click.self="close">
      <section ref="dialog" class="logout-dialog" role="dialog" aria-modal="true"
        aria-labelledby="account-logout-title" aria-describedby="account-logout-description" :aria-busy="submitting">
        <header class="logout-dialog__header">
          <h2 id="account-logout-title">{{ t('account.logout') }}</h2>
          <button class="logout-dialog__close" type="button" :aria-label="t('common.close')" :disabled="submitting" @click="close">
            <Icon name="x" :size="20" />
          </button>
        </header>
        <p id="account-logout-description" class="logout-dialog__description">{{ t('account.logoutConfirmBody') }}</p>
        <p v-if="error" class="logout-dialog__error" role="alert">{{ error }}</p>
        <footer class="logout-dialog__footer">
          <button ref="cancelButton" class="logout-dialog__button" type="button" :disabled="submitting" @click="close">{{ t('common.cancel') }}</button>
          <button class="logout-dialog__button logout-dialog__confirm" type="button" :disabled="submitting || account.busy" @click="logout">
            {{ submitting ? t('account.loggingOut') : t('account.logout') }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.logout-overlay { position: fixed; inset: 0; z-index: 1100; display: grid; place-items: center; padding: 16px; background: var(--scrim); }
.logout-dialog { width: min(520px, 100%); max-height: calc(100dvh - 32px); overflow-y: auto; padding: 28px; border: 1px solid var(--border); border-radius: var(--radius-modal); background: var(--bg-surface); color: var(--text); box-shadow: var(--shadow-lg); }
.logout-dialog__header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.logout-dialog__header h2 { margin: 0; font-size: var(--fs-xl); font-weight: 600; line-height: 1.4; }
.logout-dialog__close { display: grid; place-items: center; flex: 0 0 32px; width: 32px; height: 32px; padding: 0; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text); cursor: pointer; }
.logout-dialog__close:not(:disabled):hover { background: var(--bg-hover); }
.logout-dialog__description { margin: 20px 0 28px; font-size: var(--fs-md); line-height: 1.7; overflow-wrap: anywhere; }
.logout-dialog__footer { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 10px; }
.logout-dialog__button { min-width: 88px; min-height: 44px; padding: 10px 20px; border: 1px solid var(--border); border-radius: var(--radius-lg); background: var(--bg-surface); color: var(--text); font: inherit; font-size: var(--fs-md); font-weight: 500; cursor: pointer; }
.logout-dialog__button:not(:disabled):hover { background: var(--bg-hover); }
.logout-dialog__confirm { border-color: var(--text); background: var(--text); color: var(--bg-surface); }
.logout-dialog__confirm:not(:disabled):hover { background: color-mix(in srgb, var(--text) 85%, var(--bg-surface)); }
.logout-dialog button:disabled { opacity: var(--state-disabled-opacity); cursor: wait; }
.logout-dialog button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.logout-dialog__error { margin: 0 0 20px; color: var(--danger); font-size: var(--fs-sm); line-height: 1.5; }
@media (max-width: 480px) {
  .logout-dialog { padding: 20px; }
  .logout-dialog__close { flex-basis: 44px; width: 44px; height: 44px; }
}
</style>
