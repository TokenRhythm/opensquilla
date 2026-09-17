<template>
  <Teleport to="body">
    <div v-if="open" class="modal-overlay" @click="emit('close')">
      <section
        ref="dialogRef"
        class="modal project-edit"
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-workspace-edit-title"
        @click.stop
      >
        <h3 id="project-workspace-edit-title">{{ t('workspaces.editProject') }}</h3>
        <label>
          <span>{{ t('workspaces.projectName') }}</span>
          <input ref="nameInputRef" v-model="name" maxlength="120" @keydown.enter.prevent="save" />
        </label>
        <label>
          <span>{{ t('workspaces.projectPath') }}</span>
          <input :value="path" readonly />
        </label>
        <footer>
          <button class="btn btn--ghost" @click="emit('close')">{{ t('common.cancel') }}</button>
          <button class="btn btn--primary" :disabled="!name.trim()" @click="save">{{ t('common.save') }}</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDialogA11y } from '@/composables/useDialogA11y'

const props = defineProps<{ open: boolean; initialName: string; path: string }>()
const emit = defineEmits<{ close: []; save: [name: string] }>()
const { t } = useI18n()
const dialogRef = ref<HTMLElement | null>(null)
const nameInputRef = ref<HTMLInputElement | null>(null)
const name = ref('')

watch(() => props.open, async open => {
  if (!open) return
  name.value = props.initialName
  await nextTick()
  nameInputRef.value?.select()
}, { immediate: true })

function save() {
  const value = name.value.trim()
  if (value) emit('save', value)
}

useDialogA11y(dialogRef, computed(() => props.open), () => emit('close'), {
  initialFocus: nameInputRef,
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
.project-edit {
  width: min(92vw, 440px);
  padding: var(--sp-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-modal);
  background: var(--bg-surface);
}
.project-edit h3 { margin: 0 0 var(--sp-4); }
.project-edit label { display: grid; gap: var(--sp-2); margin-bottom: var(--sp-3); color: var(--text-muted); font-size: var(--fs-sm); }
.project-edit input { width: 100%; }
.project-edit footer { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-4); }
</style>
