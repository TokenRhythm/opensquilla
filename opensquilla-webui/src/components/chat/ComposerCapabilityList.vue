<template>
  <button v-for="item in items" :key="item.key" :id="item.id" type="button"
    class="composer-capability" :class="[rowClass, item.className, {
      'is-active': item.active,
      'is-selected': item.selected,
      'chat-slash-item--active': rowClass === 'chat-slash-item' && item.active,
    }]"
    :role="role" :disabled="item.disabled" :title="item.title" :aria-label="item.ariaLabel"
    :data-testid="item.testId"
    :aria-selected="role === 'option' ? Boolean(item.active) : undefined"
    :aria-pressed="role === 'menuitem' ? item.pressed ?? (item.selected ? true : undefined) : undefined"
    :data-skill-name="item.skillName" :data-capability-key="item.key"
    @mousedown="preserveEditorFocus && $event.preventDefault()"
    @click="emit('choose', item)"
  >
    <span class="composer-capability__icon" aria-hidden="true"><Icon :name="item.icon" :size="17" /></span>
    <span class="composer-capability__copy">
      <span class="composer-capability__name" :class="{ 'is-command': item.command }">
        <span>{{ item.label }}</span><span v-if="item.badge" class="composer-capability__badge">{{ item.badge }}</span>
      </span>
      <span v-if="item.description" class="composer-capability__description">{{ item.description }}</span>
    </span>
    <span v-if="item.status" class="composer-capability__status">{{ item.status }}</span>
    <Icon v-else-if="item.selected" name="check" :size="16" class="composer-capability__check" aria-hidden="true" />
  </button>
</template>

<script lang="ts">
import type { IconName } from '@/utils/icons'

// Use the stable skill name, never localized labels or arbitrary HTML icons.
export function composerCapabilityIcon(name: string): IconName {
  const key = name.toLocaleLowerCase().replace(/^\//, '').split(':').pop()!.replace(/_/g, '-')
  const known: Record<string, IconName> = {
    visualize: 'chart-no-axes-column',
    'browser-use': 'languages', browser: 'languages',
    xlsx: 'table', spreadsheets: 'table',
    docx: 'fileText', documents: 'fileText', pdf: 'fileText', 'pdf-toolkit': 'fileText',
    pptx: 'monitor', presentations: 'monitor',
    'html-coder': 'fileCode', latex: 'fileCode',
    github: 'fork', 'deep-research': 'search',
    'skill-creator': 'skills', 'sub-agent': 'agents',
    imagegen: 'image', 'kb-ingest': 'fileText', 'kb-query': 'search',
  }
  return known[key] || 'skills'
}

export interface ComposerCapabilityItem {
  key: string
  id?: string
  label: string
  description?: string
  icon: IconName
  title?: string
  ariaLabel?: string
  testId?: string
  className?: string
  status?: string
  badge?: string
  skillName?: string
  disabled?: boolean
  selected?: boolean
  active?: boolean
  pressed?: boolean
  command?: boolean
}
</script>

<script setup lang="ts">
import Icon from '@/components/Icon.vue'

withDefaults(defineProps<{
  items: ComposerCapabilityItem[]
  role?: 'menuitem' | 'option'
  rowClass?: string
  preserveEditorFocus?: boolean
}>(), { role: 'menuitem', rowClass: '', preserveEditorFocus: false })
const emit = defineEmits<{ choose: [item: ComposerCapabilityItem] }>()
</script>

<style scoped>
.composer-capability {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 38px;
  padding: 8px 10px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
  line-height: 1.45;
  text-align: start;
  cursor: pointer;
}
.composer-capability:hover:not(:disabled), .composer-capability.is-active {
  background: var(--bg-hover);
}
.composer-capability:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
  background: var(--bg-hover);
}
.composer-capability:disabled { opacity: var(--state-disabled-opacity); cursor: default; }
.composer-capability__icon { display: inline-flex; flex: 0 0 auto; color: var(--text-muted); }
.composer-capability__copy { display: flex; align-items: baseline; gap: 10px; flex: 1; min-width: 0; }
.composer-capability__name { display: inline-flex; align-items: baseline; gap: 4px; flex: 0 1 auto; min-width: 0; font-weight: 500; }
.composer-capability__name > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.composer-capability__name.is-command { font-family: var(--font-mono); }
.composer-capability__description { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font-size: var(--fs-xs); }
.composer-capability__status { flex: 0 1 auto; max-width: 10rem; color: var(--text-muted); font-size: var(--fs-xs); }
.composer-capability__check { color: var(--text-muted); }
.composer-capability__badge { flex: 0 0 auto; color: var(--text-muted); font-size: var(--fs-xs); font-weight: 400; }
@container composer-capabilities (max-width: 440px) {
  .composer-capability { min-height: 44px; gap: 9px; }
  .composer-capability__copy { display: grid; gap: 1px; }
  .composer-capability__description { white-space: normal; overflow-wrap: anywhere; }
  .composer-capability__status { max-width: 5.5rem; font-size: var(--fs-xs); }
}
@media (pointer: coarse) { .composer-capability { min-height: 44px; } }
</style>
