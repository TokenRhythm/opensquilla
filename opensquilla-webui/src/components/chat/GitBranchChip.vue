<template>
  <button
    type="button"
    class="git-branch-chip"
    :class="{ 'git-branch-chip--loading': loading }"
    :title="hint"
    :aria-label="hint"
    :aria-busy="loading ? 'true' : undefined"
    data-testid="git-branch-chip"
    @click="emit('open')"
  >
    <Icon name="fork" :size="14" aria-hidden="true" />
    <span class="git-branch-chip__label">{{ label }}</span>
    <span v-if="dirty" class="git-branch-chip__dirty" aria-hidden="true">*</span>
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

const props = withDefaults(defineProps<{
  /** Branch label from useGitStatus; already resolved for detached/unborn. */
  label: string
  dirtyCount: number
  loading?: boolean
}>(), { loading: false })
const emit = defineEmits<{ open: [] }>()
const { t } = useI18n()

// `main*` mirrors the source-control convention: the star means "uncommitted
// work here", so a clean repository stays visually quiet.
const dirty = computed(() => Number.isFinite(props.dirtyCount) && props.dirtyCount > 0)
const hint = computed(() => t('workbench.gitChanges.openHint', { branch: props.label }))
</script>

<style scoped>
.git-branch-chip {
  align-items: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--topbar-state-channel, var(--text-muted));
  cursor: pointer;
  display: inline-flex;
  flex: 0 1 auto;
  font: inherit;
  font-size: var(--fs-sm);
  gap: var(--sp-1);
  justify-content: center;
  max-width: 180px;
  min-height: 32px;
  min-width: 0;
  padding: 0 var(--sp-2);
  white-space: nowrap;
}
.git-branch-chip:hover { background: var(--bg-hover); color: var(--text); }
.git-branch-chip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* A read in flight dims the chip instead of reflowing the topbar. */
.git-branch-chip--loading { opacity: 0.6; }
.git-branch-chip__label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.git-branch-chip__dirty { color: var(--warn); }
@media (max-width: 768px), (pointer: coarse) {
  .git-branch-chip { min-height: 44px; min-width: 44px; }
}
</style>
