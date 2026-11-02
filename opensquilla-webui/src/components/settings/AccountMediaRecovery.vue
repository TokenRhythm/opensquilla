<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAccountStore } from '@/stores/account'

const account = useAccountStore()
const { t } = useI18n()
const kinds = ['image', 'video'] as const
const restoring = ref(false)
const failed = ref(false)
const needsRecovery = computed(() => kinds.some(kind => {
  const source = account.state?.mediaSources[kind]
  return source && source !== 'inherit'
}))

async function restore() {
  if (restoring.value || account.busy) return
  restoring.value = true
  failed.value = false
  try {
    // Each successful operation updates the account state. If one fails,
    // retry only the sources that still need recovery.
    for (const kind of kinds) {
      const source = account.state?.mediaSources[kind]
      if (source && source !== 'inherit') await account.selectMediaSource(kind, 'inherit')
    }
  } catch {
    failed.value = true
  } finally {
    restoring.value = false
  }
}
</script>

<template>
  <div v-if="needsRecovery" class="account-media-recovery" data-testid="account-media-recovery" :aria-busy="restoring">
    <div class="account-media-recovery__row">
      <p>{{ t('account.mediaRecoveryHint') }}</p>
      <button type="button" class="btn btn--ghost" :disabled="restoring || account.busy" @click="restore">{{ t(restoring ? 'account.restoringMediaDefaults' : 'account.restoreMediaDefaults') }}</button>
    </div>
    <p v-if="failed" class="account-media-recovery__error" role="alert">{{ t('account.restoreMediaDefaultsFailed') }}</p>
  </div>
</template>

<style scoped>
.account-media-recovery { border: 1px solid var(--border); border-radius: var(--radius-lg); padding: var(--sp-3) var(--sp-4); }
.account-media-recovery__row { align-items: center; display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-4); }
.account-media-recovery__row p { color: var(--text-dim); flex: 1 1 200px; font-size: var(--fs-sm); margin: 0; overflow-wrap: anywhere; }
.account-media-recovery__row button { flex: 0 0 auto; }
.account-media-recovery__error { color: var(--danger); font-size: var(--fs-sm); margin: var(--sp-2) 0 0; }
</style>
