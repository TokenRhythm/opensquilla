<template>
  <Teleport to="body">
    <div
      v-if="open && enabled && phase !== 'closed' && phase !== 'native-picking'"
      class="modal-overlay"
      @click="closeDialog"
    >
      <section
        ref="dialogRef"
        class="modal project-picker"
        role="dialog"
        aria-modal="true"
        :aria-label="t('workspaces.chooseProject')"
        @click.stop
      >
        <header class="project-picker__header">
          <h3>{{ t('workspaces.chooseProject') }}</h3>
          <button
            class="btn btn--icon btn--ghost"
            :aria-label="t('common.close')"
            @click="closeDialog"
          >
            <Icon name="x" :size="15" />
          </button>
        </header>

        <template v-if="phase === 'desktop-error'">
          <p class="project-picker__error" role="alert">{{ error }}</p>
          <footer class="project-picker__footer">
            <button class="btn btn--ghost" @click="closeDialog">
              {{ t('common.cancel') }}
            </button>
            <button class="btn btn--primary" @click="retryNativePicker">
              {{ t('workspaces.retryDirectoryPicker') }}
            </button>
          </footer>
        </template>

        <template v-else>
          <p class="project-picker__scope">{{ t('workspaces.webPickerScope') }}</p>
          <div class="project-picker__path">
            <button
              type="button"
              class="btn btn--ghost project-picker__action project-picker__parent"
              :disabled="!parentDirectory"
              @click="browse(parentDirectory || undefined)"
            >
              <Icon name="arrowUp" :size="14" />
              {{ t('workspaces.parentDirectory') }}
            </button>
            <input
              ref="pathInputRef"
              v-model="locationDraft"
              type="text"
              :aria-label="t('workspaces.projectPath')"
              :placeholder="t('workspaces.pathPlaceholder')"
              @keydown.enter.prevent="browse(locationDraft)"
            />
            <button
              v-if="systemPickerAvailable"
              type="button"
              class="btn btn--ghost project-picker__action project-picker__browse"
              :disabled="systemPickerBusy"
              @click="openSystemPicker"
            >
              <Icon name="folder" :size="14" />
              {{ t('workspaces.browse') }}
            </button>
          </div>
          <p v-if="error" class="project-picker__error" role="alert">{{ error }}</p>
          <div class="project-picker__browser">
            <div class="project-picker__browser-toolbar">
              <div class="project-picker__create">
                <button
                  v-if="!creatingDirectory"
                  type="button"
                  class="btn btn--ghost project-picker__action project-picker__create-trigger"
                  :disabled="webLoading"
                  @click="beginCreateDirectory"
                >
                  <Icon name="plus" :size="14" />
                  {{ t('workspaces.newDirectory') }}
                </button>
                <form
                  v-else
                  class="project-picker__create-form"
                  @submit.prevent="createDirectory"
                >
                  <input
                    ref="newDirectoryInputRef"
                    v-model="newDirectoryName"
                    type="text"
                    :aria-label="t('workspaces.newDirectoryName')"
                    :placeholder="t('workspaces.newDirectoryName')"
                    autocomplete="off"
                    @keydown.esc.prevent="cancelCreateDirectory"
                  />
                  <button
                    type="submit"
                    class="btn btn--primary"
                    :disabled="creatingDirectoryBusy || !newDirectoryName.trim()"
                  >
                    {{ t('workspaces.createDirectory') }}
                  </button>
                  <button
                    type="button"
                    class="btn btn--ghost"
                    :disabled="creatingDirectoryBusy"
                    @click="cancelCreateDirectory"
                  >
                    {{ t('common.cancel') }}
                  </button>
                </form>
              </div>
            </div>
            <div
              class="project-picker__entries"
              role="listbox"
              :aria-busy="webLoading"
            >
              <button
                v-for="entry in directories"
                :key="entry.path"
                type="button"
                role="option"
                :aria-selected="selectedDirectory === entry.path"
                class="project-picker__entry"
                :class="{ 'is-selected': selectedDirectory === entry.path }"
                @click="selectDirectory(entry.path)"
                @dblclick="browse(entry.path)"
                @keydown.enter.prevent.stop="browse(entry.path)"
                @keydown.space.prevent.stop="selectDirectory(entry.path)"
              >
                <Icon name="folder" :size="15" />
                <span>{{ entry.name }}</span>
              </button>
            </div>
          </div>
          <footer class="project-picker__footer">
            <button class="btn btn--ghost" @click="closeDialog">
              {{ t('common.cancel') }}
            </button>
            <button
              class="btn btn--primary project-picker__choose"
              :disabled="!canChoose"
              @click="choose"
            >
              <Icon name="check" :size="15" />
              {{ t('workspaces.chooseSelectedDirectory') }}
            </button>
          </footer>
        </template>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { useDialogA11y } from '@/composables/useDialogA11y'
