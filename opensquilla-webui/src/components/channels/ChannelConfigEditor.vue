<script setup lang="ts">
// The Configuration tab body for the /channels workspace. One skeleton, two
// modes: read renders the saved entry as text on a 148px label rail; edit
// turns each row into a live field IN PLACE (same rail, same geometry).
// Field specs, grouping, secret markers, and setup aids all come from the
// editor composable's catalog — nothing here invents structure.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ChannelConfigRow, { type ConfigRowModel } from '@/components/channels/ChannelConfigRow.vue'
import FeishuSetupAids from '@/components/channels/FeishuSetupAids.vue'
import Icon from '@/components/Icon.vue'
import { useChannelCatalogI18n } from '@/composables/setup/useChannelCatalogI18n'
import type { ChannelEditorApi } from '@/composables/channels/useChannelEditor'

const props = defineProps<{
  editor: ChannelEditorApi
  mode: 'read' | 'edit' | 'compose'
}>()

const emit = defineEmits<{
  saveAnyway: []
  retry: []
}>()

const { t } = useI18n()
const {
  localizeFieldChoice, localizeFieldDescription, localizeFieldLabel, localizeGroupLabel,
} = useChannelCatalogI18n()

// "editing" = any live-field mode (edit-in-place or compose); "edit" alone
// still gates edit-only affordances like the locked name row.
const editing = computed(() => props.mode !== 'read')
const isEdit = computed(() => props.mode === 'edit')
const phase = computed(() => props.editor.phase.value)
const spec = computed(() => props.editor.spec.value)
const probe = computed(() => props.editor.probe.value)
const fieldErrors = computed(() => props.editor.fieldErrors.value)
const extraRows = computed(() => props.editor.extraRows.value)
const saving = computed(() => props.editor.saving.value)

interface EditorGroup {
  name: string
  rows: ConfigRowModel[]
}

// Localized display labels for a select field's raw choice values (the row
// still submits the raw value; this is presentation only).
function choiceLabelsFor(
  type: string,
  field: { type?: string; name: string; choices?: string[] },
): Record<string, string> | undefined {
  if (field.type !== 'select' || !field.choices?.length) return undefined
  return Object.fromEntries(
    field.choices.map(choice => [choice, localizeFieldChoice(type, field.name, choice, choice)]),
  )
}

// Merge the form's field rows and secret rows back into catalog order, then
// split into named main groups plus the advanced fold. Secrets keep their
// spec position (app_secret sits inside Credentials next to app_id), unlike
// the settings panel which appends them after the groups.
const grouped = computed<{ main: EditorGroup[]; advanced: ConfigRowModel[] }>(() => {
  const view = props.editor.panel.value
  const type = props.editor.entryType.value
  const edited = new Set(props.editor.editedFields.value)
  const byName = new Map<string, ConfigRowModel>()
  for (const row of view.channelFields) {
    byName.set(row.field.name, {
      // The name is locked text only when editing an existing entry; a
      // compose draft's name is an ordinary editable field.
      kind: isEdit.value && row.field.name === 'name' ? 'name' : 'field',
      field: row.field,
      label: localizeFieldLabel(type, row.field.name, row.field.label),
      description: localizeFieldDescription(type, row.field.name, String(row.field.description || '')),
      value: String(row.value ?? ''),
      edited: edited.has(row.field.name),
      choiceLabels: choiceLabelsFor(type, row.field),
    })
  }
  for (const row of view.secretRows) {
    byName.set(row.field.name, {
      kind: 'secret',
      field: row.field,
      label: localizeFieldLabel(type, row.field.name, row.field.label),
      description: localizeFieldDescription(type, row.field.name, String(row.field.description || '')),
      value: row.value,
      edited: edited.has(row.field.name),
      hasStored: row.hasStored,
      replacing: row.replacing,
    })
  }
  const main: EditorGroup[] = []
  const advanced: ConfigRowModel[] = []
  const mainByGroup = new Map<string, ConfigRowModel[]>()
  for (const field of props.editor.specFields.value) {
    const row = byName.get(field.name)
    if (!row) continue // hidden by show_when
    if (field.advanced === true) {
      advanced.push(row)
      continue
    }
    const groupName = String(field.group || '')
    if (!mainByGroup.has(groupName)) {
      const rows: ConfigRowModel[] = []
      mainByGroup.set(groupName, rows)
      main.push({ name: groupName, rows })
    }
    mainByGroup.get(groupName)!.push(row)
  }
  // Credentials lead: the values the user must actually fetch from the
  // provider console render first; everything else keeps catalog order.
  // Stable sort, and shared by read/edit/compose so the in-place edit flip
  // never reflows the rail.
  main.sort((a, b) => Number(a.name !== 'credentials') - Number(b.name !== 'credentials'))
  return { main, advanced }
})

