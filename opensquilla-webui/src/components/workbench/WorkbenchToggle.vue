<template>
  <button
    v-if="available"
    type="button"
    class="btn btn--icon btn--ghost"
    :title="label"
    :aria-label="label"
    :aria-expanded="expanded"
    :aria-pressed="expanded"
    :disabled="blocked"
    aria-controls="workbench-panel"
    data-testid="topbar-workbench-toggle"
    @click="toggle"
  >
    <Icon class="workbench-toggle__icon" :name="expanded ? 'sidebar-visible' : 'sidebar-hidden'" :size="16" aria-hidden="true" />
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useToasts } from '@/composables/useToasts'
import { useWorkbenchStore } from '@/workbench/store'
import type { WorkbenchItem } from '@/workbench/types'

const props = withDefaults(defineProps<{
  enabled: boolean
  allowEmpty?: boolean
  sessionId?: string
  blocked?: boolean
}>(), { sessionId: '', blocked: false, allowEmpty: false })
const { t } = useI18n()
const { pushToast } = useToasts()
const store = useWorkbenchStore()
const belongsHere = (item: WorkbenchItem) => item.scope.type !== 'session'
  || Boolean(props.sessionId && item.scope.id === props.sessionId)
const retained = computed(() => {
  const active = store.activeItem
  if (active && belongsHere(active)) return active
  return store.findMostRecentItem(belongsHere)
})
const available = computed(() => props.enabled && (props.allowEmpty || Boolean(retained.value)
  || store.closedBrowserItems.some(item => item.scope.type === 'session'
    && item.scope.id === props.sessionId)))
const expanded = computed(() => store.expanded && (store.activeItem
  ? belongsHere(store.activeItem) : props.allowEmpty))
const label = computed(() => t(expanded.value ? 'workbench.collapse' : 'workbench.expand'))

function toggle() {
  if (props.blocked) return
  if (expanded.value) {
    store.setExpanded(false)
    return
  }
  if (retained.value) {
    store.activateItem(retained.value.id)
    store.setExpanded(true)
    return
  }
  const hasClosed = store.closedBrowserItems.some(item => item.scope.type === 'session'
    && item.scope.id === props.sessionId)
  if (!hasClosed && props.allowEmpty) {
    store.openEmpty()
  } else if (!store.reopenBrowserForSession(props.sessionId)) {
    pushToast(t('workbench.itemLimitReached'), { tone: 'warn', duration: 6000 })
  }
}
</script>

<style scoped>
.workbench-toggle__icon {
  transform: scaleX(-1);
}
</style>