import { getPlatform } from '@/platform'
import { useRpcStore } from '@/stores/rpc'
import type { SandboxPathEntry, SandboxPathListResponse } from '@/types/rpc'

type PickerPhase =
  | 'closed'
  | 'native-picking'
  | 'desktop-error'
  | 'web-loading'
  | 'web-ready'
  | 'web-error'

const props = withDefaults(defineProps<{
  open: boolean
  sessionKey: string
  initialPath?: string
  enabled?: boolean
}>(), {
  enabled: true,
})
const emit = defineEmits<{
  close: []
  choose: [path: string]
}>()
const { t } = useI18n()
const rpc = useRpcStore()
const dialogRef = ref<HTMLElement | null>(null)
const pathInputRef = ref<HTMLInputElement | null>(null)
const newDirectoryInputRef = ref<HTMLInputElement | null>(null)
const phase = ref<PickerPhase>('closed')
const currentDirectory = ref('')
const selectedDirectory = ref('')
const locationDraft = ref('')
const parentDirectory = ref<string | null>(null)
const entries = ref<SandboxPathEntry[]>([])
const error = ref('')
const creatingDirectory = ref(false)
const creatingDirectoryBusy = ref(false)
const systemPickerBusy = ref(false)
const systemPickerAvailable = ref(false)
const newDirectoryName = ref('')
let openEpoch = 0
let browseSequence = 0

const webLoading = computed(() => phase.value === 'web-loading')
const directories = computed(() =>
  entries.value.filter(entry => entry.kind === 'directory' && entry.selectable),
)
const canChoose = computed(() => {
  if (webLoading.value) return false
  const selected = selectedDirectory.value.trim()
  if (!selected) return false
  if (selected === currentDirectory.value) return true
  return directories.value.some(entry => entry.path === selected)
})

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause)
}

function isAbsoluteLocation(path: string): boolean {
  return path.startsWith('/')
    || path.startsWith('\\')
    || /^[A-Za-z]:[\\/]/.test(path)
}

function ownsRequest(epoch: number, sequence: number): boolean {
  return props.open
    && props.enabled
    && epoch === openEpoch
    && sequence === browseSequence
}

async function browse(target?: string, epoch = openEpoch) {
  if (!props.open || !props.enabled || epoch !== openEpoch) return
  const sequence = ++browseSequence
  const normalized = target?.trim() || ''
  const params: Record<string, string> = {
    sessionKey: props.sessionKey,
    kind: 'workspace',
  }
  if (normalized) {
    params.path = normalized
    if (!isAbsoluteLocation(normalized) && currentDirectory.value) {
      params.basePath = currentDirectory.value
    }
  }

  phase.value = 'web-loading'
  error.value = ''
  try {
    const response = await rpc.call<SandboxPathListResponse>(
      'sandbox.path.list',
      params,
    )
    if (!ownsRequest(epoch, sequence)) return
    const resolved = String(response.currentPath || response.path || '').trim()
    if (!resolved) throw new Error('Gateway returned an empty directory path.')
    currentDirectory.value = resolved
    locationDraft.value = resolved
    selectedDirectory.value = resolved
    parentDirectory.value = response.parentPath ?? null
    systemPickerAvailable.value = response.systemPickerAvailable === true
    entries.value = Array.isArray(response.entries) ? response.entries : []
    phase.value = 'web-ready'
  } catch (cause) {
    if (!ownsRequest(epoch, sequence)) return
    error.value = errorMessage(cause)
    phase.value = 'web-error'
  }
}

