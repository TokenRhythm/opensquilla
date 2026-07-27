<template>
  <div class="empty-state">
    <p class="empty-state__greeting">{{ greeting }}</p>
    <div v-if="!suppressed" class="empty-state__chips" role="group" :aria-label="t('chat.suggestedTasks')">
      <button
        v-for="chip in chips"
        :key="chip"
        type="button"
        class="empty-state__chip"
        @click="emit('pick', chip)"
      >{{ chip }}</button>
    </div>
    <div
      v-if="!suppressed && metaSkills.length"
      class="empty-state__meta"
      role="group"
      :aria-label="t('cronSkills.skillsView.metaSkillsTitle')"
    >
      <button
        v-for="skill in metaSkills"
        :key="skill.value"
        type="button"
        class="empty-state__meta-chip"
        :title="skill.description"
        @click="emit('pick', `/meta ${skill.value}`)"
      >
        <span class="empty-state__meta-icon" aria-hidden="true">
          <Icon :name="metaSkillIcon(skill.value)" :size="16" />
        </span>
        <span class="empty-state__meta-label">{{ metaSkillLabel(skill.value) }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useRpcCall } from '@/composables/useRpc'
import type { IconName } from '@/utils/icons'

const { t } = useI18n()

/** Capability flags from the same onboarding.status snapshot SetupView reads. */
interface CapabilityStatus {
  searchConfigured?: boolean
  imageGenerationConfigured?: boolean
  imageGenerationEnabled?: boolean
}

withDefaults(defineProps<{
  agentId: string
  metaSkills?: Array<{ value: string; description: string }>
  suppressed?: boolean
}>(), {
  metaSkills: () => [],
  suppressed: false,
})

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

const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour >= 5 && hour < 12) return t('chat.greetingMorning')
  if (hour >= 12 && hour < 18) return t('chat.greetingAfternoon')
  return t('chat.greetingEvening')
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

function metaSkillLabel(name: string): string {
  const labels: Record<string, string> = {
    AwesomeWebpageMetaSkill: t('chat.metaQuick.webpage'),
    'meta-short-drama': t('chat.metaQuick.shortDrama'),
    'meta-paper-write': t('chat.metaQuick.paperWriting'),
  }
  return labels[name] || name
}

function metaSkillIcon(name: string): IconName {
  if (name === 'AwesomeWebpageMetaSkill') return 'fileCode'
  if (name === 'meta-short-drama') return 'play'
  return 'fileText'
}
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
  animation: empty-state-greeting-reveal calc(var(--dur-base) * 2.5) var(--ease-out) both;
  margin: var(--sp-2) 0 0;
  font-family: var(--font-display);
  font-size: clamp(1.75rem, 1rem + 1.8vw, 2.25rem);
  font-weight: 600;
  letter-spacing: var(--track-display);
  color: var(--text);
}

@keyframes empty-state-greeting-reveal {
  from {
    filter: blur(5px);
    opacity: 0;
    transform: translateY(7px);
  }
  to {
    filter: blur(0);
    opacity: 1;
    transform: translateY(0);
  }
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

.empty-state__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  justify-content: center;
  margin-top: var(--sp-1);
  max-width: 100%;
}

.empty-state__meta-chip {
  align-items: center;
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  cursor: pointer;
  display: inline-flex;
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  gap: 6px;
  min-height: 32px;
  padding: 5px 9px;
  white-space: nowrap;
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}

.empty-state__meta-chip:hover {
  background: color-mix(in srgb, var(--text) 5%, transparent);
  color: var(--text);
}

.empty-state__meta-chip:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.empty-state__meta-icon {
  align-items: center;
  color: var(--text-muted);
  display: inline-flex;
  flex: 0 0 16px;
  height: 16px;
  justify-content: center;
  width: 16px;
}

.empty-state__meta-label {
  line-height: 20px;
}

@media (max-width: 768px) {
  .empty-state__chip {
    min-height: 2.75rem;
  }

  .empty-state__chips {
    min-height: 2.75rem;
  }

  .empty-state__meta-chip {
    min-height: 40px;
  }

  .empty-state__meta {
    max-width: calc(100vw - 32px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .empty-state__greeting {
    animation: none;
  }

  .empty-state__chip {
    transition: none;
  }

  .empty-state__meta-chip {
    transition: none;
  }
}
</style>