// Field names present in EVERY catalog spec are the shared plumbing
// (agent routing, enable switch, session/access policy) — identical across
// channel types, so previewing them tells the user nothing type-specific.
const commonFieldNames = computed<Set<string>>(() => {
  let names: Set<string> | null = null
  for (const s of props.editor.catalog.value) {
    const current = new Set<string>((s.fields ?? []).map(f => String(f.name)))
    if (names === null) {
      names = current
      continue
    }
    const kept = new Set<string>()
    for (const candidate of names) {
      if (current.has(candidate)) kept.add(candidate)
    }
    names = kept
  }
  return names ?? new Set<string>()
})

// The Advanced fold summary names what it hides — channel-specific folds
// first (a webhook operator sees "Connection mode" hinted without expanding),
// then the shared plumbing, plus an overflow count.
const advancedPreview = computed(() => {
  const rows = grouped.value.advanced
  const specific = rows.filter(row => !commonFieldNames.value.has(row.field.name))
  const shared = rows.filter(row => commonFieldNames.value.has(row.field.name))
  const labels = [...specific, ...shared].map(row => row.label)
  const shown = labels.slice(0, 3)
  const rest = labels.length - shown.length
  return shown.join(' · ') + (rest > 0 ? ` +${rest}` : '')
})

// Setup aids (Feishu console shortcuts): copy/link aids form a titled group
// between the credential groups and Advanced. Note-kind aids have no in-form
// rendering — the only one (feishu ws_order_note) is post-save guidance,
// rendered by the channel page's final-step callout.
const setupAids = computed(() => spec.value?.setupAids ?? [])
const inlineAids = computed(() => setupAids.value.filter(a => a.kind === 'copy' || a.kind === 'link'))

const currentAppId = computed(() => {
  const row = props.editor.panel.value.channelFields.find(r => r.field.name === 'app_id')
  return String(row?.value || '').trim()
})
const domainIsLark = computed(() => {
  const row = props.editor.panel.value.channelFields.find(r => r.field.name === 'domain')
  return String(row?.value || '') === 'lark'
})

function humanize(value: string): string {
  return value.replace(/[_-]+/g, ' ').replace(/\b\w/g, char => char.toUpperCase())
}

// Extras keys are raw config identifiers; a trailing unit suffix must read
// as a unit, not a stray capitalized word ("debounce_window_s" →
// "Debounce Window (s)", never "Debounce Window S").
const UNIT_SUFFIXES: Array<[string, string]> = [['_ms', 'ms'], ['_s', 's']]
function humanizeKey(key: string): string {
  for (const [suffix, unit] of UNIT_SUFFIXES) {
    if (key.endsWith(suffix) && key.length > suffix.length) {
      return `${humanize(key.slice(0, -suffix.length))} (${unit})`
    }
  }
  return humanize(key)
}

function extraLabel(key: string): string {
  return localizeFieldLabel(props.editor.entryType.value, key, humanizeKey(key))
}

function groupTitle(name: string): string {
  return localizeGroupLabel(props.editor.entryType.value, name, humanize(name))
}

function onUpdate(name: string, value: unknown) {
  props.editor.updateField(name, value)
}
function onReplace(name: string) {
  props.editor.replaceSecret(name)
}
function onCancelReplace(name: string) {
  props.editor.cancelSecretReplace(name)
}
</script>

