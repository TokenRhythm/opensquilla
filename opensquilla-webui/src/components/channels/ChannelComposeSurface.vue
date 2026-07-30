<script setup lang="ts">
// Centered compose takeover for adding a channel: type gallery pre-pick, a
// receipt chip + the shared ChannelConfigEditor (compose mode) post-pick.
// The gallery→chip collapse is an honest state change animated on the shared
// motion tokens; reduced motion collapses it to instant. All draft state
// lives in the view-owned compose editor instance — this component is
// presentation and event wiring only.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import ChannelConfigEditor from '@/components/channels/ChannelConfigEditor.vue'
import ChannelTypeGallery from '@/components/channels/ChannelTypeGallery.vue'
import { useChannelCatalogI18n } from '@/composables/setup/useChannelCatalogI18n'
import type { ChannelEditorApi } from '@/composables/channels/useChannelEditor'

const props = defineProps<{
  editor: ChannelEditorApi
  pickedType: string
  confirmPending: boolean
  saving: boolean
}>()

const emit = defineEmits<{
  exit: []
  pick: [type: string]
  change: []
  test: []
  save: []
  saveAnyway: []
  keepEditing: []
  confirmDiscard: []
  loadCatalog: []
}>()

const { t } = useI18n()
const { localizeLabel } = useChannelCatalogI18n()

const picked = computed(() => Boolean(props.pickedType))
const spec = computed(() => props.editor.spec.value)
const specLabel = computed(() =>
  localizeLabel(props.pickedType, spec.value?.label || props.pickedType))
const transportLabel = computed(() => {
  const transport = spec.value?.transport || ''
  if (!transport || transport === 'unknown') return ''
  return transport.replace(/[_-]+/g, ' ').replace(/\b\w/g, char => char.toUpperCase())
})
const testing = computed(() => props.editor.probe.value.phase === 'running')

// Autofocus the first field the user must actually fill once the picked
// type's form is live: the first visible required field whose value is empty
// (the lead credential — the suggested name arrives prefilled), falling back
// to the name field when everything required is already seeded.
function firstActionableFieldName(): string {
  const rows = props.editor.panel.value.channelFields
  const target = rows.find(row =>
    row.field.required === true
    && row.field.advanced !== true
    && !String(row.value ?? '').trim())
  return target?.field.name || 'name'
}

watch(
  () => [props.pickedType, props.editor.phase.value] as const,
  ([type, phase], previous) => {
    const [oldType, oldPhase] = previous ?? ['', 'idle']
    if (type && phase === 'active' && (type !== oldType || oldPhase !== 'active')) {
      void nextTick(() => {
        // The deep-link late-seed re-runs startCompose (phase bounces
        // loading → active); if the user already put focus somewhere in the
        // form, the reseed must not yank it away.
        const active = document.activeElement
        if (active instanceof HTMLElement && active.closest('.chc__editor')) return
        const name = firstActionableFieldName()
        document.querySelector<HTMLElement>(
          `.chc [data-field="${name}"] input, .chc [data-field="${name}"] select`,
        )?.focus()
      })
    }
  },
)

/* ── Dialog a11y: initial focus, Tab trap, focus return ────────────────── */

const surfaceRef = ref<HTMLElement | null>(null)
let invokerEl: HTMLElement | null = null

// Escape stays with the host view's two-stage document handler; this listener
// only keeps Tab cycling inside the aria-modal surface.
function trapFocus(event: KeyboardEvent): void {
  if (event.key !== 'Tab') return
  const rootEl = surfaceRef.value
  if (!rootEl) return
  const focusables = Array.from(rootEl.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), '
    + 'textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'))
  if (focusables.length === 0) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const activeEl = document.activeElement as HTMLElement | null
  const inside = !!activeEl && rootEl.contains(activeEl)
  if (event.shiftKey && (!inside || activeEl === first)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (!inside || activeEl === last)) {
    event.preventDefault()
    first.focus()
  }
}

onMounted(() => {
  // Remember the invoker (the Add-channel button) so dismissing the takeover
  // hands focus back instead of dropping it on <body>.
  invokerEl = document.activeElement instanceof HTMLElement ? document.activeElement : null
  document.addEventListener('keydown', trapFocus)
  // Move focus INTO the dialog on open; the post-pick watch above then moves
  // it to the Name field once the form is live.
  void nextTick(() => surfaceRef.value?.focus())
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', trapFocus)
  if (invokerEl?.isConnected) invokerEl.focus()
  invokerEl = null
})
</script>

