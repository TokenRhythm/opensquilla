import { computed, ref } from 'vue'
import i18n from '@/i18n'
import { useRpcStore } from '@/stores/rpc'
import { errorMessage } from '@/composables/channels/shared'
import { useSetupChannelsForm } from '@/composables/setup/useSetupChannelsForm'
import {
  REDACTED_SENTINEL,
  parseUpsertOutcome,
  probeChannelEntry,
  upsertChannelEntry,
  type ChannelRpcCaller,
  type ChannelSaveOutcome,
} from '@/composables/setup/channelRpc'

// In-place channel configuration editor for the /channels workspace. Owns
// exactly four RPC edges — onboarding.catalog, channels.get,
// onboarding.channel.probe, onboarding.channel.upsert — and reuses the pure
// useSetupChannelsForm draft state. It deliberately does NOT import
// useSetupCatalog: that composable is a function-scoped single-consumer state
// tree for the Settings dialog, and instantiating it here would fork an
// unshared copy of that whole tree.

export interface ChannelEditorFieldSpec {
  name: string
  label: string
  type?: string
  required?: boolean
  default?: string | boolean | number
  placeholder?: string
  description?: string
  secret?: boolean
  choices?: string[]
  group?: string
  advanced?: boolean
  showWhen?: Record<string, string>
  [key: string]: unknown
}

export interface ChannelSetupAid {
  id: string
  kind: 'copy' | 'link' | 'note' | string
  content?: string
}

export interface ChannelEditorSpec {
  type: string
  label: string
  description?: string
  transport?: string
  docsHint?: string
  fields?: ChannelEditorFieldSpec[]
  whatYouNeed?: string[]
  setupAids?: ChannelSetupAid[]
}

export interface ProbeTranscriptRow {
  id: string
  tone: 'ok' | 'fail' | 'info'
  text: string
}

export interface DraftProbeState {
  phase: 'idle' | 'running' | 'done'
  ok?: boolean
  /** The verdict came from a Save attempt (offers "Save anyway"). */
  fromSave?: boolean
  rows: ProbeTranscriptRow[]
  latencyMs?: number | null
}

export type ChannelEditorPhase = 'idle' | 'loading' | 'active' | 'error'

export interface ChannelSaveResult {
  status: 'invalid' | 'probe-failed' | 'saved' | 'error'
  outcome?: ChannelSaveOutcome
  message?: string
}

// ---------------------------------------------------------------------------
// onboarding.catalog — module-scope cache. Field specs, secret markers,
// defaults, and grouping all come only from this RPC. Fetched on first need,
// kept for the session, refreshed in the background on view mount so opening
// the Configuration tab never blocks on a catalog round-trip twice.
// ---------------------------------------------------------------------------

let catalogCache: ChannelEditorSpec[] | null = null
let catalogInflight: Promise<ChannelEditorSpec[]> | null = null

export function ensureChannelCatalog(
  rpc: ChannelRpcCaller,
  opts: { refresh?: boolean } = {},
): Promise<ChannelEditorSpec[]> {
  if (!opts.refresh && catalogCache) return Promise.resolve(catalogCache)
  if (!catalogInflight) {
    catalogInflight = rpc
      .call<{ channels?: ChannelEditorSpec[] }>('onboarding.catalog')
      .then(res => {
        catalogCache = res?.channels || []
        return catalogCache
      })
      .finally(() => {
        catalogInflight = null
      })
  }
  return catalogInflight
}

/** Test hook: drop the module-scope catalog cache between cases. */
export function _resetChannelCatalogForTests(): void {
  catalogCache = null
  catalogInflight = null
}

/**
 * First free name for a new channel of `type`: the bare type, then type-2,
 * type-3, … . Compared case-insensitively against ALL existing channel names
 * (any type — the name is the global identity key, and upsert by a colliding
 * name would overwrite that entry).
 */
