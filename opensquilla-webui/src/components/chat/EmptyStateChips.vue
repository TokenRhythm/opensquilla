<template>
  <div class="empty-state">
    <p class="empty-state__greeting">{{ greeting }}</p>
    <p class="empty-state__identity">{{ identityLine }}</p>
    <div v-if="!suppressed" class="empty-state__chips" role="group" :aria-label="t('chat.suggestedTasks')">
      <button
        v-for="chip in chips"
        :key="chip"
        type="button"
        class="empty-state__chip"
        @click="emit('pick', chip)"
      >{{ chip }}</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRpcCall } from '@/composables/useRpc'

const { t } = useI18n()

/** Capability flags from the same onboarding.status snapshot SetupView reads. */
interface CapabilityStatus {
  searchConfigured?: boolean
  imageGenerationConfigured?: boolean
  imageGenerationEnabled?: boolean
}

interface AgentIdentityPayload {
  name?: string | null
}

const props = defineProps<{
  agentId: string
  suppressed?: boolean
}>()

const emit = defineEmits<{
  pick: [text: string]
}>()

// Rendered immediately so a late capability lookup swaps labels in place
// instead of shifting the landing layout, and kept whenever the lookup fails.
const FALLBACK_CHIPS = computed(() => [
  t('chat.chips.whatCanYouDo'),
  t('chat.chips.summarizeWebpage'),
  t('chat.chips.planWeek'),
])

const capabilityStatus = useRpcCall<CapabilityStatus>('onboarding.status')
const identity = useRpcCall<AgentIdentityPayload>('agent.identity.get', { agentId: props.agentId })

const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour >= 5 && hour < 12) return t('chat.greetingMorning')
  if (hour >= 12 && hour < 18) return t('chat.greetingAfternoon')
  return t('chat.greetingEvening')
})

const identityLine = computed(() => {
  const name = identity.data.value?.name
  const label = typeof name === 'string' && name.trim() ? name.trim() : props.agentId
  return t('chat.identityReady', { label })
})

const chips = computed(() => {
  const status = capabilityStatus.data.value
  if (!status) return FALLBACK_CHIPS.value
  const derived: string[] = []
  if (status.searchConfigured) derived.push(t('chat.chips.searchAiNews'))
  if (status.imageGenerationConfigured && status.imageGenerationEnabled !== false) {
    derived.push(t('chat.chips.generateImage'))
  }
  derived.push(t('chat.chips.summarizeWebpage'), t('chat.chips.whatCanYouDo'))
  if (derived.length < 3) derived.push(t('chat.chips.planWeek'))
  return derived.slice(0, 4)
})
</script>

<style scoped>
.empty-state {
  /* The landing wrapper disables pointer events; the chips need them back. */
  pointer-events: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-2);
  position: relative;
  text-align: center;
}

/* Dawn halo behind the greeting — themes without --atmosphere-dawn simply
   render no halo. Kept behind text via z-index, never intercepts input. */
.empty-state::before {
  background: var(--atmosphere-dawn, transparent);
  border-radius: var(--radius-full);
  content: '';
  filter: blur(48px);
  inset: -34% -16%;
  opacity: 0.55;
  pointer-events: none;
  position: absolute;
  z-index: -1;
}

.empty-state__greeting {
  margin: var(--sp-2) 0 0;
  font-family: var(--font-display);
  font-size: clamp(1.75rem, 1rem + 1.8vw, 2.25rem);
  font-weight: 600;
  letter-spacing: var(--track-display);
  color: var(--text);
}

.empty-state__identity {
  margin: 0;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--text-dim);
}

.empty-state__chips {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--sp-2);
  margin-top: var(--sp-4);
  /* Reserve one chip row so late capability resolution cannot shift layout. */
  min-height: 2.25rem;
}

.empty-state__chip {
  display: inline-flex;
  align-items: center;
  min-height: 2.25rem;
  padding: 0.375rem 0.875rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--bg-elevated);
  font: inherit;
  font-size: 0.8125rem;
  color: var(--text-muted);
  cursor: pointer;
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}

.empty-state__chip:hover {
  background: var(--bg-hover);
  border-color: var(--border-strong);
  color: var(--text);
}

.empty-state__chip:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

@media (max-width: 768px) {
  .empty-state__chip {
    min-height: 2.75rem;
  }

  .empty-state__chips {
    min-height: 2.75rem;
  }
}

@media (prefers-reduced-motion: reduce) {
  .empty-state__chip {
    transition: none;
  }
}
</style>
