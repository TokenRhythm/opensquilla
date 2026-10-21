<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import ActivityMotion from './ActivityMotion.vue'
import ActivityCodeDetails from './ActivityCodeDetails.vue'
import { useActivityDisclosureVisibility } from '@/composables/chat/useActivityDisclosureVisibility'
import { activityDisplayPath } from '@/utils/chat/activityToolDetails'
import type { LiveToolInputPreview } from '@/types/toolInputPreview'

const props = defineProps<{
  preview: LiveToolInputPreview
  open?: boolean
  stateScope?: string
  executing?: boolean
}>()
const emit = defineEmits<{ toggleItem: [id: string, open: boolean] }>()
const { t } = useI18n()
const activityVisible = useActivityDisclosureVisibility()
const stateId = computed(() => props.stateScope ? `${props.stateScope}:${props.preview.toolUseId}` : props.preview.toolUseId)
const isCode = computed(() => props.preview.toolName === 'execute_code')
const labelKey = computed(() => `chat.activity.${isCode.value ? 'codeDraft' : 'fileDraft'}`)
const codeView = computed(() => ({ kind: 'write' as const, path: isCode.value ? 'execute_code' : props.preview.path, content: props.preview.content }))
function toggle() { emit('toggleItem', stateId.value, !props.open) }
</script>

<template>
  <section class="tool-input-preview" data-testid="tool-input-preview" :data-preview-id="preview.previewId"
    data-activity-entry="true" :data-activity-type="isCode ? 'code-draft' : 'file-draft'" :data-activity-order="preview.activityOrder">
    <button type="button" class="tool-input-preview__summary" :aria-expanded="open === true" @click="toggle">
      <ActivityMotion :active="executing === true && activityVisible && preview.state === 'generating'">
        <Icon :name="isCode ? 'terminal' : 'pencil'" :size="14" aria-hidden="true" />
        <span class="tool-input-preview__label">{{ t(`${labelKey}.${preview.state === 'committed' ? 'ready' : 'generating'}`) }}</span>
        <span v-if="!isCode" class="tool-input-preview__path" :title="preview.path">{{ activityDisplayPath(preview.path) }}</span>
      </ActivityMotion>
      <Icon class="tool-input-preview__chevron" :class="{ 'tool-input-preview__chevron--open': open }" name="chevronRight" :size="12" aria-hidden="true" />
    </button>
    <template v-if="open">
      <ActivityCodeDetails :view="codeView" draft :truncated="preview.truncated" />
      <p class="tool-input-preview__note">{{ t(`${labelKey}.${preview.truncated ? 'truncated' : 'note'}`) }}</p>
    </template>
  </section>
</template>

<style scoped>
.tool-input-preview { min-width: 0; color: var(--text-muted); }
.tool-input-preview__summary { display: flex; align-items: center; width: 100%; min-width: 0; min-height: 1.75rem; padding: 0.25rem 0; gap: 0.625rem; border: 0; border-radius: 0; background: transparent; color: inherit; text-align: left; font: inherit; font-size: 0.8125rem; line-height: 1.45; cursor: pointer; }
.tool-input-preview__summary :deep(svg) { flex: 0 0 auto; }
.tool-input-preview__label { flex: 0 0 auto; }
.tool-input-preview__path { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--text-dim); }
.tool-input-preview__chevron { opacity: 0; }
.tool-input-preview__summary:hover .tool-input-preview__chevron,
.tool-input-preview__summary:focus-visible .tool-input-preview__chevron { opacity: 1; }
.tool-input-preview__chevron--open { transform: rotate(90deg); }
.tool-input-preview__summary:hover .tool-input-preview__label,
.tool-input-preview__summary:hover .tool-input-preview__path,
.tool-input-preview__summary:focus-visible .tool-input-preview__label,
.tool-input-preview__summary:focus-visible .tool-input-preview__path { color: var(--text); }
.tool-input-preview__summary:focus-visible { outline: none; box-shadow: none; }
.tool-input-preview__summary:focus-visible .tool-input-preview__label,
.tool-input-preview__summary:focus-visible .tool-input-preview__path { text-decoration: underline; text-underline-offset: 0.2em; }
.tool-input-preview__note { margin: 0.375rem 0; font-size: 0.75rem; color: var(--text-dim); }
@media (pointer: coarse) { .tool-input-preview__summary { min-height: 2.75rem; } }
</style>
