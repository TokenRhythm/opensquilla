<template>
  <form class="hub-task" @submit.prevent="submit">
    <textarea
      ref="textareaRef"
      v-model="text"
      class="hub-task__input"
      rows="1"
      :placeholder="t('sessions.taskInput.placeholder')"
      :aria-label="t('sessions.taskInput.ariaLabel')"
      @keydown.enter.exact.prevent="submit"
    ></textarea>
    <div class="hub-task__bar">
      <span v-show="text.trim()" class="hub-task__hint">{{ t('sessions.taskInput.hint') }}</span>
      <button
        type="submit"
        class="btn btn--primary hub-task__send"
        :disabled="!text.trim()"
      >
        <Icon name="arrowUp" :size="16" />
        <span>{{ t('sessions.taskInput.start') }}</span>
      </button>
    </div>
  </form>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'

const { t } = useI18n()

const emit = defineEmits<{
  submit: [text: string]
}>()

const text = ref('')
const textareaRef = ref<HTMLTextAreaElement | null>(null)

function submit() {
  const value = text.value.trim()
  if (!value) return
  emit('submit', value)
  text.value = ''
}
</script>

<style scoped>
.hub-task {
  align-items: center;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  display: grid;
  gap: var(--sp-3);
  grid-template-columns: minmax(0, 1fr) auto;
  padding: 8px 10px 8px 18px;
  transition: border-color var(--transition), box-shadow var(--transition);
}

.hub-task:focus-within {
  border-color: var(--border-focus);
  box-shadow: var(--focus-ring);
}

.hub-task__input {
  background: transparent;
  border: none;
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 1rem;
  line-height: 1.5;
  min-height: 40px;
  outline: none;
  overflow-y: auto;
  padding: 8px 0;
  resize: none;
  width: 100%;
}

.hub-task__input::placeholder {
  color: var(--text-dim);
}

.hub-task__bar {
  align-items: center;
  display: flex;
  justify-content: flex-end;
}

.hub-task__hint {
  display: none;
}

.hub-task__send {
  align-items: center;
  display: inline-flex;
  gap: 6px;
  min-height: 38px;
}

.hub-task__send:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

@media (max-width: 760px) {
  .hub-task__hint {
    display: none;
  }
}
</style>
