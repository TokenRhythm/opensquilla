<template>
  <section class="visualize-context-chip" :aria-label="t('visualize.contextAttached')" data-testid="visualize-context-chip">
    <div class="visualize-context-chip__row">
      <div class="visualize-context-chip__copy">
        <span class="visualize-context-chip__label">{{ t('visualize.contextAttached') }} · {{ snapshot.name }}</span>
        <span class="visualize-context-chip__summary">{{ snapshot.summary }}</span>
      </div>
      <button type="button" :aria-expanded="expanded" :aria-controls="detailsId" @click="expanded = !expanded">{{ t('visualize.contextDetails') }}</button>
      <button type="button" @click="emit('remove')">{{ t('visualize.contextRemove') }}</button>
    </div>
    <div v-if="expanded" :id="detailsId" class="visualize-context-chip__details">
      <p>{{ t('visualize.contextSnapshotHint') }}</p>
      <pre v-if="snapshot.data !== undefined">{{ JSON.stringify(snapshot.data, null, 2) }}</pre>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { VisualizeDraftSnapshot } from '@/visualize/useVisualizeDraft'
const props = defineProps<{ snapshot: VisualizeDraftSnapshot }>()
const emit = defineEmits<{ remove: [] }>()
const { t } = useI18n()
const expanded = ref(false)
const detailsId = `visualize-context-${useId()}`
watch(() => props.snapshot.id, () => { expanded.value = false })
</script>

<style scoped>
.visualize-context-chip { margin: 8px 10px 0; border: 1px solid var(--border); border-radius: var(--radius-md); padding: 8px 10px; color: var(--text); background: var(--bg-surface); min-width: 0; }
.visualize-context-chip__row { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; }
.visualize-context-chip__copy { flex: 1 1 200px; min-width: 0; display: flex; flex-direction: column; gap: 3px; overflow-wrap: anywhere; }
.visualize-context-chip__label { font-size: 11px; color: var(--text-secondary); }
.visualize-context-chip__summary { font-size: 12px; line-height: 1.5; }
.visualize-context-chip button { min-height: 44px; padding: 6px 8px; border: 0; border-radius: var(--radius-sm); color: var(--text-secondary); background: transparent; font-size: 12px; cursor: pointer; }
.visualize-context-chip button:hover, .visualize-context-chip button:focus-visible { background: var(--bg-hover); color: var(--text); }
.visualize-context-chip__details { border-top: 1px solid var(--border); margin-top: 6px; padding-top: 8px; font-size: 12px; }
.visualize-context-chip__details p { margin: 0 0 6px; color: var(--text-secondary); }
.visualize-context-chip__details pre { margin: 0; max-height: 180px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
