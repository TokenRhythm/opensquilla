<template>
  <section class="annotation-comment" :class="{ 'is-expanded': expanded }" :aria-label="label" :aria-busy="busy">
    <p v-if="showTarget" class="annotation-comment__target" :title="label">{{ label }}</p>
    <div class="annotation-comment__field">
      <textarea ref="input" :value="body" rows="1" :readonly="busy" :maxlength="16384"
        :placeholder="t('workbench.annotationUnified.placeholder')" :aria-label="label"
        @input="change" @keydown="keydown" @compositionstart="composing = true" @compositionend="composing = false" />
      <button v-if="expanded" type="button" class="annotation-comment__record" :disabled="!canRecord || composing"
        :title="t('workbench.annotationUnified.submit', { count: 1 })"
        :aria-label="t('workbench.annotationUnified.submit', { count: 1 })"
        @click="record"><Icon name="arrowUp" :size="17" /></button>
    </div>
    <p v-if="error || stale" class="annotation-comment__error" role="alert">{{ error || t('workbench.annotationUnified.stale') }}</p>
    <div v-if="expanded" class="annotation-comment__actions">
      <small>{{ t('workbench.annotationUnified.recordHint') }}</small>
      <button type="button" :disabled="busy || composing" @click="emit('reselect')">{{ t('workbench.annotationUnified.reselect') }}</button>
      <button type="button" :disabled="busy || composing" @click="emit('cancel')">{{ t('workbench.annotationUnified.cancel') }}</button>
    </div>
  </section>
</template>
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Icon from '@/components/Icon.vue'
import { promptAnnotationBodyWithinLimit } from '@/types/promptAnnotations'
const props = defineProps<{ body: string; label: string; count: number; busy?: boolean; stale?: boolean; error?: string; showTarget?: boolean }>()
const emit = defineEmits<{ update: [body: string]; record: []; cancel: []; reselect: [] }>()
const { t } = useI18n()
const input = ref<HTMLTextAreaElement>()
const composing = ref(false)
const expanded = computed(() => Boolean(props.body || props.error || props.stale || props.busy))
const canRecord = computed(() => !props.busy && !props.stale && Boolean(props.body.trim()) && promptAnnotationBodyWithinLimit(props.body))
let observer: ResizeObserver | null = null
function record() { if (canRecord.value && !composing.value) emit('record') }
function resize() {
  if (!input.value) return
  input.value.style.height = '24px'
  input.value.style.height = `${Math.min(160, Math.max(24, input.value.scrollHeight))}px`
}
function change(event: Event) { emit('update', (event.target as HTMLTextAreaElement).value); resize() }
function keydown(event: KeyboardEvent) {
  if (props.busy || event.isComposing || composing.value || event.keyCode === 229) return
  if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); emit('cancel') }
  if (event.key === 'Enter' && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
    event.preventDefault()
    record()
  }
}
watch(() => props.body, () => nextTick(resize))
onMounted(() => {
  resize(); input.value?.focus({ preventScroll: true })
  if (typeof ResizeObserver !== 'undefined' && input.value?.parentElement) {
    let previousWidth = input.value.parentElement.getBoundingClientRect().width
    observer = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width
      if (width !== undefined && width !== previousWidth) { previousWidth = width; resize() }
    })
    observer.observe(input.value.parentElement)
  }
})
onBeforeUnmount(() => observer?.disconnect())
</script>
<style scoped>
.annotation-comment { position: relative; border: 1px solid var(--border-strong); border-radius: var(--radius-md); background: var(--bg-surface); color: var(--text); box-shadow: var(--shadow-lg); overflow: hidden; }
.annotation-comment::before { content: ''; position: absolute; inset: 12px auto 12px 0; width: 3px; border-radius: 0 var(--radius-xs) var(--radius-xs) 0; background: var(--accent); }
.annotation-comment__target { margin: 0; padding: 8px 12px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font-size: var(--fs-xs); }
.annotation-comment__field { display: flex; align-items: flex-end; gap: 8px; padding: 10px 12px; }
textarea { flex: 1; min-width: 0; width: 100%; min-height: 24px; max-height: 160px; padding: 0; margin: 0; background: transparent; color: inherit; border: 0; outline: 0; resize: none; font: 14px/24px var(--font-sans); }
.annotation-comment__record { display: grid; place-items: center; flex: 0 0 28px; height: 28px; border: 0; border-radius: var(--radius-md); background: var(--accent); color: var(--accent-foreground); cursor: pointer; }
.annotation-comment__actions { display: flex; align-items: center; gap: 8px; padding: 0 12px 8px; color: var(--text-muted); }
small { flex: 1; min-width: 0; font-size: 10px; }
.annotation-comment__actions button { border: 0; padding: 0; background: transparent; color: inherit; font-size: 11px; cursor: pointer; }
button:disabled { opacity: .45; cursor: default; }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.annotation-comment__error { margin: 0; padding: 0 12px 8px; color: var(--danger); font-size: 12px; }
</style>