<template>
  <div class="cfge" :class="{ 'is-edit': editing }">
    <!-- Skeleton rail: catalog/config still loading — the geometry the real
         rows will occupy, so opening Configuration never blocks or jumps. -->
    <div v-if="phase === 'loading' || phase === 'idle'" class="cfge__skeleton" aria-hidden="true">
      <div v-for="n in 5" :key="n" class="cfge__skeleton-row">
        <span class="cfge__skeleton-label"></span>
        <span class="cfge__skeleton-value"></span>
      </div>
    </div>

    <div v-else-if="phase === 'error'" class="cfge__load-error" role="alert">
      <span>{{ editor.loadError.value }}</span>
      <button type="button" class="btn btn--ghost" @click="emit('retry')">
        {{ t('console.common.retry') }}
      </button>
    </div>

    <template v-else-if="spec">
      <section
        v-for="group in grouped.main"
        :key="group.name || 'main'"
        class="cfge__group"
        :aria-label="group.name ? groupTitle(group.name) : undefined"
      >
        <h4 v-if="group.name" class="cfge__group-title">{{ groupTitle(group.name) }}</h4>
        <ChannelConfigRow
          v-for="row in group.rows"
          :key="row.field.name"
          :row="row"
          :edit="editing"
          :error="fieldErrors[row.field.name]"
          @update="onUpdate"
          @replace="onReplace"
          @cancel-replace="onCancelReplace"
        />
      </section>

      <FeishuSetupAids
        v-if="editing && inlineAids.length"
        class="cfge__group"
        :aids="inlineAids"
        :app-id="currentAppId"
        :lark="domainIsLark"
      />

      <details v-if="grouped.advanced.length" class="cfge__advanced cfge__group">
        <summary>
          <span>{{ t('setup.channels.advancedGroup') }}</span>
          <span v-if="advancedPreview" class="cfge__advanced-preview">· {{ advancedPreview }}</span>
        </summary>
        <div class="cfge__advanced-body">
          <ChannelConfigRow
            v-for="row in grouped.advanced"
            :key="row.field.name"
            :row="row"
            :edit="editing"
            :error="fieldErrors[row.field.name]"
            @update="onUpdate"
            @replace="onReplace"
            @cancel-replace="onCancelReplace"
          />
        </div>
      </details>

      <section v-if="extraRows.length" class="cfge__group" :aria-label="t('console.channels.editor.otherSettings')">
        <h4 class="cfge__group-title">{{ t('console.channels.editor.otherSettings') }}</h4>
        <div v-for="row in extraRows" :key="row.key" class="cfge__row cfge__row--extra">
          <div class="cfge__rail"><span class="cfge__label">{{ extraLabel(row.key) }}</span></div>
          <div class="cfge__control">
            <span class="cfge__value" :class="{ 'cfge__value--secret': row.secret }">
              {{ row.secret ? t('console.channels.editor.storedSecret') : row.value }}
            </span>
          </div>
        </div>
      </section>

      <!-- Draft probe transcript: pass/fail rows in the Diagnostics idiom,
           never a toast-only error. Renders above the sticky action bar. -->
      <div v-if="editing && probe.phase !== 'idle'" class="cfge__transcript" role="status">
        <div v-if="probe.phase === 'running'" class="cfge__transcript-row is-info">
          <Icon name="gauge" :size="14" aria-hidden="true" />
          <span>{{ t('console.channels.editor.probeRunning') }}</span>
        </div>
        <template v-else>
          <div
            v-for="row in probe.rows"
            :key="row.id"
            class="cfge__transcript-row"
            :class="row.tone === 'ok' ? 'is-ok' : row.tone === 'fail' ? 'is-fail' : 'is-info'"
          >
            <Icon :name="row.tone === 'ok' ? 'check' : row.tone === 'fail' ? 'x' : 'info'" :size="14" aria-hidden="true" />
            <span class="cfge__transcript-text">{{ row.text }}</span>
            <span v-if="row.id === 'validate' && probe.latencyMs != null" class="cfge__transcript-latency">
              {{ t('console.channels.editor.probeLatency', { ms: probe.latencyMs }) }}
            </span>
          </div>
          <div v-if="probe.ok === false && probe.fromSave" class="cfge__transcript-actions">
            <button type="button" class="btn btn--ghost" :disabled="saving" @click="emit('saveAnyway')">
              {{ t('console.channels.editor.saveAnyway') }}
            </button>
          </div>
        </template>
      </div>
    </template>

    <!-- Unknown catalog type: the saved entry stays readable (and removable),
         it just is not editable from here. -->
    <template v-else>
      <div v-for="row in extraRows" :key="row.key" class="cfge__row cfge__row--extra">
        <div class="cfge__rail"><span class="cfge__label">{{ extraLabel(row.key) }}</span></div>
        <div class="cfge__control">
          <span class="cfge__value" :class="{ 'cfge__value--secret': row.secret }">
            {{ row.secret ? t('console.channels.editor.storedSecret') : row.value }}
          </span>
        </div>
      </div>
      <p class="cfge__hint">{{ t('console.channels.editor.unknownType', { type: editor.entryType.value || '?' }) }}</p>
    </template>
  </div>
