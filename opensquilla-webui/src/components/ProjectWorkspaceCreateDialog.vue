<template>
  <Teleport to="body">
    <div v-if="open" class="modal-overlay" @click="close">
      <form
        ref="dialogRef"
        class="modal project-create"
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-workspace-create-title"
        @click.stop
        @submit.prevent="create"
      >
        <header class="project-create__header">
          <h3 id="project-workspace-create-title">{{ t('workspaces.addProject') }}</h3>
          <button
            type="button"
            class="btn btn--icon btn--ghost"
            :aria-label="t('common.close')"
            :disabled="busy"
            @click="close"
          >
            <Icon name="x" :size="15" />
          </button>
        </header>

        <div class="project-create__modes" role="group" :aria-label="t('workspaces.projectAction')">
          <button type="button" class="btn" :class="mode === 'create' ? 'btn--primary' : 'btn--ghost'"
            :disabled="busy || sourcePicking || !creationAvailable" :aria-pressed="mode === 'create'" @click="emit('update:mode', 'create')">
            {{ t('workspaces.createProject') }}
          </button>
          <button type="button" class="btn" :class="mode === 'open' ? 'btn--primary' : 'btn--ghost'"
            :disabled="busy || sourcePicking" :aria-pressed="mode === 'open'" @click="emit('update:mode', 'open')">
            {{ t('workspaces.openExistingProject') }}
          </button>
        </div>
        <p v-if="!creationAvailable" class="project-create__hint">{{ t('workspaces.createUnavailable') }}</p>

        <label v-if="mode === 'create'" class="project-create__name">
          <span class="sr-only">{{ t('workspaces.projectName') }}</span>
          <span class="project-create__input-wrap">
            <Icon name="folder" :size="16" />
            <input
              ref="nameInputRef"
              :value="name"
              type="text"
              maxlength="120"
              autocomplete="off"
              :placeholder="t('workspaces.projectNamePlaceholder')"
              :disabled="busy"
              @input="updateName"
            />
          </span>
        </label>

        <div class="project-create__source">
          <span class="project-create__source-label">{{ t(mode === 'create' ? 'workspaces.saveParentDirectory' : 'workspaces.projectLocation') }}</span>
          <button
            type="button"
            class="project-create__source-picker"
            :disabled="busy || sourcePicking"
            @click="emit('choose-source')"
          >
            <Icon name="folder" :size="21" />
            <span v-if="sourcePath" class="project-create__source-copy">
              <strong>{{ sourceName }}</strong>
              <small>{{ sourcePath }}</small>
            </span>
            <span v-else>{{ t(mode === 'create' ? 'workspaces.chooseParentDirectory' : 'workspaces.chooseExistingDirectory') }}</span>
          </button>
        </div>

        <div class="project-create__summary">
          <span>{{ t('workspaces.executionHost', { host: hostName || t('workspaces.gatewayHost') }) }}</span>
          <strong v-if="targetPath" class="project-create__target">{{ targetPath }}</strong>
          <p>{{ t(mode === 'create' ? 'workspaces.createLocationHint' : 'workspaces.openLocationHint') }}</p>
        </div>
        <p v-if="error" class="project-create__error" role="alert">{{ error }}</p>

        <footer class="project-create__footer">
          <button type="button" class="btn btn--ghost" :disabled="busy" @click="close">
            {{ t('common.cancel') }}
          </button>
          <button type="submit" class="btn btn--primary" :disabled="!canCreate">
            {{ busy ? t('workspaces.preparingProject') : t(mode === 'create' ? 'workspaces.createProject' : 'workspaces.openExistingProject') }}
          </button>
        </footer>
      </form>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { projectWorkspaceTarget, validProjectDirectoryName, type ProjectWorkspaceMode } from '@/utils/projectWorkspaceCreation'

const props = defineProps<{
  open: boolean
  mode: ProjectWorkspaceMode
  name: string
  sourcePath: string
  busy: boolean
  sourcePicking?: boolean
  creationAvailable: boolean
  hostName?: string
  hostPlatform?: 'windows' | 'posix'
  error?: string
}>()
const emit = defineEmits<{
  close: []
  'update:name': [name: string]
  'update:mode': [mode: ProjectWorkspaceMode]
  'choose-source': []
  create: [payload: { mode: ProjectWorkspaceMode; name: string; path: string }]
}>()
const { t } = useI18n()
const dialogRef = ref<HTMLElement | null>(null)
const nameInputRef = ref<HTMLInputElement | null>(null)

