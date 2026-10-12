<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import ControlSwitch from '@/components/ControlSwitch.vue'
import { useShortcutsStore, SHORTCUT_DEFS, type ShortcutId } from '@/stores/shortcuts'
import { eventToBinding, formatBinding } from '@/utils/keychord'
import { isMacPlatform } from '@/utils/browser'

const { t } = useI18n()

// Client-only preferences: global chord shortcuts persisted to this browser via
// the shortcuts store. Like Appearance, changes apply instantly and never enter
// the settings dirty bar. The sidebar New chat badge + the palette hint are
// reactive shortcuts over the SAME store, so the surfaces can never drift.
const shortcuts = useShortcutsStore()
const mac = isMacPlatform()

// At most one shortcut records at a time. While recording, a capture-phase
// window listener owns every keydown so the chord is captured here instead of
// firing the app shortcut (or closing the dialog on Escape).
const recordingId = ref<ShortcutId | null>(null)
const recordError = ref('')

function displayChord(id: ShortcutId): string {
  return formatBinding(shortcuts.states[id].binding, mac)
}

function onToggle(id: ShortcutId, enabled: boolean) {
  shortcuts.setEnabled(id, enabled)
}

function startRecording(id: ShortcutId) {
  recordError.value = ''
  recordingId.value = id
  window.addEventListener('keydown', onRecordKey, true)
}

function stopRecording() {
  recordingId.value = null
  window.removeEventListener('keydown', onRecordKey, true)
}

function onRecordKey(e: KeyboardEvent) {
  // Own the event entirely so it never reaches the app handler or the dialog.
  e.preventDefault()
  e.stopPropagation()

  if (e.key === 'Escape') {
    recordError.value = ''
    stopRecording()
    return
  }

  const id = recordingId.value
  if (!id) return

  const binding = eventToBinding(e, mac)
  // Null while only modifiers are held, or when no primary modifier is present —
  // keep waiting for a full Ctrl/⌘-based chord rather than recording a bare key.
  if (!binding) {
    recordError.value = t('setup.keyboard.addModifier', { modifier: mac ? '⌘' : 'Ctrl' })
    return
  }

  const conflict = shortcuts.findConflict(binding, id)
  if (conflict) {
    const other = SHORTCUT_DEFS.find(d => d.id === conflict)
    recordError.value = t('setup.keyboard.conflict', { label: other?.label ?? conflict })
    return
  }

  shortcuts.setBinding(id, binding)
  // Binding a chord implies wanting it active; enable so the new key works at
  // once and the badge/hint update without a second click.
  shortcuts.setEnabled(id, true)
  recordError.value = ''
  stopRecording()
}

function reset(id: ShortcutId) {
  if (recordingId.value === id) stopRecording()
  recordError.value = ''
  shortcuts.resetBinding(id)
}

onBeforeUnmount(stopRecording)
</script>

<template>
  <section class="control-section">
    <div class="control-section__head">
      <h3 class="control-section__title">{{ t('setup.keyboard.title') }}</h3>
      <p class="control-section__desc">{{ t('setup.keyboard.desc') }}</p>
    </div>

    <div v-for="def in SHORTCUT_DEFS" :key="def.id" class="control-row">
      <div class="control-row__label-block">
        <span class="control-row__label">{{ def.label }}</span>
        <span class="control-row__desc">{{ def.description }}</span>
        <p v-if="recordingId === def.id && recordError" class="kb-error" role="alert">{{ recordError }}</p>
      </div>

      <div class="control-row__control kb-control">
        <template v-if="recordingId === def.id">
          <span class="kb-recording" aria-live="polite">{{ t('setup.keyboard.pressKeys') }}</span>
          <button type="button" class="btn btn--ghost kb-btn" @click="stopRecording">{{ t('common.cancel') }}</button>
        </template>
        <template v-else>
          <kbd
            class="kb-chord"
            :class="{ 'is-off': !shortcuts.states[def.id].enabled }"
          >{{ displayChord(def.id) || '—' }}</kbd>
          <button type="button" class="btn btn--ghost kb-btn" @click="startRecording(def.id)">{{ t('setup.keyboard.rebind') }}</button>
          <button type="button" class="btn btn--ghost kb-btn" @click="reset(def.id)">{{ t('setup.keyboard.reset') }}</button>
          <ControlSwitch
            :checked="shortcuts.states[def.id].enabled"
            :aria-label="t('setup.keyboard.enableAria', { label: def.label })"
            @change="onToggle(def.id, $event)"
          />
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.kb-control {
  flex-wrap: wrap;
}

.kb-chord {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-weight: 500;
  line-height: 1.4;
  padding: 2px 8px;
}

/* Disabled shortcut: show the bound chord but read it as inactive. */
.kb-chord.is-off {
  color: var(--text-muted);
  opacity: 0.6;
}

.kb-recording {
  color: var(--accent);
  font-size: var(--fs-xs);
  font-weight: 600;
}

.kb-btn {
  font-size: var(--fs-xs);
  padding: 4px var(--sp-2);
}

.kb-error {
  color: var(--danger);
  font-size: var(--fs-xs);
  margin: 4px 0 0;
}
</style>
