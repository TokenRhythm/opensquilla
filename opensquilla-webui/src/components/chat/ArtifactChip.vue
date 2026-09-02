<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import type { IconName } from '@/utils/icons'
import type { ArtifactPayload } from '@/types/artifacts'

const { t } = useI18n()

const props = defineProps<{
  artifact: ArtifactPayload
  category: string
  iconName: IconName
  title: string
  kindPill: string
  size: string
  /** Previewable types expose an explicit Open action; others are download-only. */
  previewable: boolean
  /** Visible label for the primary action ("Open" or "Download"). */
  actionLabel: string
}>()

const emit = defineEmits<{
  open: [artifact: ArtifactPayload]
  download: [artifact: ArtifactPayload]
}>()

// The card body maps to the primary action: Open when previewable, otherwise
// Download. A previewable card never downloads on body click; a download-only
// card has no misleading Open affordance.
function onBodyClick() {
  if (props.previewable) emit('open', props.artifact)
  else emit('download', props.artifact)
}

// The verb (Open / Download) belongs to the dedicated action buttons; the body
// is the artifact identity row. Naming it by the file plus its kind/size keeps
// a unique accessible name so a screen reader does not hear the same action
// twice, while still describing the target. Skipping the verb avoids colliding
// with the "Open …" / "Download …" controls beside it.
const bodyLabel = computed(() => {
  const context = [props.kindPill, props.size].filter(Boolean).join(' · ')
  return context ? `${props.title}, ${context}` : props.title
})
</script>

<template>
  <div class="msg-artifact-chip" :data-previewable="previewable ? 'true' : 'false'">
    <button
      type="button"
      class="msg-artifact-body"
      :aria-label="bodyLabel"
      @click="onBodyClick"
    >
      <span class="msg-artifact-icon" :data-kind="category" aria-hidden="true">
        <Icon :name="iconName" :size="22" />
      </span>
      <span class="msg-artifact-info">
        <span class="msg-artifact-name">{{ title }}</span>
        <span class="msg-artifact-meta">
          <span v-if="kindPill" class="msg-artifact-kind">{{ kindPill }}</span>
          <span v-if="size" class="msg-artifact-size">{{ size }}</span>
        </span>
      </span>
    </button>
    <span class="msg-artifact-actions">
      <!-- Previewable: an explicit "Open" verb plus an icon-only Download.
           Download-only: a single labelled "Download" action, no Open. -->
      <button
        v-if="previewable"
        type="button"
        class="msg-artifact-action"
        :aria-label="t('chat.openTitle', { title })"
        @click="emit('open', artifact)"
      >
        <Icon name="externalLink" :size="14" />
        <span class="msg-artifact-action__label">{{ t('chat.open') }}</span>
      </button>
      <button
        v-if="previewable"
        type="button"
        class="msg-artifact-download"
        :aria-label="t('chat.downloadTitle', { title })"
        @click="emit('download', artifact)"
      >
        <Icon name="download" :size="16" />
      </button>
      <button
        v-else
        type="button"
        class="msg-artifact-action"
        :aria-label="t('chat.downloadTitle', { title })"
        @click="emit('download', artifact)"
      >
        <Icon name="download" :size="14" />
        <span class="msg-artifact-action__label">{{ t('chat.download') }}</span>
      </button>
    </span>
  </div>
</template>

<style scoped>
.msg-artifact-chip {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--sp-2);
  width: 100%;
  padding: var(--sp-1) var(--sp-2) var(--sp-1) var(--sp-1);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  transition: border-color var(--dur-fast) var(--ease-standard), box-shadow var(--dur-fast) var(--ease-standard);
}

.msg-artifact-chip:hover {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-sm);
}

.msg-artifact-body {
  display: grid;
  grid-template-columns: 3rem minmax(0, 1fr);
  align-items: center;
  gap: var(--sp-3);
  min-width: 0;
  padding: var(--sp-1);
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.msg-artifact-body:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.msg-artifact-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 3rem;
  height: 3rem;
  border-radius: var(--radius-md);
  color: var(--info);
  background: color-mix(in srgb, var(--info) 10%, var(--bg-surface));
}

.msg-artifact-icon[data-kind="data"] {
  color: var(--warn);
  background: color-mix(in srgb, var(--warn) 10%, var(--bg-surface));
}

.msg-artifact-icon[data-kind="code"] {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-surface));
}

.msg-artifact-info {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  min-width: 0;
}

.msg-artifact-name {
  color: var(--text);
  font-size: 0.9375rem;
  font-weight: 500;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-artifact-meta {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-2);
  color: var(--text-dim);
  font-size: var(--fs-xs);
  line-height: 1.35;
  min-width: 0;
}

.msg-artifact-kind {
  flex-shrink: 0;
  padding: 0 var(--sp-1);
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-weight: 600;
  letter-spacing: 0.05em;
}

.msg-artifact-size {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-artifact-actions {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  flex-shrink: 0;
}

.msg-artifact-action {
  display: inline-flex;
  align-items: center;
  gap: var(--sp-1);
  height: var(--sp-8);
  padding: 0 var(--sp-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text);
  font-size: var(--fs-xs);
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.msg-artifact-action:hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  color: var(--accent);
}

.msg-artifact-download {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--sp-8);
  height: var(--sp-8);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--text-muted);
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard);
}

.msg-artifact-download:hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  color: var(--accent);
}

.msg-artifact-action:focus-visible,
.msg-artifact-download:focus-visible {
  outline: none;
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}

@media (max-width: 768px) {
  .msg-artifact-body {
    grid-template-columns: 2.25rem minmax(0, 1fr);
    gap: var(--sp-2);
  }

  .msg-artifact-icon {
    width: 2.25rem;
    height: 2.25rem;
  }

  .msg-artifact-action__label {
    display: none;
  }

  .msg-artifact-action {
    padding: 0;
    justify-content: center;
  }

  /* 44px touch targets while the slimmer icon column returns width to the
     filename, which otherwise truncates to a couple of characters on phones. */
  .msg-artifact-action,
  .msg-artifact-download {
    width: 2.75rem;
    height: 2.75rem;
  }
}

@media (prefers-reduced-motion: reduce) {
  .msg-artifact-chip,
  .msg-artifact-action,
  .msg-artifact-download {
    transition: none;
  }
}
</style>
