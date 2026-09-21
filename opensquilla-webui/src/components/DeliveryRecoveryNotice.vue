<script setup lang="ts">
import { computed, inject, onUnmounted, ref, shallowRef } from 'vue'
import { useI18n } from 'vue-i18n'
import { DURABLE_DELIVERY_KEY, type DeliverySnapshot } from '@/modules/delivery'

const emit = defineEmits<{ openSession: [key: string] }>()
const { t } = useI18n()
const delivery = inject(DURABLE_DELIVERY_KEY, null)
const snapshots = shallowRef<readonly DeliverySnapshot[]>(delivery?.snapshots() || [])
const unsubscribe = delivery?.subscribe(() => { snapshots.value = delivery.snapshots() })
onUnmounted(() => unsubscribe?.())
const expanded = ref(false)
const limit = ref(10)
const checking = ref(false)
const failedCheck = ref<string | null>(null)
const entries = computed(() => snapshots.value.filter(item => (
  item.phase === 'unknown' || item.stopPending || item.waitReason
)))
const visible = computed(() => expanded.value ? entries.value.slice(0, limit.value) : [])

function detail(item: DeliverySnapshot): string {
  // Product copy stays here; the application owner keeps framework-free status.
  return t(`deliveryRecovery.reason.${failedCheck.value === item.id ? 'storage-check' : item.waitReason || (item.stopPending ? 'stop' : 'pending')}`)
}

async function recheck(id: string): Promise<void> {
  if (!delivery || checking.value) return
  checking.value = true
  failedCheck.value = null
  try { await delivery.retry(id) } catch { failedCheck.value = id } finally { checking.value = false }
}
</script>

<template>
  <section v-if="entries.length" class="delivery-notice" :aria-label="t('deliveryRecovery.label')" data-testid="delivery-recovery-notice">
    <div class="delivery-notice__summary">
      <p role="status">{{ t('deliveryRecovery.count', { count: entries.length }) }}</p>
      <button type="button" :aria-expanded="expanded" @click="expanded = !expanded">
        {{ t(expanded ? 'deliveryRecovery.collapse' : 'deliveryRecovery.show') }}
      </button>
    </div>
    <ul v-if="expanded" class="delivery-notice__items">
      <li v-for="item in visible" :key="item.id">
        <span>{{ detail(item) }}</span>
        <span v-if="item.stopPending && item.waitReason !== 'storage'" class="delivery-notice__stop">{{ t('deliveryRecovery.stopPending') }}</span>
        <div class="delivery-notice__actions">
          <button v-if="item.sessionKey && item.waitReason !== 'identity'" type="button" @click="emit('openSession', item.sessionKey)">{{ t('deliveryRecovery.open') }}</button>
          <button v-if="item.waitReason !== 'reload' && item.waitReason !== 'not-sent'" type="button" :disabled="checking" @click="recheck(item.id)">
            {{ t(checking ? 'deliveryRecovery.checking' : 'deliveryRecovery.check') }}
          </button>
        </div>
      </li>
      <li v-if="visible.length < entries.length">
        <button type="button" @click="limit += 10">{{ t('deliveryRecovery.more') }}</button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.delivery-notice {
  flex-shrink: 0;
  border-bottom: 1px solid var(--border);
  background: color-mix(in srgb, var(--info) 8%, var(--bg-surface));
  color: var(--text);
  font-size: var(--fs-sm);
  padding: var(--sp-2) var(--sp-4);
}
.delivery-notice__summary, .delivery-notice__actions { display: flex; align-items: center; gap: var(--sp-3); }
.delivery-notice__summary { justify-content: space-between; }
.delivery-notice p { margin: 0; }
.delivery-notice__items { max-height: 25vh; overflow: auto; margin: var(--sp-2) 0 0; padding-left: var(--sp-4); }
.delivery-notice__items li { padding: var(--sp-2) 0; }
.delivery-notice__stop { display: block; color: var(--text-muted); }
.delivery-notice button { background: transparent; border: 0; color: var(--text); cursor: pointer; font: inherit; text-decoration: underline; padding: var(--sp-1); }
.delivery-notice button:disabled { color: var(--text-muted); cursor: wait; }
</style>
