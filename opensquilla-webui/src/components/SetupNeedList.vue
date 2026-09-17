<template>
  <div v-if="needs.length > 0" class="setup-need-list" :aria-label="label">
    <span>{{ label }}</span>
    <ul>
      <li v-for="(item, i) in needs" :key="i">{{ localizeNeed(item) }}</li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  items?: string[]
  label: string
}>()

const { t } = useI18n()
const needs = computed(() => (props.items || []).filter(Boolean))

// The backend onboarding specs emit "what you need" bullets as English text.
// Map the known, non-interpolated ones to localized copy; dynamic/credential
// items (which embed env-var names) fall back to their raw English text.
const NEED_KEYS: Record<string, string> = {
  'No API key required.': 'setup.needs.noApiKey',
  'Bundled local embeddings for the default path.': 'setup.needs.bundledLocal',
  'No embedding service; keyword search remains available.': 'setup.needs.noEmbedding',
  'Local ONNX embedding assets from the recommended install.': 'setup.needs.localOnnx',
  'Optional remote fallback credentials if configured.': 'setup.needs.optionalRemoteFallback',
  'A provider/model id that supports image generation.': 'setup.needs.imageProviderModel',
  'Optional embedding model override.': 'setup.needs.optionalEmbeddingModel',
  'Optional ONNX directory override for custom local assets.': 'setup.needs.optionalOnnxDir',
  'Provider/model identifier.': 'setup.needs.providerModelId',
  'Optional locale hint such as zh-CN, en-US, or en-GB.': 'setup.needs.optionalLocaleHint',
  'off surfaces the original provider error.': 'setup.needs.offSurfacesError',
  'Environment variable to read for audio provider access.': 'setup.needs.audioEnvVar',
  'Remote embedding API key or env reference.': 'setup.needs.remoteEmbeddingKey',
}

function localizeNeed(item: string): string {
  const key = NEED_KEYS[item]
  return key ? t(key) : item
}
</script>

<style scoped>
.setup-need-list {
  background: var(--bg-surface-2);
  border-radius: var(--radius-card);
  font-size: var(--fs-sm);
  padding: var(--sp-3);
}

.setup-need-list span {
  color: var(--text-dim);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
}

.setup-need-list ul {
  color: var(--text-muted);
  list-style: none;
  margin: var(--sp-1) 0 0;
  padding: 0;
}

.setup-need-list li::before {
  color: var(--accent);
  content: "\2022";
  margin-right: 6px;
}
</style>
