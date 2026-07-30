<script setup lang="ts">
// Platform type gallery for the compose takeover: recognition-first cards —
// a large brand mark, the platform name, and one small credential footnote
// derived from the catalog's required credential/secret fields. No transport
// badge, no description: picking a platform is a recognition task, and the
// details arrive with the form. Ordering is locale-aware (zh locales lead
// with the CN-ecosystem platforms; everything else leads with Slack/Telegram/
// Discord/Matrix) so the most likely pick sits first.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ChannelBrandMark from '@/components/setup/ChannelBrandMark.vue'
import { useChannelCatalogI18n } from '@/composables/setup/useChannelCatalogI18n'
import { orderChannelSpecs } from '@/composables/setup/channelPlatformOrder'
import type { ChannelEditorFieldSpec, ChannelEditorSpec } from '@/composables/channels/useChannelEditor'

const props = defineProps<{
  channels: ChannelEditorSpec[]
  pending: boolean
  error?: string
}>()

const emit = defineEmits<{
  pick: [type: string]
  retry: []
}>()

const { t, locale } = useI18n()
const { localizeFieldLabel, localizeLabel } = useChannelCatalogI18n()

function displayLabel(spec: ChannelEditorSpec): string {
  return localizeLabel(spec.type, spec.label)
}

const sorted = computed(() =>
  orderChannelSpecs(props.channels, String(locale.value), displayLabel))

// The one-line footnote: localized labels of the required credential/secret
// fields ("App ID · App Secret", "Bot token"), restricted to the fields
// VISIBLE under the spec's default values — a field gated behind a
// non-default mode (showWhen pointing at a value the defaults don't select)
// belongs to an alternative setup path and would otherwise advertise a
// credential mix no single connection mode requires. Platforms without
// dedicated credential fields (Matrix authenticates with homeserver + user
// id) fall back to their required fields, so no card ships blank. Derived
// from the catalog spec, never invented here.
function credentialSummary(spec: ChannelEditorSpec): string {
  const fields = spec.fields || []
  const defaults: Record<string, unknown> = {}
  for (const field of fields) defaults[field.name] = field.default
  const visibleByDefault = (field: ChannelEditorFieldSpec): boolean =>
    !field.showWhen || Object.entries(field.showWhen).every(
      ([key, value]) => String(defaults[key] ?? '') === String(value))
  const required = fields.filter(field => field.required === true && visibleByDefault(field))
  const credentials = required.filter(
    field => field.secret === true || field.group === 'credentials',
  )
  const source = credentials.length > 0
    ? credentials
    : required.filter(field => field.name !== 'name')
  return source
    .slice(0, 3)
    .map(field => localizeFieldLabel(spec.type, field.name, field.label))
    .join(' · ')
}
</script>

<template>
  <div class="ctg" :aria-label="t('console.channels.compose.galleryLabel')" role="group">
    <div v-if="pending && channels.length === 0" class="ctg__grid" aria-hidden="true">
      <span v-for="n in 8" :key="n" class="ctg__skeleton"></span>
    </div>
    <p v-else-if="error && channels.length === 0" class="ctg__error" role="alert">
      <span>{{ t('console.channels.compose.catalogFailed', { error }) }}</span>
      <button type="button" class="btn btn--ghost" @click="emit('retry')">{{ t('console.common.retry') }}</button>
    </p>
    <div v-else class="ctg__grid">
      <button
        v-for="c in sorted"
        :key="c.type"
        type="button"
        class="ctg__card"
        :data-channel-type="c.type"
        @click="emit('pick', c.type)"
      >
        <ChannelBrandMark class="ctg__mark" :type="c.type" :label="displayLabel(c)" />
        <strong class="ctg__name">{{ displayLabel(c) }}</strong>
        <span v-if="credentialSummary(c)" class="ctg__cred">{{ credentialSummary(c) }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.ctg__grid { display: grid; gap: var(--sp-2); grid-template-columns: repeat(4, 1fr); }
.ctg__card {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  cursor: pointer;
  display: grid;
  font: inherit;
  gap: var(--sp-2);
  justify-items: center;
  padding: var(--sp-4) var(--sp-2) var(--sp-3);
  text-align: center;
}
.ctg__card:hover, .ctg__card:focus-visible { background: var(--bg-elevated); border-color: var(--border-strong, var(--border)); }
.ctg__card:focus-visible { box-shadow: var(--focus-ring); outline: 0; }
.ctg__card :deep(.brand-mark) { font-size: 16px; height: 40px; width: 40px; }
.ctg__name { color: var(--text); font-size: var(--fs-sm); font-weight: 600; line-height: 1.25; }
.ctg__cred { color: var(--text-dim); font-size: 11px; line-height: 1.4; min-width: 0; overflow-wrap: anywhere; }
.ctg__skeleton { background: var(--bg-surface-2); border-radius: var(--radius-md); display: block; min-height: 108px; }
.ctg__error { align-items: center; color: var(--danger); display: flex; flex-wrap: wrap; font-size: var(--fs-sm); gap: var(--sp-2); justify-content: space-between; margin: 0; }

@media (max-width: 768px) {
  .ctg__grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 400px) {
  .ctg__grid { grid-template-columns: 1fr; }
}
</style>
