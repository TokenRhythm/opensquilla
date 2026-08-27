<template>
  <section class="resource-collection" :aria-label="label">
    <p v-if="resources.length === 0" class="resource-collection__empty">
      {{ emptyLabel }}
    </p>
    <template v-for="group in groups" v-else :key="group.type">
      <section v-if="group.resources.length" class="resource-collection__group">
        <h3>{{ group.label }}</h3>
        <ul>
          <li
            v-for="resource in group.resources"
            :key="resourceKey(resource)"
            class="resource-collection__item"
          >
            <button
              type="button"
              class="resource-collection__open"
              :disabled="isBusy(resource)"
              :aria-label="openLabel(resource)"
              @click="emitAction('resource-open', resource)"
            >
              <span class="resource-collection__icon" aria-hidden="true">
                <Icon :name="resourceIcon(resource)" :size="18" />
              </span>
              <span class="resource-collection__copy">
                <strong>{{ resource.name }}</strong>
                <small>{{ resourceMeta(resource) }}</small>
                <small v-if="isBusy(resource)" role="status">
                  {{ preparingLabel }}
                </small>
                <small
                  v-if="hasOpenError(resource)"
                  class="resource-collection__open-error"
                  role="alert"
                >
                  {{ openErrorMessage }}
                </small>
                <small
                  v-if="resource.capabilities.reasonCode"
                  class="resource-collection__unavailable-reason"
                >
                  {{ unavailableReason(resource) }}
                </small>
              </span>
            </button>
            <span class="resource-collection__actions">
              <button
                v-if="hasOpenError(resource)"
                type="button"
                :disabled="isBusy(resource)"
                :title="retryLabel"
                :aria-label="retryLabel"
                @click="emitAction('resource-open', resource)"
              >
                <Icon name="refresh" :size="15" />
              </button>
              <button
                v-if="resource.capabilities.download"
                type="button"
                :disabled="isBusy(resource)"
                :title="downloadLabel(resource)"
                :aria-label="downloadLabel(resource)"
                @click="emitAction('resource-download', resource)"
              >
                <Icon name="download" :size="15" />
              </button>
            </span>
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import Icon from '@/components/Icon.vue'
import type {
  WorkbenchResource,
} from '@/types/workbenchResources'
import { workbenchResourceRefId } from '@/types/workbenchResources'
import type { IconName } from '@/utils/icons'
import type { WorkbenchComponentEvent } from '@/workbench/types'

const props = defineProps<{
  busyKey?: string
  downloadLabel: (resource: WorkbenchResource) => string
  emptyLabel: string
  groupLabels: { files: string; links: string }
  label: string
  openErrorKey?: string
  openErrorMessage?: string
  openLabel: (resource: WorkbenchResource) => string
  preparingLabel: string
  retryLabel: string
  resources: readonly WorkbenchResource[]
  unavailableReason: (resource: WorkbenchResource) => string
}>()

const emit = defineEmits<{
  workbenchEvent: [event: WorkbenchComponentEvent]
}>()

const groups = computed(() => (
  (['files', 'links'] as const).map(type => ({
    type,
    label: props.groupLabels[type],
    resources: props.resources.filter(resource => (
      type === 'links'
        ? resource.resource.type === 'url'
        : resource.resource.type !== 'url'
    )),
  }))
))

function resourceKey(resource: WorkbenchResource): string {
  return `${resource.resource.type}:${workbenchResourceRefId(resource.resource)}`
}

function isBusy(resource: WorkbenchResource): boolean {
  return props.busyKey === resourceKey(resource)
}

function hasOpenError(resource: WorkbenchResource): boolean {
  return props.openErrorKey === resourceKey(resource) && Boolean(props.openErrorMessage)
}

function resourceIcon(resource: WorkbenchResource): IconName {
  if (resource.resource.type === 'attachment') return 'paperclip'
  if (resource.resource.type === 'url') return 'externalLink'
  return 'fileText'
}

function resourceMeta(resource: WorkbenchResource): string {
  const type = resource.mime.includes('/')
    ? resource.mime.split('/').pop() || resource.mime
    : resource.mime
  const size = Number(resource.size)
  const sizeLabel = Number.isFinite(size) && size > 0
    ? `${Math.max(1, Math.round(size / 1024))} KB`
    : ''
  return [type.toUpperCase(), sizeLabel].filter(Boolean).join(' · ')
}

function emitAction(type: string, resource: WorkbenchResource) {
  emit('workbenchEvent', { type, payload: resource })
}
</script>

<style scoped>
.resource-collection {
  min-height: 100%;
  overflow: auto;
  padding: 8px 14px 40px;
}

.resource-collection__empty {
  padding: 28px 12px;
  color: var(--text-dim);
  font-size: var(--fs-sm);
  text-align: center;
}

.resource-collection__group + .resource-collection__group {
  margin-top: var(--sp-4);
}

.resource-collection__group h3 {
  margin: 0;
  padding: 10px 8px 6px;
  color: var(--text-dim);
  font-size: var(--fs-xs);
  font-weight: 700;
  letter-spacing: .04em;
  text-transform: uppercase;
}

.resource-collection__group ul {
  margin: 0;
  padding: 0;
  list-style: none;
}

.resource-collection__item {
  display: flex;
  min-width: 0;
  align-items: center;
  border-block-end: 1px solid var(--border);
}

.resource-collection__open {
  display: flex;
  min-width: 0;
  flex: 1;
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

.resource-collection__open:hover:not(:disabled) {
  background: var(--bg-hover);
}

.resource-collection__open:focus-visible,
.resource-collection__actions button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.resource-collection__open:disabled {
  cursor: default;
  opacity: .65;
}

.resource-collection__icon {
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

.resource-collection__copy {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.resource-collection__copy strong,
.resource-collection__copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.resource-collection__copy strong {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.resource-collection__copy small {
  color: var(--text-dim);
  font-size: var(--fs-xs);
}

.resource-collection__copy .resource-collection__unavailable-reason {
  color: var(--warning, var(--text-dim));
  white-space: normal;
}

.resource-collection__copy .resource-collection__open-error {
  color: var(--danger);
  white-space: normal;
}

.resource-collection__actions {
  display: inline-flex;
  flex: 0 0 auto;
  gap: 2px;
  padding-inline-end: 4px;
}

.resource-collection__actions button {
  display: inline-flex;
  width: 30px;
  height: 30px;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}

.resource-collection__actions button:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text);
}

.resource-collection__actions button:disabled {
  cursor: progress;
  opacity: .5;
}
</style>
