<template>
  <section class="artifact-collection" :aria-label="label">
    <p v-if="artifacts.length === 0" class="artifact-collection__empty">
      {{ emptyLabel }}
    </p>
    <ul v-else class="artifact-collection__list">
      <li
        v-for="artifact in artifacts"
        :key="artifactKey(artifact)"
        class="artifact-collection__item"
      >
        <button
          type="button"
          class="artifact-collection__open"
          :aria-label="openLabel(artifact)"
          @click="openArtifact(artifact)"
        >
          <span class="artifact-collection__icon" aria-hidden="true">
            <Icon :name="artifactIconName(artifact)" :size="18" />
          </span>
          <span class="artifact-collection__copy">
            <strong>{{ artifactFileTitle(artifact) }}</strong>
            <small>{{ artifactFileSubtitle(artifact) }}</small>
          </span>
          <Icon
            class="artifact-collection__chevron"
            name="chevronRight"
            :size="15"
            aria-hidden="true"
          />
        </button>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import Icon from '@/components/Icon.vue'
import type { ArtifactPayload } from '@/types/rpc'
import {
  artifactFileSubtitle,
  artifactFileTitle,
  artifactIconName,
} from '@/utils/chat/artifacts'
import type { WorkbenchComponentEvent } from '@/workbench/types'

const props = defineProps<{
  artifacts: readonly ArtifactPayload[]
  emptyLabel: string
  label: string
  openArtifactLabel: (artifact: ArtifactPayload) => string
}>()

const emit = defineEmits<{
  workbenchEvent: [event: WorkbenchComponentEvent]
}>()

function artifactKey(artifact: ArtifactPayload): string {
  return String(
    artifact.id
      || artifact.key
      || artifact.download_url
      || `${artifact.name || 'artifact'}:${artifact.mime || ''}:${artifact.size || ''}`,
  )
}

function openLabel(artifact: ArtifactPayload): string {
  return props.openArtifactLabel(artifact)
}

function openArtifact(artifact: ArtifactPayload) {
  emit('workbenchEvent', {
    type: 'artifact-open',
    payload: artifact,
  })
}
</script>

<style scoped>
.artifact-collection {
  min-height: 100%;
  overflow: auto;
  padding: 8px 14px 40px;
}

.artifact-collection__empty {
  color: var(--text-dim);
  font-size: var(--fs-sm);
  padding: 28px 12px;
  text-align: center;
}

.artifact-collection__list {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.artifact-collection__item {
  min-width: 0;
  border-block-end: 1px solid var(--border);
}

.artifact-collection__open {
  display: flex;
  width: 100%;
  min-width: 0;
  align-items: center;
  gap: 12px;
  padding: 13px 8px;
  border: 0;
  background: transparent;
  color: var(--text);
  cursor: pointer;
  font: inherit;
  text-align: start;
}

.artifact-collection__open:hover {
  background: var(--bg-hover);
}

.artifact-collection__open:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.artifact-collection__icon {
  display: inline-flex;
  width: 34px;
  height: 34px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
  color: var(--text-dim);
}

.artifact-collection__copy {
  display: grid;
  min-width: 0;
  flex: 1;
  gap: 2px;
}

.artifact-collection__copy strong,
.artifact-collection__copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-collection__copy strong {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.artifact-collection__copy small,
.artifact-collection__chevron {
  color: var(--text-dim);
}

.artifact-collection__copy small {
  font-size: var(--fs-xs);
}

.artifact-collection__chevron {
  flex: 0 0 auto;
}

@media (max-width: 600px) {
  .artifact-collection {
    padding-inline: 10px;
  }
}
</style>