export function suggestChannelName(type: string, existingNames: string[]): string {
  const taken = new Set(existingNames.map(name => name.trim().toLowerCase()).filter(Boolean))
  const base = type.trim() || 'channel'
  if (!taken.has(base.toLowerCase())) return base
  for (let n = 2; n < 100; n += 1) {
    const candidate = `${base}-${n}`
    if (!taken.has(candidate.toLowerCase())) return candidate
  }
  return `${base}-${existingNames.length + 1}`
}

export function useChannelEditor() {
  const rpc = useRpcStore()
  const form = useSetupChannelsForm()

  const catalog = ref<ChannelEditorSpec[]>(catalogCache || [])
  const catalogPending = ref(false)
  const catalogError = ref('')
  const phase = ref<ChannelEditorPhase>('idle')
  const loadedName = ref('')
  const loadError = ref('')
  const entryType = ref('')
  const loadedEntry = ref<Record<string, unknown> | null>(null)
  const loadedSecretFields = ref<string[]>([])
  // Per-field baseline (string form) captured at seed time — drives the
  // per-field "edited" rail ticks and the named-groups summary in the bar.
  const baselineValues = ref<Record<string, string>>({})
  const probe = ref<DraftProbeState>({ phase: 'idle', rows: [] })
  const fieldErrors = ref<Record<string, string>>({})
  const saving = ref(false)

  const spec = computed<ChannelEditorSpec | null>(
    () => catalog.value.find(s => s.type === entryType.value) || null,
  )
  const specFields = computed<ChannelEditorFieldSpec[]>(() => spec.value?.fields ?? [])
  const canEdit = computed(() => phase.value === 'active' && spec.value !== null)

  const panel = form.createPanel(specFields)

  async function ensureCatalog(refresh = false): Promise<ChannelEditorSpec[]> {
    // Cold-load guard: panels can mount before the WS handshake completes;
    // waiting here turns a hard "not connected" error into a short defer.
    await rpc.waitForConnection()
    const channels = await ensureChannelCatalog(rpc, { refresh })
    catalog.value = channels
    return channels
  }

  /** Background catalog refresh (view mount); a stale copy keeps rendering. */
  async function refreshCatalog(): Promise<void> {
    try {
      await ensureCatalog(true)
    } catch {
      // Keep the cached copy; the next open() surfaces a real failure.
    }
  }

  /**
   * Foreground catalog load for the type gallery: tracks pending/error so an
   * empty gallery can distinguish "loading" from "failed, offer retry".
   */
  async function loadCatalog(): Promise<void> {
    if (catalogPending.value) return
    catalogPending.value = true
    catalogError.value = ''
    try {
      await ensureCatalog()
    } catch (err) {
      catalogError.value = errorMessage(err)
    } finally {
      catalogPending.value = false
    }
  }

  function captureBaseline(fields: ChannelEditorFieldSpec[], entry: Record<string, unknown>): void {
    const baseline: Record<string, string> = {}
    for (const field of fields) {
      if (field.secret === true) continue
      baseline[field.name] = String(entry[field.name] ?? field.default ?? '')
    }
    baselineValues.value = baseline
  }

  function resetProbe(): void {
    if (probe.value.phase !== 'idle') probe.value = { phase: 'idle', rows: [] }
  }

  /**
   * channels.get → seed the form in edit mode. `quiet` reseeds in place
   * (post-save baseline reset) without flashing the loading skeleton.
   */
  async function open(name: string, opts: { quiet?: boolean } = {}): Promise<void> {
    if (!opts.quiet) phase.value = 'loading'
    loadError.value = ''
    fieldErrors.value = {}
    resetProbe()
    try {
      await rpc.waitForConnection()
      const [channels, res] = await Promise.all([
        ensureCatalog(),
        rpc.call<{ entry?: Record<string, unknown>; secretFields?: string[] }>('channels.get', {
          name,
        }),
      ])
      const entry = res?.entry
      if (!entry) throw new Error(i18n.global.t('console.channels.editor.entryMissing', { name }))
      entryType.value = String(entry.type || '')
      loadedEntry.value = entry
      loadedSecretFields.value = res?.secretFields || []
      const found = channels.find(s => s.type === entryType.value) || null
      if (found) {
        form.initFromEntry(found, entry, loadedSecretFields.value)
        captureBaseline(found.fields ?? [], entry)
      }
      loadedName.value = name
      phase.value = 'active'
    } catch (err) {
      phase.value = 'error'
      loadError.value = errorMessage(err)
    }
  }

  /** Restore the last-loaded server entry in place (Discard / Esc-cancel). */
  function discard(): void {
    const s = spec.value
    if (!s || !loadedEntry.value) return
    form.initFromEntry(s, loadedEntry.value, loadedSecretFields.value)
    fieldErrors.value = {}
    resetProbe()
  }

  /**
   * Seed a fresh compose draft for a picked platform type: spec defaults,
   * everything editable (secrets as plain password inputs — nothing stored
   * yet). Drafts are deliberately not persisted; re-running this after a
   * refresh yields the same empty draft. When the caller knows the existing
   * channel names it passes them so the required name field seeds with a
   * unique suggestion instead of blocking Save while blank; with no list
   * (status still loading) the name stays blank rather than risking a
   * collision-turned-overwrite on upsert.
   */
  async function startCompose(
    type: string,
    opts: { existingNames?: string[] } = {},
  ): Promise<void> {
    phase.value = 'loading'
    loadError.value = ''
    fieldErrors.value = {}
    resetProbe()
    loadedName.value = ''
    loadedEntry.value = null
    loadedSecretFields.value = []
    try {
      const channels = await ensureCatalog()
      entryType.value = type
      const found = channels.find(s => s.type === type) || null
      form.selectChannelType(type)
      const seed = opts.existingNames
        ? { name: suggestChannelName(type, opts.existingNames) }
        : undefined
      form.resetForSpec(found, seed)
      captureBaseline(found?.fields ?? [], seed ?? {})
      phase.value = 'active'
    } catch (err) {
      phase.value = 'error'
      loadError.value = errorMessage(err)
    }
  }

  /** Full clear when the selection changes or the aside closes. */
  function reset(): void {
    phase.value = 'idle'
    loadedName.value = ''
    loadError.value = ''
    entryType.value = ''
    loadedEntry.value = null
    loadedSecretFields.value = []
    baselineValues.value = {}
    fieldErrors.value = {}
    probe.value = { phase: 'idle', rows: [] }
    form.resetForSpec(null)
  }

  function updateField(name: string, value: unknown): void {
    form.updateField(name, value)
    // A probe verdict describes the draft it ran against; any edit voids it.
    resetProbe()
    if (fieldErrors.value[name]) {
      const next = { ...fieldErrors.value }
      delete next[name]
      fieldErrors.value = next
    }
  }

  function replaceSecret(name: string): void {
    form.replaceSecret(name)
    resetProbe()
  }

  function cancelSecretReplace(name: string): void {
    form.cancelSecretReplace(name)
    resetProbe()
  }

  // Spec-ordered names of fields whose draft value differs from the loaded
  // entry. Untouched stored secrets can never appear here (they contribute
  // keep-current), so the list mirrors what an upsert would actually change.
  const editedFields = computed<string[]>(() => {
    const view = panel.value
    const changed = new Set<string>()
    for (const row of view.channelFields) {
      if (row.field.name === 'name') continue
      if (String(row.value ?? '') !== (baselineValues.value[row.field.name] ?? '')) {
        changed.add(row.field.name)
      }
    }
    for (const row of view.secretRows) {
      if ((row.replacing || !row.hasStored) && row.value !== '') changed.add(row.field.name)
    }
    return specFields.value.map(f => f.name).filter(name => changed.has(name))
  })

  // Saved-entry keys outside the spec (hand-edited extras) — shown read-only;
  // payload() carries them through untouched. With no spec at all (unknown
  // type) this becomes the whole read-only fallback rendering.
  const extraRows = computed(() => {
    const entry = loadedEntry.value
    if (!entry) return []
    const specNames = new Set(specFields.value.map(f => f.name))
    return Object.entries(entry)
      .filter(([key]) => key !== 'type' && !specNames.has(key))
      .map(([key, value]) => ({
        key,
        secret: value === REDACTED_SENTINEL || loadedSecretFields.value.includes(key),
        value: Array.isArray(value) ? value.join(', ') || '—' : String(value ?? '—'),
      }))
  })

  /**
   * Probe the CURRENT DRAFT via onboarding.channel.probe {entry} — the
   * merge-aware server-side validation (blank secrets resolve against the
   * stored entry). Deliberately different from the read-mode action-row Test,
   * which probes the SAVED channel live via channels.probe {name}.
   */
  async function testDraft(opts: { fromSave?: boolean } = {}): Promise<boolean> {
    if (probe.value.phase === 'running') return false
    probe.value = { phase: 'running', rows: [] }
    const started = Date.now()
    try {
      const res = await probeChannelEntry(rpc, form.payload())
      const rows: ProbeTranscriptRow[] = [
        { id: 'validate', tone: 'ok', text: i18n.global.t('console.channels.editor.probeValid') },
        ...(res?.warnings || []).map((text, index) => ({
          id: `warning-${index}`,
          tone: 'info' as const,
          text,
        })),
      ]
      probe.value = {
        phase: 'done',
        ok: true,
        fromSave: opts.fromSave,
        rows,
        latencyMs: Date.now() - started,
      }
      return true
    } catch (err) {
      probe.value = {
        phase: 'done',
        ok: false,
        fromSave: opts.fromSave,
        rows: [{ id: 'validate', tone: 'fail', text: errorMessage(err) }],
        latencyMs: Date.now() - started,
      }
      return false
    }
  }

  async function commit(entry: Record<string, unknown>): Promise<ChannelSaveResult> {
    try {
      const res = await upsertChannelEntry(rpc, entry)
      const outcome = parseUpsertOutcome(entry.name, res)
      if (form.isEditing.value) {
        // Reseed from the server so a just-replaced secret flips back to its
        // masked row, the plaintext leaves memory, and the baseline resets.
        await open(outcome.name || loadedName.value, { quiet: true })
      } else {
        // Compose: the caller dismisses the takeover and selects the new
        // channel — no reseed, just drop the stale probe verdict.
        resetProbe()
      }
      return { status: 'saved', outcome }
    } catch (err) {
      return { status: 'error', message: errorMessage(err) }
    }
  }

  /** Save = probe → upsert. A probe failure blocks with inline rows; the
   *  caller offers "Save anyway" (saveAnyway) — probing is advisory. */
  async function save(): Promise<ChannelSaveResult> {
    const missing = form.missingRequiredFields()
    if (missing.length > 0) {
      fieldErrors.value = Object.fromEntries(
        missing.map(name => [name, i18n.global.t('setup.channels.fieldRequired')]),
      )
      return { status: 'invalid' }
    }
    fieldErrors.value = {}
    saving.value = true
    try {
      if (!(await testDraft({ fromSave: true }))) return { status: 'probe-failed' }
      return await commit(form.payload())
    } finally {
      saving.value = false
    }
  }

  async function saveAnyway(): Promise<ChannelSaveResult> {
    saving.value = true
    try {
      return await commit(form.payload())
    } finally {
      saving.value = false
    }
  }

  return {
    form,
    panel,
    spec,
    specFields,
    catalog,
    catalogPending,
    catalogError,
    phase,
    canEdit,
    loadedName,
    loadedEntry,
    loadError,
    entryType,
    extraRows,
    editedFields,
    fieldErrors,
    probe,
    saving,
    open,
    startCompose,
    refreshCatalog,
    loadCatalog,
    discard,
    reset,
    updateField,
    replaceSecret,
    cancelSecretReplace,
    testDraft,
    save,
    saveAnyway,
  }
}

export type ChannelEditorApi = ReturnType<typeof useChannelEditor>
