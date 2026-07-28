export const DESKTOP_EXIT_PHASES = [
  'running',
  'deferred',
  'draining',
  'committed',
] as const

export type DesktopExitPhase = typeof DESKTOP_EXIT_PHASES[number]

export const DESKTOP_MAIN_WINDOW_CLOSE_BEHAVIORS = [
  'background',
  'quit',
  'ask',
] as const

export type DesktopMainWindowCloseBehavior =
  typeof DESKTOP_MAIN_WINDOW_CLOSE_BEHAVIORS[number]

export const DESKTOP_WORKBENCH_PREVIEW_MODES = ['full', 'offline'] as const

export type DesktopWorkbenchPreviewMode =
  typeof DESKTOP_WORKBENCH_PREVIEW_MODES[number]

export interface DesktopPreferencesFile {
  schema_version: 2
  main_window_close_behavior: DesktopMainWindowCloseBehavior
  background_close_notice_shown: boolean
  workbench_preview_mode: DesktopWorkbenchPreviewMode
  workbench_preview_notice_shown: boolean
}

export interface DesktopPreferencesNormalization {
  value: DesktopPreferencesFile
  /**
   * False means the source schema is newer than this runtime understands.
   * Known fields remain usable, but callers must preserve the source bytes.
   */
  writable: boolean
}

export type DesktopMainWindowCloseAction =
  | 'allow'
  | 'focus-onboarding'
  | 'hide'
  | 'quit'
  | 'ask'

export interface DesktopMainWindowCloseContext {
  platform: NodeJS.Platform
  exitPhase: DesktopExitPhase
  systemSessionEnding: boolean
  onboardingOpen: boolean
  behavior: DesktopMainWindowCloseBehavior
  windowsTrayReady: boolean
}

const CLOSE_BEHAVIOR_SET = new Set<string>(DESKTOP_MAIN_WINDOW_CLOSE_BEHAVIORS)
const PREVIEW_MODE_SET = new Set<string>(DESKTOP_WORKBENCH_PREVIEW_MODES)
const PREFERENCES_KEYS = new Set<string>([
  'schema_version',
  'main_window_close_behavior',
  'background_close_notice_shown',
  'workbench_preview_mode',
  'workbench_preview_notice_shown',
])

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function isCloseBehavior(value: unknown): value is DesktopMainWindowCloseBehavior {
  return typeof value === 'string' && CLOSE_BEHAVIOR_SET.has(value)
}

function isPreviewMode(value: unknown): value is DesktopWorkbenchPreviewMode {
  return typeof value === 'string' && PREVIEW_MODE_SET.has(value)
}

export function defaultDesktopMainWindowCloseBehavior(
  platform: NodeJS.Platform,
): DesktopMainWindowCloseBehavior {
  return platform === 'darwin' || platform === 'win32' ? 'background' : 'quit'
}

export function defaultDesktopPreferences(platform: NodeJS.Platform): DesktopPreferencesFile {
  return {
    schema_version: 2,
    main_window_close_behavior: defaultDesktopMainWindowCloseBehavior(platform),
    background_close_notice_shown: false,
    workbench_preview_mode: 'full',
    workbench_preview_notice_shown: false,
  }
}

/**
 * Normalize untrusted JSON without coercing values. A future schema may reuse
 * fields whose meaning is already known, but must never be overwritten by this
 * runtime; callers use `writable` to preserve its original bytes.
 */
export function normalizeDesktopPreferences(
  raw: unknown,
  platform: NodeJS.Platform,
): DesktopPreferencesNormalization {
  const defaults = defaultDesktopPreferences(platform)
  const payload = record(raw)
  if (!payload) return { value: defaults, writable: true }

  const schemaVersion = payload.schema_version
  const legacySchema = schemaVersion === 1
  const currentSchema = schemaVersion === 2
  const futureSchema = Number.isSafeInteger(schemaVersion) && Number(schemaVersion) > 2
  if (!legacySchema && !currentSchema && !futureSchema) {
    return { value: defaults, writable: true }
  }

  return {
    value: {
      schema_version: 2,
      main_window_close_behavior: isCloseBehavior(payload.main_window_close_behavior)
        ? payload.main_window_close_behavior
        : defaults.main_window_close_behavior,
      background_close_notice_shown:
        typeof payload.background_close_notice_shown === 'boolean'
          ? payload.background_close_notice_shown
          : false,
      workbench_preview_mode:
        currentSchema || futureSchema
          ? isPreviewMode(payload.workbench_preview_mode)
            ? payload.workbench_preview_mode
            : defaults.workbench_preview_mode
          : defaults.workbench_preview_mode,
      workbench_preview_notice_shown:
        (currentSchema || futureSchema)
        && typeof payload.workbench_preview_notice_shown === 'boolean'
          ? payload.workbench_preview_notice_shown
          : false,
    },
    writable: legacySchema || currentSchema,
  }
}

/**
 * Emit one canonical schema-v2 document. Serialization is deliberately strict:
 * callers must normalize untrusted input first, and accidental future/unknown
 * fields must not be silently downgraded.
 */
export function serializeDesktopPreferences(value: DesktopPreferencesFile): string {
  const payload = record(value)
  if (
    !payload
    || payload.schema_version !== 2
    || !isCloseBehavior(payload.main_window_close_behavior)
    || typeof payload.background_close_notice_shown !== 'boolean'
    || !isPreviewMode(payload.workbench_preview_mode)
    || typeof payload.workbench_preview_notice_shown !== 'boolean'
    || Object.keys(payload).some((key) => !PREFERENCES_KEYS.has(key))
  ) {
    throw new Error('Desktop preferences are not a valid schema-v2 document.')
  }
  const canonical: DesktopPreferencesFile = {
    schema_version: 2,
    main_window_close_behavior: payload.main_window_close_behavior,
    background_close_notice_shown: payload.background_close_notice_shown,
    workbench_preview_mode: payload.workbench_preview_mode,
    workbench_preview_notice_shown: payload.workbench_preview_notice_shown,
  }
  return `${JSON.stringify(canonical, null, 2)}\n`
}

export function mainWindowCloseAction(
  context: DesktopMainWindowCloseContext,
): DesktopMainWindowCloseAction {
  if (context.systemSessionEnding || context.exitPhase === 'committed') return 'allow'
  const backgroundSupported = context.platform === 'darwin'
    || (context.platform === 'win32' && context.windowsTrayReady)
  if (!backgroundSupported) return 'quit'
  if (context.onboardingOpen) return 'focus-onboarding'
  // A deferred or draining exit still owns live renderer/runtime state. A
  // repeated close must not start another quit or reopen an Ask dialog while
  // that single-flight operation is settling; keep the recoverable window
  // hidden until the exit either commits or returns to running.
  if (context.exitPhase !== 'running') return 'hide'
  if (context.behavior === 'quit') return 'quit'
  if (context.behavior === 'ask') return 'ask'
  return 'hide'
}

export function canRevealDesktopApp(phase: DesktopExitPhase): boolean {
  return phase === 'running'
}
