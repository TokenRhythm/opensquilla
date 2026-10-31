<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDialogA11y } from '@/composables/useDialogA11y'
import type { RecommendationPreview } from '@/composables/setup/useModelRecommendations'
import type { RecommendationRow } from '@/modules/providerConfiguration'

const props = defineProps<{ preview: RecommendationPreview | null; saving: boolean }>()
const emit = defineEmits<{ cancel: []; confirm: [] }>()
const { t } = useI18n()
const modal = ref<HTMLElement | null>(null)
const title = ref<HTMLElement | null>(null)
const errorMessage = ref<HTMLElement | null>(null)
const open = computed(() => props.preview !== null)
const provider = computed(() => props.preview?.plan.providerId === 'tokenrhythm' ? 'TokenRhythm' : 'OpenRouter')
const kind = computed(() => t(`setup.modelStrategy.recommendations.${props.preview?.plan.kind || 'router'}`))
const rows = computed(() => {
  const before = props.preview?.before || []
  const after = props.preview?.plan.rows || []
  return [...new Set([...after, ...before].map(row => row.role))].map(role => ({
    role, before: before.find(row => row.role === role), after: after.find(row => row.role === role),
  }))
})
function equal(a?: RecommendationRow, b?: RecommendationRow) {
  return a?.model === b?.model && a?.provider === b?.provider && a?.thinkingLevel === b?.thinkingLevel
}
function roleLabel(role: string) {
  if (/^c[0-3]$/.test(role)) return role.toUpperCase()
  if (role === 'aggregator') return t('setup.modelStrategy.aggregatorSectionLabel')
  return t('setup.modelStrategy.recommendations.proposer', { number: role.replace('proposer_', '') })
}
function cancel() { if (!props.saving) emit('cancel') }
// A long comparison should open at its heading, not scroll straight to the
// footer on small screens. A rejected revision brings the new warning into view.
useDialogA11y(modal, open, cancel, { initialFocus: title })
watch(() => props.preview?.error, async error => {
  if (!error) return
  await nextTick()
  errorMessage.value?.focus()
})
</script>

<template>
  <Teleport to="body">
    <div v-if="preview" class="recommendation-overlay" @click.self="cancel">
      <section ref="modal" class="recommendation-dialog" role="dialog" aria-modal="true" aria-labelledby="recommendation-title" aria-describedby="recommendation-description" data-testid="recommendation-dialog" :aria-busy="saving">
        <h3 id="recommendation-title" ref="title" tabindex="-1">{{ t('setup.modelStrategy.recommendations.title', { provider, kind }) }}</h3>
        <p id="recommendation-description">{{ t('setup.modelStrategy.recommendations.description') }}</p>
        <p v-if="preview.hasDraft" class="recommendation-dialog__warning" role="status">{{ t('setup.modelStrategy.recommendations.draftWarning') }}</p>
        <p v-if="preview.error" ref="errorMessage" tabindex="-1" class="recommendation-dialog__warning" role="alert">{{ preview.error }}</p>
        <div class="recommendation-dialog__table">
          <table>
            <thead><tr><th>{{ t('setup.modelStrategy.modelRolesTitle') }}</th><th>{{ t(preview.hasDraft ? 'setup.modelStrategy.recommendations.draft' : 'setup.modelStrategy.recommendations.current') }}</th><th>{{ t('setup.modelStrategy.recommendations.next') }}</th></tr></thead>
            <tbody><tr v-for="row in rows" :key="row.role" :class="{ 'is-changed': !equal(row.before, row.after) }" :data-changed="!equal(row.before, row.after)">
              <th scope="row">{{ roleLabel(row.role) }}</th>
              <td><template v-if="row.before"><span>{{ row.before.provider }} · {{ row.before.model }}</span><small>{{ t('setup.modelStrategy.recommendations.thinking', { level: row.before.thinkingLevel || t('setup.modelStrategy.recommendations.defaultThinking') }) }}</small></template><span v-else>—</span></td>
              <td><template v-if="row.after"><span>{{ row.after.provider }} · {{ row.after.model }}</span><small>{{ t('setup.modelStrategy.recommendations.thinking', { level: row.after.thinkingLevel || t('setup.modelStrategy.recommendations.defaultThinking') }) }}</small></template><span v-else>{{ t('setup.modelStrategy.recommendations.removed') }}</span></td>
            </tr></tbody>
          </table>
        </div>
        <p>{{ t('setup.modelStrategy.recommendations.future') }}</p>
        <footer><button type="button" class="btn btn--ghost" :disabled="saving" @click="cancel">{{ t('setup.modelStrategy.recommendations.keep') }}</button><button type="button" class="btn btn--primary" data-testid="recommendation-confirm" :disabled="saving" @click="emit('confirm')">{{ t(saving ? 'setup.modelStrategy.recommendations.saving' : 'setup.modelStrategy.recommendations.confirm') }}</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.recommendation-overlay { position: fixed; inset: 0; z-index: 1100; display: flex; align-items: center; justify-content: center; background: var(--scrim); padding: var(--sp-4); }
.recommendation-dialog { width: 100%; max-width: 760px; max-height: 90dvh; overflow: auto; padding: var(--sp-5); border: 1px solid var(--border); border-radius: var(--radius-modal); background: var(--bg-surface); color: var(--text); }
.recommendation-dialog h3 { margin: 0 0 var(--sp-3); font-size: var(--fs-md); }
.recommendation-dialog p { color: var(--text-muted); font-size: var(--fs-sm); line-height: 1.6; margin: var(--sp-3) 0; }
.recommendation-dialog .recommendation-dialog__warning { color: var(--text); border-left: 3px solid var(--accent); padding: var(--sp-2) var(--sp-3); background: var(--bg-hover); }
.recommendation-dialog__table { overflow-x: auto; margin: var(--sp-4) 0; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; font-size: var(--fs-sm); }
th, td { padding: var(--sp-3) var(--sp-2); text-align: left; border-bottom: 1px solid var(--border); overflow-wrap: anywhere; vertical-align: top; }
th { font-weight: 500; } th:first-child { width: 20%; }
small { display: block; margin-top: var(--sp-1); color: var(--text-muted); font-size: var(--fs-xs); }
.is-changed { background: var(--bg-hover); }
footer { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-4); }
</style>
