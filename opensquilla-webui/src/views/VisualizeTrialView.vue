<template>
  <main class="visualize-trial">
    <h1>{{ t('visualize.trial') }}</h1>
    <p>{{ runtimeLabel }}</p>
    <p>{{ t('visualize.trialIntro') }}</p>
    <VisualizeCanvas v-for="sample in visualizeSamples" :key="sample.id" :receipt="receipt(sample)" session-key="visualize-trial" :sample-html="sample.html" />
    <label for="visualize-draft">{{ t('visualize.draft') }}</label>
    <textarea id="visualize-draft" v-model="draft" rows="4" />
    <button type="button" @click="draft = ''">{{ t('visualize.reset') }}</button>
  </main>
</template>
<script setup lang="ts">
import { provide, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import VisualizeCanvas from '@/components/chat/VisualizeCanvas.vue'
import { VISUALIZE_CONVERSATION_KEY, type VisualizeReceipt } from '@/visualize/types'
import { visualizeSamples } from '@/visualize/samples'
import { VISUALIZE_RUNTIME_LABEL as runtimeLabel } from '@/visualize/runtime'
const { t } = useI18n()
const draft = ref('')
provide(VISUALIZE_CONVERSATION_KEY, { enabled: key => key === 'visualize-trial', attach: () => false, draft(key, _receipt, text) {
  if (key !== 'visualize-trial') return false
  draft.value = draft.value ? draft.value + '\n' + text : text
  return true
} })
function receipt(sample: { id: string; title: string }): VisualizeReceipt {
  return { callId: sample.id, documentId: `doc_${sample.id}`, revisionId: 'rev_trial_v1', artifactId: sample.id, name: sample.title, mime: 'text/html', size: 1, sha256: '0'.repeat(64), marker: `visualize:doc_${sample.id}:rev_trial_v1` }
}
</script>
<style scoped>
.visualize-trial { max-width: 960px; width: 100%; margin: 0 auto; padding: var(--sp-5); color: var(--text); overflow: auto; }
h1 { font-size: var(--fs-xl); }
p { color: var(--text-muted); line-height: 1.6; }
label { display: block; margin: var(--sp-3) 0; }
textarea { box-sizing: border-box; width: 100%; padding: var(--sp-3); color: var(--text); background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-control); }
button { padding: var(--sp-2) var(--sp-3); margin-top: var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-control); background: var(--bg-hover); color: var(--text); }
</style>
