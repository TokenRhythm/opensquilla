<template><slot /></template>

<script setup lang="ts">
import { computed, provide } from 'vue'
import { CONVERSATION_POLICY_KEY, CONVERSATION_WORKBENCH_OWNER_KEY, type ConversationPolicy } from '@/modules/conversationPolicy'

// A renderless boundary preserves each host's scroll/virtualization layout.
// All descendants share the same capability policy; this component never
// creates sessions, captures context, subscribes, or owns their lifetime.
const props = defineProps<{ policy: Readonly<ConversationPolicy>; workbenchOwnerSessionKey?: string }>()
provide(CONVERSATION_POLICY_KEY, computed(() => props.policy))
provide(CONVERSATION_WORKBENCH_OWNER_KEY, computed(() => props.workbenchOwnerSessionKey || ''))
</script>
