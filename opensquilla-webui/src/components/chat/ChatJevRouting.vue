<script setup lang="ts">
import { computed, inject } from 'vue'
import { JEV_ROUTING_KEY, type JevSessionNavigation } from '@/modules/jevRouting'
import { useChatJevRouting } from '@/composables/chat/useChatJevRouting'
import JevRoutingPanel from './JevRoutingPanel.vue'

const props = defineProps<{
  sessionKey: string
  available: boolean
  connectionEpoch?: unknown
  turnBusy?: boolean
  draftText?: string
  composing?: boolean
  draftConsumed?: number
}>()
const emit = defineEmits<{ 'open-session': [navigation: JevSessionNavigation] }>()
const routing = inject(JEV_ROUTING_KEY, null)
const { snapshot, busy, error, respond } = useChatJevRouting({
  routing,
  onOpenSession: navigation => emit('open-session', navigation),
  sessionKey: computed(() => props.sessionKey),
  available: computed(() => props.available),
  connectionEpoch: computed(() => props.connectionEpoch),
  draftText: computed(() => props.draftText || ''),
  composing: computed(() => props.composing === true),
  draftConsumed: computed(() => props.draftConsumed ?? 0),
})
</script>

<template>
  <JevRoutingPanel v-if="snapshot" :snapshot="snapshot" :busy="busy" :error="error" :turn-busy="turnBusy" @respond="respond" />
</template>