<template>
  <div class="chc" role="dialog" aria-modal="true" :aria-label="t('console.channels.addChannel')">
    <div class="chc__scrim" @click="emit('exit')"></div>
    <div ref="surfaceRef" class="chc__surface" tabindex="-1">
      <header class="chc__head">
        <button type="button" class="chc__back" @click="emit('exit')">
          <span aria-hidden="true">‹</span>
          <span>{{ t('console.channels.compose.back') }}</span>
        </button>
        <!-- The title narrates the step: platform pick first, then the add
             form named after the picked platform. -->
        <h2 class="chc__title">
          {{ picked
            ? t('console.channels.compose.addPlatform', { platform: specLabel })
            : t('console.channels.compose.galleryLabel') }}
        </h2>
      </header>

      <div class="chc__body">
        <Transition name="chc-swap" mode="out-in">
          <ChannelTypeGallery
            v-if="!picked"
            key="gallery"
            :channels="editor.catalog.value"
            :pending="editor.catalogPending.value"
            :error="editor.catalogError.value"
            @pick="type => emit('pick', type)"
            @retry="emit('loadCatalog')"
          />
          <div v-else key="form" class="chc__form">
            <div class="chc__chip">
              <span class="chc__chipmark" aria-hidden="true">✓</span>
              <strong class="chc__chipname">{{ specLabel }}</strong>
              <span v-if="transportLabel" class="chc__chiptransport">· {{ transportLabel }}</span>
              <button type="button" class="btn btn--ghost chc__chipchange" @click="emit('change')">
                {{ t('console.channels.compose.change') }}
              </button>
            </div>
            <ChannelConfigEditor
              class="chc__editor"
              :editor="editor"
              mode="compose"
              @save-anyway="emit('saveAnyway')"
              @retry="emit('pick', pickedType)"
            />
          </div>
        </Transition>
      </div>

      <div v-if="confirmPending" class="chc__footer chc__footer--confirm" role="group" :aria-label="t('console.channels.compose.discardQuestion', { platform: specLabel })">
        <span class="chc__question">{{ t('console.channels.compose.discardQuestion', { platform: specLabel }) }}</span>
        <div class="chc__actions">
          <button type="button" class="btn btn--ghost" @click="emit('keepEditing')">
            {{ t('console.channels.editor.keepEditing') }}
          </button>
          <button type="button" class="btn btn--ghost chc__danger" @click="emit('confirmDiscard')">
            {{ t('console.channels.editor.discard') }}
          </button>
        </div>
      </div>
      <footer v-else-if="picked" class="chc__footer">
        <button type="button" class="btn btn--ghost" :disabled="testing || saving" @click="emit('test')">
          {{ testing ? t('setup.channels.testing') : t('setup.channels.testConnection') }}
        </button>
        <div class="chc__actions">
          <button type="button" class="btn btn--ghost" :disabled="saving" @click="emit('exit')">
            {{ t('common.cancel') }}
          </button>
          <button type="button" class="btn btn--primary" :disabled="saving" @click="emit('save')">
            {{ saving ? t('console.channels.editor.saving') : t('setup.channels.save') }}
          </button>
        </div>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.chc { inset: 0; position: fixed; z-index: 50; }
.chc__scrim { background: color-mix(in srgb, var(--bg) 60%, transparent); inset: 0; overscroll-behavior: contain; position: fixed; }
.chc__surface {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--elev-3);
  display: flex;
  flex-direction: column;
  left: 50%;
  max-height: calc(100vh - 96px);
  max-width: 720px;
  outline: 0;
  overflow: hidden;
  position: fixed;
  top: 64px;
  transform: translateX(-50%);
  width: calc(100vw - 32px);
}
.chc__head { align-items: center; border-bottom: 1px solid var(--border); display: flex; gap: var(--sp-3); padding: var(--sp-3) var(--sp-4); }
.chc__back { align-items: center; background: transparent; border: 0; color: var(--text-muted); cursor: pointer; display: inline-flex; font: inherit; font-size: var(--fs-sm); gap: 5px; padding: 4px 6px 4px 0; }
.chc__back:hover { color: var(--text); }
.chc__title { font-size: var(--fs-md); margin: 0; }
.chc__body { overflow-y: auto; overscroll-behavior: contain; padding: var(--sp-4); }
.chc__form { display: grid; gap: var(--sp-3); }
.chc__chip { align-items: baseline; border: 1px solid var(--border); border-radius: var(--radius-md); display: flex; flex-wrap: wrap; gap: var(--sp-2); padding: 8px 12px; }
.chc__chipmark { color: var(--ok); }
.chc__chipname { font-size: var(--fs-sm); }
.chc__chiptransport { color: var(--text-dim); font-size: var(--fs-sm); }
.chc__chipchange { margin-left: auto; min-height: 28px; padding: 2px 10px; }
/* The shared editor brings its own tab-panel padding; the surface body
   already provides it, so strip the double inset (scoped, higher specificity
   than the editor's own .cfge rule). */
.chc__body .chc__editor { padding: 0; }
.chc__footer { align-items: center; border-top: 1px solid var(--border); display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-3); justify-content: space-between; padding: var(--sp-2) var(--sp-4); }
.chc__question { color: var(--text); font-size: var(--fs-sm); font-weight: 600; min-width: 0; overflow-wrap: anywhere; }
.chc__actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-left: auto; }
.chc__actions .btn, .chc__footer > .btn { min-height: 32px; padding: 5px 12px; }
.chc__danger { color: var(--danger); }

/* Gallery ⇄ form swap: the receipt-chip collapse rides the shared tokens. */
.chc-swap-enter-active, .chc-swap-leave-active { transition: opacity var(--dur-base) var(--ease-out), transform var(--dur-base) var(--ease-out); }
.chc-swap-enter-from, .chc-swap-leave-to { opacity: 0; transform: translateY(6px); }
@media (prefers-reduced-motion: reduce) {
  .chc-swap-enter-active, .chc-swap-leave-active { transition: none; }
}

@media (max-width: 768px) {
  /* Full-width sheet above the mobile tab bar, mirroring the aside overlay. */
  .chc__surface {
    bottom: calc(var(--mobile-tabbar-h, 56px) + env(safe-area-inset-bottom, 0px) + 12px);
    border-radius: var(--radius-md);
    left: 12px;
    max-height: none;
    max-width: none;
    right: 12px;
    top: 48px;
    transform: none;
    width: auto;
  }
}
</style>