const sourceName = computed(() => {
  const normalized = props.sourcePath.replace(/[\\/]+$/, '')
  return normalized.split(/[\\/]/).pop() || normalized
})
const targetPath = computed(() => props.mode === 'create'
  ? projectWorkspaceTarget(props.sourcePath, props.name, props.hostPlatform)
  : props.sourcePath.trim())
const canCreate = computed(() =>
  !props.busy
  && !props.sourcePicking
  && (props.mode === 'open' || (props.creationAvailable && validProjectDirectoryName(props.name)))
  && Boolean(props.sourcePath.trim()),
)

function updateName(event: Event) {
  emit('update:name', (event.target as HTMLInputElement).value)
}

function close() {
  if (!props.busy) emit('close')
}

function create() {
  if (!canCreate.value) return
  emit('create', {
    mode: props.mode,
    name: props.name.trim(),
    path: props.sourcePath.trim(),
  })
}

useDialogA11y(dialogRef, computed(() => props.open), close, {
  initialFocus: nameInputRef,
})
</script>

<style scoped>
.sr-only {
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  position: absolute;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: grid;
  place-items: center;
  background: var(--scrim);
}

.project-create {
  width: min(94vw, 520px);
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  padding: var(--sp-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  background: var(--bg-surface);
  box-shadow: var(--shadow-lg);
}

.project-create__header,
.project-create__footer {
  display: flex;
  align-items: center;
}

.project-create__header h3 {
  flex: 1;
  margin: 0;
  font-size: var(--fs-xl);
}

.project-create__modes { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
.project-create__hint,
.project-create__summary { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); }
.project-create__summary { display: grid; gap: var(--sp-2); }
.project-create__summary p { margin: 0; }
.project-create__target { color: var(--text); overflow-wrap: anywhere; font-weight: 500; }
.project-create__error { margin: 0; color: var(--danger); font-size: var(--fs-sm); overflow-wrap: anywhere; }

.project-create__name {
  display: block;
}

.project-create__input-wrap {
  min-height: 42px;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  background: var(--bg);
  transition: border-color var(--transition), box-shadow var(--transition);
}

.project-create__input-wrap:focus-within {
  border-color: var(--accent);
  box-shadow: none;
}

.project-create__input-wrap input:not([type="radio"]):not([type="checkbox"]) {
  width: 100%;
  min-width: 0;
  min-height: 40px;
  padding: 0;
  border: 0;
  outline: 0;
  background: transparent;
  box-shadow: none;
}

.project-create__input-wrap input:not([type="radio"]):not([type="checkbox"]):focus {
  background: transparent;
  border-color: transparent;
  box-shadow: none;
}

.project-create__source {
  display: grid;
  gap: var(--sp-2);
}

.project-create__source-label {
  color: var(--text);
  font-size: var(--fs-sm);
}

.project-create__source-picker {
  min-height: 82px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-3);
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
  color: var(--text-muted);
  cursor: pointer;
  transition: border-color var(--transition), background var(--transition), color var(--transition);
}

.project-create__source-picker:not(:disabled):hover,
.project-create__source-picker:not(:disabled):focus-visible {
  border-color: var(--border-strong);
  background: var(--bg-hover);
  color: var(--text);
  outline: none;
  box-shadow: none;
}

.project-create__source-picker:disabled {
  cursor: not-allowed;
  opacity: var(--state-disabled-opacity);
}

.project-create__source-copy {
  min-width: 0;
  display: grid;
  gap: 2px;
  text-align: left;
}

.project-create__source-copy strong,
.project-create__source-copy small {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.project-create__source-copy strong {
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 500;
}

.project-create__source-copy small {
  max-width: 390px;
  color: var(--text-muted);
  font-size: var(--fs-xs);
}

.project-create__footer {
  justify-content: flex-end;
  gap: var(--sp-2);
}
</style>
