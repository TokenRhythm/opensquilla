<template>
  <Teleport to="body">
    <div
      v-if="open && phase !== 'closed' && phase !== 'native-picking'"
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
              class="btn btn--ghost"
              :disabled="!parentDirectory"
              @click="browse(parentDirectory || undefined)"
            >
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
              class="btn btn--ghost"
              @click="browse(locationDraft)"
            >
              {{ t('workspaces.goToPath') }}
            </button>
          </div>
          <p v-if="error" class="project-picker__error" role="alert">{{ error }}</p>
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
              <Icon name="sessions" :size="14" />
              <span>{{ entry.name }}</span>
            </button>
          </div>
          <footer class="project-picker__footer">
            <button class="btn btn--ghost" @click="closeDialog">
              {{ t('common.cancel') }}
            </button>
            <button
              class="btn btn--primary"
              :disabled="!canChoose"
              @click="choose"
            >
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

const props = defineProps<{
  open: boolean
  sessionKey: string
  initialPath?: string
}>()
const emit = defineEmits<{
  close: []
  choose: [path: string]
}>()
const { t } = useI18n()
const rpc = useRpcStore()
const dialogRef = ref<HTMLElement | null>(null)
const pathInputRef = ref<HTMLInputElement | null>(null)
const phase = ref<PickerPhase>('closed')
const currentDirectory = ref('')
const selectedDirectory = ref('')
const locationDraft = ref('')
const parentDirectory = ref<string | null>(null)
const entries = ref<SandboxPathEntry[]>([])
const error = ref('')
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
  return props.open && epoch === openEpoch && sequence === browseSequence
}

async function browse(target?: string, epoch = openEpoch) {
  if (!props.open || epoch !== openEpoch) return
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
  if (!canChoose.value) return
  const selected = selectedDirectory.value.trim()
  invalidateAndClose()
  emit('choose', selected)
}

async function runNativePicker(epoch: number) {
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
    const choice = await nativePicker()
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
    if (!open) {
      phase.value = 'closed'
      return
    }
    const epoch = openEpoch
    await runNativePicker(epoch)
  },
  { immediate: true },
)

useDialogA11y(
  dialogRef,
  computed(
    () => props.open
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
.project-picker__path,
.project-picker__footer {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.project-picker__header h3 { flex: 1; margin: 0; }
.project-picker__scope { margin: 0; color: var(--text-muted); font-size: var(--fs-sm); }
.project-picker__path input { flex: 1; min-width: 0; }
.project-picker__entries {
  min-height: 180px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--sp-1);
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
</style>
