<script setup lang="ts">
// Sticky editor action bar pinned to the aside bottom. Two states, one bar:
// the dirty summary naming the changed fields with Test/Discard/Save, and the
// inline discard confirmation (a destructive-ghost button pair — deliberately
// never a modal) that confirmDiscardDraft() raises for every guarded exit.
// Dirty state stays typographic: the single accent lives on Save alone.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  changedLabels: string[]
  confirmPending: boolean
  testing: boolean
  saving: boolean
}>()

const emit = defineEmits<{
  test: []
  discard: []
  save: []
  keepEditing: []
  confirmDiscard: []
}>()

const { t } = useI18n()

const MAX_NAMED = 3
const summary = computed(() => {
  const labels = props.changedLabels
  const named = labels.slice(0, MAX_NAMED).join(', ')
  const rest = labels.length - MAX_NAMED
  const fields = rest > 0
    ? `${named} ${t('console.channels.editor.moreFields', { count: rest })}`
    : named
  return t('console.channels.editor.unsavedSummary', { fields })
})
</script>

<template>
  <div class="ceb" :class="{ 'is-confirm': confirmPending }">
    <template v-if="confirmPending">
      <span class="ceb__question">{{ t('console.channels.editor.discardQuestion') }}</span>
      <div class="ceb__actions">
        <button type="button" class="btn btn--ghost" @click="emit('keepEditing')">
          {{ t('console.channels.editor.keepEditing') }}
        </button>
        <button type="button" class="btn btn--ghost ceb__danger" @click="emit('confirmDiscard')">
          {{ t('console.channels.editor.discard') }}
        </button>
      </div>
    </template>
    <template v-else>
      <span class="ceb__summary">{{ summary }}</span>
      <div class="ceb__actions">
        <button type="button" class="btn btn--ghost" :disabled="testing || saving" @click="emit('test')">
          {{ testing ? t('setup.channels.testing') : t('setup.channels.testConnection') }}
        </button>
        <button type="button" class="btn btn--ghost" :disabled="saving" @click="emit('discard')">
          {{ t('console.channels.editor.discard') }}
        </button>
        <button type="button" class="btn btn--primary" :disabled="saving" @click="emit('save')">
          {{ saving ? t('console.channels.editor.saving') : t('setup.channels.saveChanges') }}
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ceb {
  align-items: center;
  background: var(--bg-surface);
  border-top: 1px solid var(--border);
  /* Pinned to the bottom of the aside's scroll container: the flex column
     places it there and sticky keeps it there even if an ancestor turns
     into a scrollport — tab-body content scrolls under the hairline. */
  bottom: 0;
  display: flex;
  flex: none;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-3);
  justify-content: space-between;
  padding: var(--sp-2) var(--sp-4);
  position: sticky;
}
.ceb__summary, .ceb__question { color: var(--text-muted); font-size: var(--fs-sm); min-width: 0; overflow-wrap: anywhere; }
.ceb__question { color: var(--text); font-weight: 600; }
.ceb__actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-left: auto; }
.ceb__actions .btn { min-height: 32px; padding: 5px 12px; }
.ceb__danger { color: var(--danger); }
</style>
