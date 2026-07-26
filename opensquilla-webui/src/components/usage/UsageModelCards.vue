<template>
  <section class="usage-models">
    <div class="usage-section-head">
      <h3 class="usage-section-title">{{ t('usageLogs.models.title') }}</h3>
      <span class="usage-section-meta">{{ modelsMeta }}</span>
    </div>
    <div class="usage-model-grid control-card-grid" style="--control-card-min: 260px">
      <template v-if="modelCards.length === 0">
        <div class="usage-models__empty">{{ t('usageLogs.models.empty') }}</div>
      </template>
      <article
        v-for="(m, i) in modelCards"
        :key="m.model"
        class="usage-model-card control-card control-fade-item"
        :style="`--i:${i}`"
      >
        <header class="usage-model-card__head">
          <div class="usage-model-card__id">
            <span v-if="m.provider" class="usage-model-card__provider">{{ m.provider }}</span>
            <span class="usage-model-card__name" :title="m.model">{{ m.name }}</span>
          </div>
          <span class="usage-model-card__share" :title="t('usageLogs.models.shareOfTotalCost')">{{ m.share.toFixed(1) }}%</span>
        </header>
        <div class="usage-model-card__share-bar">
          <span class="usage-model-card__share-fill" :style="`width:${m.share.toFixed(1)}%`" />
        </div>
        <dl class="usage-model-card__rows">
          <div><dt>{{ t('usageLogs.metrics.tokens') }}</dt><dd class="usage-mono">{{ m.totalTokens.toLocaleString() }}</dd></div>
          <div><dt>{{ t('usageLogs.metrics.input') }}</dt><dd class="usage-mono usage-dim">{{ m.inputTokens.toLocaleString() }}</dd></div>
          <div><dt>{{ t('usageLogs.metrics.output') }}</dt><dd class="usage-mono usage-dim">{{ m.outputTokens.toLocaleString() }}</dd></div>
          <div v-if="m.cacheReadTokens > 0"><dt>{{ t('usageLogs.metrics.cacheRead') }}</dt><dd class="usage-mono usage-dim">{{ m.cacheReadTokens.toLocaleString() }}</dd></div>
          <div v-if="m.cacheWriteTokens > 0"><dt>{{ t('usageLogs.metrics.cacheWrite') }}</dt><dd class="usage-mono usage-dim">{{ m.cacheWriteTokens.toLocaleString() }}</dd></div>
          <div><dt>{{ t('usageLogs.metrics.sessions') }}</dt><dd>{{ m.sessions }}</dd></div>
          <div class="usage-model-card__cost-row">
            <dt>{{ t('usageLogs.metrics.cost') }}</dt>
            <dd class="usage-mono usage-cost">
              {{ fmtCost(m.costUsd, { source: m }) }}
              <span
                class="usage-source"
                :class="costSourceClassesForModelCard(m)"
                :title="costSourceTooltipForModelCard(m)"
              >{{ costSourceLabelForModelCard(m) }}</span>
            </dd>
          </div>
        </dl>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { ModelCard } from '@/types/usage'

const { t } = useI18n()

defineProps<{
  modelCards: ModelCard[]
  modelsMeta: string
  fmtCost: (
    cost: number | null | undefined,
    opts?: { decimals?: number; source?: object },
  ) => string
  costSourceClassesForModelCard: (m: ModelCard) => Record<string, boolean>
  costSourceLabelForModelCard: (m: ModelCard) => string
  costSourceTooltipForModelCard: (m: ModelCard) => string
}>()
</script>
