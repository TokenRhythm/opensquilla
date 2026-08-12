<template>
  <div
    class="turn-usage-details"
    data-turn-usage-details
    role="group"
    :aria-label="t('chat.usageDetails')"
  >
    <dl class="turn-usage-details__facts">
      <div
        v-if="meta.model && !meta.ensemble"
        class="turn-usage-details__row"
      >
        <dt>{{ t('chat.msgMeta.model') }}</dt>
        <dd>{{ meta.modelShort || meta.model }}</dd>
      </div>
      <div
        v-if="meta.costUsd && !meta.ensemble"
        class="turn-usage-details__row"
      >
        <dt>{{ t('chat.msgMeta.cost') }}</dt>
        <dd>{{ fmtUsd(meta.costUsd) }}</dd>
      </div>
      <div
        v-if="meta.hasTokens"
        class="turn-usage-details__row"
      >
        <dt>{{ t('chat.msgMeta.tokens') }}</dt>
        <dd>&#8593;{{ fmtTok(meta.input) }} &#8595;{{ fmtTok(meta.output) }}</dd>
      </div>
      <div
        v-if="meta.cachedTokens"
        class="turn-usage-details__row"
      >
        <dt>{{ t('chat.msgMeta.cache') }}</dt>
        <dd>{{ fmtTok(meta.cachedTokens) }}</dd>
      </div>
      <div
        v-if="meta.reasoningTokens"
        class="turn-usage-details__row"
      >
        <dt>{{ t('chat.msgMeta.think') }}</dt>
        <dd>{{ fmtTok(meta.reasoningTokens) }}</dd>
      </div>
      <template v-if="meta.ensemble">
        <div class="turn-usage-details__row turn-usage-details__row--ensemble">
          <dt>{{ t('chat.msgMeta.ensemble') }}</dt>
          <dd>{{ ensembleSummary }}</dd>
        </div>
        <div
          v-if="meta.ensemble.costUsd || meta.costUsd || !usageIncomplete"
          class="turn-usage-details__row"
        >
          <dt>{{ t('chat.msgMeta.cost') }}</dt>
          <dd>{{ fmtUsd(meta.ensemble.costUsd || meta.costUsd) }}</dd>
        </div>
        <div
          v-if="meta.ensemble.fallbackUsed"
          class="turn-usage-details__row"
        >
          <dt>{{ t('chat.msgMeta.fallback') }}</dt>
          <dd>{{ t('chat.msgMeta.fallbackUsed') }}</dd>
        </div>
      </template>
      <div
        v-if="coverageText"
        class="turn-usage-details__row turn-usage-details__row--coverage"
        data-turn-usage-coverage="incomplete"
      >
        <dt>{{ t('chat.msgMeta.coverage') }}</dt>
        <dd>{{ coverageText }}</dd>
      </div>
    </dl>

    <ul
      v-if="meta.ensemble?.models.length"
      class="turn-usage-details__models"
      :aria-label="t('chat.msgMeta.ensembleModelsAria')"
    >
      <li
        v-for="member in meta.ensemble.models"
        :key="`${member.role}:${member.provider}:${member.model}`"
        class="turn-usage-details__model"
      >
        <span class="turn-usage-details__model-role">
          {{ ensembleRole(member.role, member.label) }}
        </span>
        <span
          class="turn-usage-details__model-name"
          :title="member.model"
        >
          {{ member.modelShort || member.model }}
        </span>
        <span class="turn-usage-details__model-cost">
          {{ member.costUsd || !usageIncomplete ? fmtUsd(member.costUsd) : '—' }}
        </span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatMessageMeta } from '@/types/chat'
import {
  hasIncompleteUsageCoverage,
  usageCoverageText,
} from '@/utils/chat/usageCoverage'

const props = defineProps<{
  meta: ChatMessageMeta
  fmtTok: (value: number) => string
}>()

const { t } = useI18n()

const usageIncomplete = computed(() => hasIncompleteUsageCoverage(props.meta))
const coverageText = computed(() => usageCoverageText(
  props.meta,
  (key, named) => String(named ? t(key, named) : t(key)),
))

const ensembleSummary = computed(() => {
  const ensemble = props.meta.ensemble
  if (!ensemble) return ''
  const requests = ensemble.requestCount > 0
    ? String(t('chat.msgMeta.ensembleRequests', { count: ensemble.requestCount }))
    : ''
  const profile = ensemble.profile && ensemble.profile !== 'llm_ensemble'
    ? ensemble.profile
    : ''
  return [profile, requests].filter(Boolean).join(' · ')
    || String(t('chat.msgMeta.ensembleModels', { count: ensemble.modelCount }))
})

function fmtUsd(value: number): string {
  const amount = Number.isFinite(value) ? Math.max(0, value) : 0
  if (amount === 0) return '$0'
  if (amount < 0.0001) return '<$0.0001'
  return `$${amount.toFixed(6).replace(/\.?0+$/, '')}`
}

function ensembleRole(role: string, label: string): string {
  const normalized = String(role || '').replace(/_/g, ' ')
  if (normalized === 'proposer') return 'proposer'
  if (normalized === 'aggregator') return 'aggregator'
  if (normalized === 'fallback single') return String(t('chat.msgMeta.fallback'))
  return label || normalized || 'member'
}
</script>

<style scoped>
.turn-usage-details {
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
  min-width: 0;
  padding-bottom: 0.25rem;
  color: var(--text-muted);
  font-size: var(--fs-xs);
  line-height: 1.4;
}

.turn-usage-details__facts {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  min-width: 0;
  margin: 0;
}

.turn-usage-details__row {
  display: grid;
  grid-template-columns: minmax(5rem, 0.35fr) minmax(0, 1fr);
  align-items: baseline;
  gap: 0.75rem;
  min-width: 0;
}

.turn-usage-details__row--ensemble {
  margin-top: 0.125rem;
  padding-top: 0.375rem;
  border-top: 1px solid var(--hairline);
}

.turn-usage-details__row--coverage {
  margin-top: 0.125rem;
  padding-top: 0.375rem;
  border-top: 1px solid var(--hairline);
}

.turn-usage-details__row dt {
  min-width: 0;
  color: var(--text-dim);
}

.turn-usage-details__row dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--text);
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.turn-usage-details__row--coverage dd {
  color: var(--warn);
  font-family: inherit;
  font-variant-numeric: normal;
}

.turn-usage-details__models {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  min-width: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.turn-usage-details__model {
  display: grid;
  grid-template-columns: minmax(4.75rem, 0.8fr) minmax(7rem, 1fr) auto;
  align-items: baseline;
  gap: 0.5rem;
  min-width: 0;
}

.turn-usage-details__model-role {
  overflow: hidden;
  color: var(--text-dim);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.turn-usage-details__model-name {
  min-width: 0;
  overflow: hidden;
  color: var(--text);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.turn-usage-details__model-cost {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

@media (max-width: 480px) {
  .turn-usage-details__row,
  .turn-usage-details__model {
    grid-template-columns: minmax(0, 1fr);
    gap: 0.125rem;
  }

  .turn-usage-details__model-role,
  .turn-usage-details__model-name {
    overflow: visible;
    text-overflow: clip;
    white-space: normal;
  }
}
</style>