</template>

<style scoped>
/* 8px rhythm: 8 inside a field (rail → control gap), 16 between fields,
   28 between groups. The rail is a fixed 148px so read and edit share one
   skeleton and the flip reflows nothing. */
.cfge { display: block; padding: var(--sp-3) var(--sp-4) var(--sp-4); }
.cfge__group + .cfge__group { margin-top: 28px; }
/* Read mode is a fact sheet, not a form: on wide panels the record flows in
   two columns so a saved channel reads at a glance instead of as a strip. */
@media (min-width: 1100px) {
  .cfge:not(.is-edit) section.cfge__group { column-gap: 56px; display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); row-gap: var(--sp-4); }
  .cfge:not(.is-edit) section.cfge__group > .cfge__group-title { grid-column: 1 / -1; }
  /* The strip layout's sibling margin (below) misaligns the first row of each
     grid column and doubles the rhythm inside cells — the grid's row-gap owns
     the spacing here. */
  .cfge:not(.is-edit) section.cfge__group > .cfge__row { margin-top: 0; }
}
.cfge:not(.is-edit) .cfge__row { min-height: 32px; }
.cfge__group-title {
  border-bottom: 1px solid var(--border);
  color: var(--text);
  font-size: var(--fs-sm);
  font-weight: 600;
  margin: 0 0 var(--sp-3);
  padding-bottom: var(--sp-2);
}
.cfge__row { align-items: start; column-gap: var(--sp-2); display: grid; grid-template-columns: 148px minmax(0, 1fr); }
.cfge__row + .cfge__row { margin-top: var(--sp-4); }
.cfge :deep(.cfge__rail) { align-items: baseline; display: flex; gap: 6px; min-width: 0; padding-top: 6px; }
.cfge :deep(.cfge__label) { color: var(--text-dim); font-size: var(--fs-sm); font-weight: 500; overflow-wrap: anywhere; }
.cfge :deep(.cfge__tick) { color: var(--text); flex: none; font-size: 8px; line-height: 1; }
.cfge :deep(.cfge__control) { display: grid; gap: var(--sp-1); min-width: 0; }
.cfge :deep(.cfge__value),
.cfge :deep(.cfge__input) {
  border: 1px solid transparent;
  border-radius: var(--radius-control);
  box-sizing: border-box;
  color: var(--text);
  font: inherit;
  font-size: var(--fs-sm);
  line-height: 20px;
  min-height: 32px;
  overflow-wrap: anywhere;
  padding: 5px 10px;
  width: 100%;
}
.cfge :deep(.cfge__value) { display: inline-flex; align-items: center; gap: 8px; }
.cfge :deep(.cfge__input) { background: var(--bg); border-color: var(--border); min-width: 0; outline: 0; }
.cfge :deep(.cfge__input:focus-visible) { border-color: var(--accent); box-shadow: var(--focus-ring); }
.cfge :deep(.cfge__value--secret) { font-variant-numeric: tabular-nums; letter-spacing: 0.08em; }
.cfge :deep(.cfge__value--empty) { color: var(--text-dim); }
.cfge :deep(.cfge__value--bool.is-off) { color: var(--text-muted); }
.cfge :deep(.cfge__booldot) { background: var(--text-dim); border-radius: 50%; flex: none; height: 7px; width: 7px; }
.cfge :deep(.cfge__value--bool.is-on .cfge__booldot) { background: var(--ok); }
.cfge :deep(.cfge__value--locked > svg) { color: var(--text-dim); flex: none; }
.cfge :deep(.cfge__secretline) { align-items: center; display: flex; gap: var(--sp-2); }
.cfge :deep(.cfge__secretline .cfge__value),
.cfge :deep(.cfge__secretline .cfge__input) { flex: 1 1 140px; width: auto; }
.cfge :deep(.cfge__secretbtn) { flex: none; font-size: var(--fs-sm); padding: 3px 10px; white-space: nowrap; }
.cfge :deep(.cfge__switchline) { align-items: center; display: flex; min-height: 32px; }
/* Two-option segmented select: both choices visible, the active one inked. */
.cfge :deep(.cfge__seg) {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius-control);
  display: inline-flex;
  gap: 2px;
  padding: 2px;
}
.cfge :deep(.cfge__seg-opt) {
  background: transparent;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  cursor: pointer;
  font: inherit;
  font-size: var(--fs-sm);
  min-height: 26px;
  padding: 2px 12px;
  transition: background var(--dur-fast) var(--ease-out), color var(--dur-fast) var(--ease-out);
}
.cfge :deep(.cfge__seg-opt:hover) { color: var(--text); }
.cfge :deep(.cfge__seg-opt.is-on) { background: var(--bg-elevated); color: var(--text); font-weight: 600; }
.cfge :deep(.cfge__seg-opt:focus-visible) { box-shadow: var(--focus-ring); outline: 0; }
@media (prefers-reduced-motion: reduce) {
  .cfge :deep(.cfge__seg-opt) { transition: none; }
}
.cfge :deep(.cfge__desc) { color: var(--text-dim); font-size: var(--fs-xs); line-height: 1.45; padding: 0 10px; }
.cfge :deep(.cfge__field-error) { color: var(--danger); font-size: var(--fs-xs); margin: 0; padding: 0 10px; }
.cfge :deep(.cfge__sr-only) { height: 1px; margin: -1px; overflow: hidden; padding: 0; position: absolute; width: 1px; clip: rect(0, 0, 0, 0); white-space: nowrap; }
.cfge__hint { color: var(--text-dim); font-size: var(--fs-sm); margin: var(--sp-3) 0 0; }
.cfge__advanced { border-top: 1px solid var(--border); padding-top: var(--sp-2); }
.cfge__advanced > summary { color: var(--text-muted); cursor: pointer; font-size: var(--fs-sm); padding: var(--sp-1) 0; }
.cfge__advanced-preview { color: var(--text-dim); font-size: var(--fs-xs); margin-inline-start: var(--sp-1); }
.cfge__advanced-body { display: block; padding-top: var(--sp-3); }
.cfge__load-error { align-items: center; color: var(--danger); display: flex; flex-wrap: wrap; font-size: var(--fs-sm); gap: var(--sp-2); justify-content: space-between; }
.cfge__skeleton { display: grid; gap: var(--sp-4); }
.cfge__skeleton-row { column-gap: var(--sp-2); display: grid; grid-template-columns: 148px minmax(0, 1fr); }
.cfge__skeleton-label, .cfge__skeleton-value { background: var(--bg-surface-2); border-radius: var(--radius-sm); display: block; height: 14px; margin-top: 9px; }
.cfge__skeleton-label { width: 72%; }
.cfge__skeleton-value { width: 56%; }
.cfge__transcript { border-top: 1px solid var(--border); display: grid; gap: var(--sp-1); margin-top: 28px; padding-top: var(--sp-3); }
.cfge__transcript-row { align-items: baseline; color: var(--text-muted); display: flex; font-size: var(--fs-sm); gap: var(--sp-2); }
.cfge__transcript-row > svg { flex: none; transform: translateY(2px); }
.cfge__transcript-row.is-ok > svg { color: var(--ok); }
.cfge__transcript-row.is-fail { color: var(--danger); }
.cfge__transcript-row.is-fail > svg { color: var(--danger); }
.cfge__transcript-row.is-info > svg { color: var(--text-dim); }
.cfge__transcript-text { min-width: 0; overflow-wrap: anywhere; }
.cfge__transcript-latency { color: var(--text-dim); font-variant-numeric: tabular-nums; margin-left: auto; white-space: nowrap; }
.cfge__transcript-actions { display: flex; justify-content: flex-end; padding-top: var(--sp-1); }

@media (max-width: 480px) {
  /* The rail collapses to label-over-control before it can crush inputs. */
  .cfge__row, .cfge__skeleton-row { grid-template-columns: minmax(0, 1fr); row-gap: var(--sp-1); }
  .cfge :deep(.cfge__rail) { padding-top: 0; }
}
</style>
