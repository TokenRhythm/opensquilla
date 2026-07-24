<template>
  <Teleport to="body">
    <div v-if="open && !nativePicking" class="modal-overlay" @click="emit('close')">
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
          <button class="btn btn--icon btn--ghost" :aria-label="t('common.close')" @click="emit('close')">
            <Icon name="x" :size="15" />
          </button>
        </header>
        <p class="project-picker__scope">{{ t('workspaces.webPickerScope') }}</p>
        <div class="project-picker__path">
          <input
            ref="pathInputRef"
            v-model="path"
            type="text"
            :aria-label="t('workspaces.projectPath')"
            :placeholder="t('workspaces.pathPlaceholder')"
            @keydown.enter.prevent="browse(path)"
          />
          <button class="btn btn--ghost" :disabled="loading" @click="browse(path)">
            {{ t('workspaces.browse') }}
          </button>
        </div>
        <p v-if="error" class="project-picker__error" role="alert">{{ error }}</p>
        <div class="project-picker__entries" role="listbox">
          <button
            v-for="entry in directories"
            :key="entry.path"
            type="button"
            role="option"
            :aria-selected="path === entry.path"
            class="project-picker__entry"
            :class="{ 'is-selected': path === entry.path }"
            @click="path = entry.path"
            @dblclick="browse(entry.path)"
          >
            <Icon name="sessions" :size="14" />
            <span>{{ entry.name }}</span>
          </button>
        </div>
        <footer class="project-picker__footer">
          <button class="btn btn--ghost" @click="emit('close')">{{ t('common.cancel') }}</button>
          <button class="btn btn--primary" :disabled="!path.trim()" @click="choose">
            {{ t('workspaces.choose') }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { getPlatform } from '@/platform'
import { useRpcStore } from '@/stores/rpc'
import { useDialogA11y } from '@/composables/useDialogA11y'

interface PathEntry {
  name: string
  path: string
  kind: string
  selectable: boolean
}

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
const path = ref('')
const entries = ref<PathEntry[]>([])
const loading = ref(false)
const nativePicking = ref(false)
const error = ref('')
const directories = computed(() => entries.value.filter(entry => entry.kind === 'directory'))

async function browse(target: string) {
  const normalized = target.trim() || '.'
  loading.value = true
  error.value = ''
  try {
    const response = await rpc.call<{
      path?: string
      parentPath?: string
      entries?: PathEntry[]
    }>('sandbox.path.list', {
      sessionKey: props.sessionKey,
      path: normalized,
      kind: 'workspace',
      browseChildren: true,
    })
    path.value = response.path || normalized
    entries.value = Array.isArray(response.entries) ? response.entries : []
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause)
    entries.value = []
  } finally {
    loading.value = false
  }
}

function choose() {
  const selected = path.value.trim()
  if (selected) emit('choose', selected)
}

watch(() => props.open, async open => {
  if (!open) return
  error.value = ''
  path.value = props.initialPath || ''
  const nativePicker = getPlatform().files.chooseProjectDirectory
  if (nativePicker) {
    nativePicking.value = true
    try {
      const choice = await nativePicker()
      if (choice?.path) emit('choose', choice.path)
      else emit('close')
    } finally {
      nativePicking.value = false
    }
    return
  }
  await nextTick()
  pathInputRef.value?.focus()
  await browse(path.value || '.')
}, { immediate: true })

useDialogA11y(dialogRef, computed(() => props.open && !nativePicking.value), () => emit('close'), {
  initialFocus: pathInputRef,
})
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
  width: min(92vw, 560px);
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
