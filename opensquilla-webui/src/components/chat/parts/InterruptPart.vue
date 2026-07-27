<template>
  <ApprovalCard
    v-if="part.interruptKind === 'approval' && part.approval"
    :approval="toApprovalItem(part.approval)"
    :resolution="approvalResolution"
    :busy="part.busy"
    :error="part.error"
    :timeline="timeline"
    @allow-once="emit('resolve', part.approval.approvalId, 'allow-once')"
    @allow-always="emit('resolve', part.approval.approvalId, 'allow-always')"
    @deny="note => emit('resolve', part.approval!.approvalId, 'deny', note)"
    @extend="emit('extend', part.approval.approvalId)"
  />
  <ClarifyCard
    v-else-if="part.interruptKind === 'clarify' && part.clarify"
    :request="part.clarify"
    :submitted="part.resolution === 'replied'"
    :busy="part.busy"
    :error="part.error"
    @submit="fields => emit('clarify-submit', fields, part.clarify!)"
    @dismiss="emit('clarify-dismiss')"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ApprovalCard from '@/components/chat/ApprovalCard.vue'
import ClarifyCard from '@/components/chat/ClarifyCard.vue'
import type { ChatApprovalItem, ChatApprovalResolution } from '@/composables/chat/useChatApprovals'
import type { ChatPart, InterruptApprovalData } from '@/types/parts'

const props = defineProps<{
  part: Extract<ChatPart, { type: 'interrupt' }>
  timeline?: boolean
}>()

const emit = defineEmits<{
  resolve: [id: string, decision: 'allow-once' | 'allow-always' | 'deny', note?: string]
  extend: [id: string]
  'clarify-submit': [fields: Record<string, string>, request: NonNullable<Extract<ChatPart, { type: 'interrupt' }>['clarify']>]
  'clarify-dismiss': []
}>()

/** InterruptApprovalData → ChatApprovalItem: rename approvalId→id; the rest is
 *  field-identical, so ApprovalCard's prop type stays unchanged. */
function toApprovalItem(data: InterruptApprovalData): ChatApprovalItem {
  return {
    id: data.approvalId,
    namespace: data.namespace,
    toolName: data.toolName,
    command: data.command,
    approvalKind: data.approvalKind,
    args: data.args,
    warning: data.warning,
    agent: data.agent,
    sessionKey: data.sessionKey,
    deadline: data.deadline,
  }
}

// 'replied' is a clarify outcome and never reaches an approval part; narrow the
// shared InterruptResolution down to what ApprovalCard accepts.
const approvalResolution = computed<ChatApprovalResolution | null>(() =>
  props.part.resolution === 'replied' ? null : props.part.resolution)
</script>