function selectDirectory(path: string) {
  selectedDirectory.value = path
}

async function beginCreateDirectory() {
  creatingDirectory.value = true
  newDirectoryName.value = ''
  await nextTick()
  newDirectoryInputRef.value?.focus()
}

function cancelCreateDirectory() {
  if (creatingDirectoryBusy.value) return
  creatingDirectory.value = false
  newDirectoryName.value = ''
}

async function createDirectory() {
  if (!props.enabled) return
  const name = newDirectoryName.value.trim()
  if (!name || !currentDirectory.value || creatingDirectoryBusy.value) return
  const epoch = openEpoch
  creatingDirectoryBusy.value = true
  error.value = ''
  try {
    const response = await rpc.call<{ path: string }>(
      'sandbox.path.create-directory',
      {
        sessionKey: props.sessionKey,
        parentPath: currentDirectory.value,
        name,
        kind: 'workspace',
      },
    )
    if (!props.open || epoch !== openEpoch) return
    const createdPath = String(response.path || '').trim()
    if (!createdPath) throw new Error('Gateway returned an empty directory path.')
    creatingDirectory.value = false
    newDirectoryName.value = ''
    await browse(createdPath)
  } catch (cause) {
    if (!props.open || epoch !== openEpoch) return
    error.value = t('workspaces.createDirectoryFailed', {
      error: errorMessage(cause),
    })
  } finally {
    if (epoch === openEpoch) creatingDirectoryBusy.value = false
  }
}

function invalidateAndClose() {
  openEpoch += 1
  browseSequence += 1
  phase.value = 'closed'
}

function closeDialog() {
  invalidateAndClose()
  emit('close')
}

function choose() {
  if (!props.enabled || !canChoose.value) return
  const selected = selectedDirectory.value.trim()
  invalidateAndClose()
  emit('choose', selected)
}

async function openSystemPicker() {
  if (!props.open || !props.enabled || systemPickerBusy.value) return
  const epoch = openEpoch
  systemPickerBusy.value = true
  error.value = ''
  const params: Record<string, string> = {
    sessionKey: props.sessionKey,
    kind: 'workspace',
  }
  if (currentDirectory.value) params.initialPath = currentDirectory.value
  try {
    const choice = await rpc.call<{ path: string | null }>(
      'sandbox.path.pick',
      params,
    )
    if (epoch !== openEpoch || !props.open) return
    const selected = String(choice?.path || '').trim()
    if (!selected) return
    invalidateAndClose()
    emit('choose', selected)
  } catch (cause) {
    if (epoch !== openEpoch || !props.open) return
    error.value = t('workspaces.directoryPickerFailed', {
      error: errorMessage(cause),
    })
  } finally {
    if (epoch === openEpoch) systemPickerBusy.value = false
  }
}

async function runNativePicker(epoch: number) {
  if (!props.enabled) return
  const nativePicker = getPlatform().files.chooseProjectDirectory
  if (typeof nativePicker !== 'function') {
    await nextTick()
    if (epoch !== openEpoch || !props.open) return
    pathInputRef.value?.focus()
    await browse(props.initialPath?.trim() || undefined, epoch)
    return
  }

  phase.value = 'native-picking'
  error.value = ''
  try {
    const choice = await nativePicker({
      initialPath: props.initialPath?.trim() || undefined,
    })
    if (epoch !== openEpoch || !props.open) return
    if (choice?.path) {
      const selected = choice.path
      invalidateAndClose()
      emit('choose', selected)
    } else {
      closeDialog()
    }
  } catch (cause) {
    if (epoch !== openEpoch || !props.open) return
    error.value = t('workspaces.directoryPickerFailed', {
      error: errorMessage(cause),
    })
    phase.value = 'desktop-error'
  }
}

function retryNativePicker() {
  void runNativePicker(openEpoch)
}

watch(
  () => props.open,
  async (open) => {
    openEpoch += 1
    browseSequence = 0
    currentDirectory.value = ''
    selectedDirectory.value = ''
    locationDraft.value = props.initialPath?.trim() || ''
    parentDirectory.value = null
    entries.value = []
    error.value = ''
    creatingDirectory.value = false
    creatingDirectoryBusy.value = false
    systemPickerBusy.value = false
    systemPickerAvailable.value = false
    newDirectoryName.value = ''
    if (!open) {
      phase.value = 'closed'
      return
    }
    if (!props.enabled) {
      phase.value = 'closed'
      return
    }
    const epoch = openEpoch
    await runNativePicker(epoch)
  },
  { immediate: true },
)

watch(
  () => props.enabled,
  enabled => {
    if (enabled || !props.open) return
    invalidateAndClose()
    emit('close')
  },
)

useDialogA11y(
  dialogRef,
  computed(
    () => props.open
      && props.enabled
      && phase.value !== 'closed'
      && phase.value !== 'native-picking',
  ),
  closeDialog,
  { initialFocus: pathInputRef },
)
</script>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: grid;
  place-items: center;
  background: var(--scrim);
}
.project-picker {
  width: min(92vw, 620px);
  max-height: min(78vh, 620px);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  background: var(--bg-surface);
}
.project-picker__header,
.project-picker__footer {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.project-picker__header h3 { flex: 1; margin: 0; }
.project-picker__scope { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); }
.project-picker__path {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--sp-2);
}
.project-picker__path input {
  width: 100%;
  min-width: 0;
  min-height: 38px;
}
.project-picker__action.btn--ghost {
  min-height: 38px;
  padding: 7px 11px;
  border-color: color-mix(in srgb, var(--border) 88%, transparent);
  background: var(--bg-elevated);
  color: var(--text);
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--text) 5%, transparent);
}
.project-picker__action.btn--ghost:not(:disabled):hover {
  border-color: color-mix(in srgb, var(--accent) 38%, var(--border));
  background: var(--bg-hover);
}
.project-picker__action.btn--ghost:not(:disabled):active,
.project-picker__choose.btn--primary:not(:disabled):active {
  transform: scale(0.98);
}
.project-picker__action :deep(svg),
.project-picker__choose :deep(svg) {
  flex: 0 0 auto;
}
.project-picker__browser {
  min-height: 180px;
  display: flex;
  flex: 1;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  overflow: hidden;
  background: var(--bg-surface);
}
.project-picker__browser-toolbar {
  min-height: 43px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  padding: 5px var(--sp-2);
  border-bottom: 1px solid color-mix(in srgb, var(--border) 76%, transparent);
  background: color-mix(in srgb, var(--bg-elevated) 60%, var(--bg-surface));
}
.project-picker__entries {
  min-height: 0;
  flex: 1;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  padding: var(--sp-1);
}
.project-picker__create {
  width: 100%;
  min-height: 32px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
}
.project-picker__create-trigger {
  gap: var(--sp-1);
  font-weight: 600;
}
.project-picker__create-trigger.project-picker__action {
  min-height: 32px;
  padding: 4px 9px;
  background: var(--bg-surface);
  color: var(--text-muted);
  font-size: var(--fs-xs);
}
.project-picker__create-form {
  width: 100%;
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.project-picker__create-form input {
  flex: 1;
  min-width: 0;
}
.project-picker__entry {
  width: 100%;
  display: flex;
  gap: var(--sp-2);
  align-items: center;
  padding: var(--sp-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  text-align: left;
}
.project-picker__entry:hover,
.project-picker__entry.is-selected { background: var(--bg-hover); }
.project-picker__error { margin: 0; color: var(--danger); font-size: var(--fs-sm); }
.project-picker__footer { justify-content: flex-end; }
.project-picker__choose {
  min-height: 38px;
  padding-inline: var(--sp-4);
  box-shadow: 0 3px 10px color-mix(in srgb, var(--accent) 20%, transparent);
}

@media (max-width: 560px) {
  .project-picker__path {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .project-picker__parent {
    grid-column: 1 / -1;
    justify-self: start;
  }
}
</style>
