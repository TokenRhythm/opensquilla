import assert from 'node:assert/strict'

import {
  canRevealDesktopApp,
  defaultDesktopMainWindowCloseBehavior,
  defaultDesktopPreferences,
  mainWindowCloseAction,
  normalizeDesktopPreferences,
  serializeDesktopPreferences,
} from '../dist/desktop-window-lifecycle.js'

assert.equal(defaultDesktopMainWindowCloseBehavior('darwin'), 'background')
assert.equal(defaultDesktopMainWindowCloseBehavior('win32'), 'background')
assert.equal(defaultDesktopMainWindowCloseBehavior('linux'), 'quit')
assert.equal(defaultDesktopMainWindowCloseBehavior('freebsd'), 'quit')

assert.deepEqual(defaultDesktopPreferences('darwin'), {
  schema_version: 2,
  main_window_close_behavior: 'background',
  background_close_notice_shown: false,
  workbench_preview_mode: 'full',
  workbench_preview_notice_shown: false,
})
assert.deepEqual(defaultDesktopPreferences('linux'), {
  schema_version: 2,
  main_window_close_behavior: 'quit',
  background_close_notice_shown: false,
  workbench_preview_mode: 'full',
  workbench_preview_notice_shown: false,
})

for (const invalid of [
  null,
  undefined,
  [],
  'preferences',
  1,
  {},
  { schema_version: 0 },
  { schema_version: '1' },
  { schema_version: 1.5 },
  { schema_version: Number.MAX_SAFE_INTEGER + 1 },
]) {
  assert.deepEqual(
    normalizeDesktopPreferences(invalid, 'win32'),
    {
      value: {
        schema_version: 2,
        main_window_close_behavior: 'background',
        background_close_notice_shown: false,
        workbench_preview_mode: 'full',
        workbench_preview_notice_shown: false,
      },
      writable: true,
    },
    'invalid or unsupported input must use writable platform defaults without coercion',
  )
}

for (const behavior of ['background', 'quit', 'ask']) {
  for (const noticeShown of [false, true]) {
    assert.deepEqual(
      normalizeDesktopPreferences({
        schema_version: 1,
        main_window_close_behavior: behavior,
        background_close_notice_shown: noticeShown,
        ignored_legacy_field: 'does not enter the normalized document',
      }, 'linux'),
      {
        value: {
          schema_version: 2,
          main_window_close_behavior: behavior,
          background_close_notice_shown: noticeShown,
          workbench_preview_mode: 'full',
          workbench_preview_notice_shown: false,
        },
        writable: true,
      },
    )
  }
}

assert.deepEqual(
  normalizeDesktopPreferences({
    schema_version: 1,
    main_window_close_behavior: 'BACKGROUND',
    background_close_notice_shown: 1,
  }, 'linux'),
  {
    value: {
      schema_version: 2,
      main_window_close_behavior: 'quit',
      background_close_notice_shown: false,
      workbench_preview_mode: 'full',
      workbench_preview_notice_shown: false,
    },
    writable: true,
  },
  'current-schema values are validated without string or boolean coercion',
)

assert.deepEqual(
  normalizeDesktopPreferences({
    schema_version: 2,
    main_window_close_behavior: 'ask',
    background_close_notice_shown: true,
    workbench_preview_mode: 'offline',
    workbench_preview_notice_shown: true,
  }, 'win32'),
  {
    value: {
      schema_version: 2,
      main_window_close_behavior: 'ask',
      background_close_notice_shown: true,
      workbench_preview_mode: 'offline',
      workbench_preview_notice_shown: true,
    },
    writable: true,
  },
  'schema-v2 preview preferences are normalized without coercion',
)
assert.deepEqual(
  normalizeDesktopPreferences({
    schema_version: 3,
    main_window_close_behavior: 'ask',
    background_close_notice_shown: true,
    workbench_preview_mode: 'offline',
    workbench_preview_notice_shown: true,
    future_field: { preservedByCaller: true },
  }, 'win32'),
  {
    value: {
      schema_version: 2,
      main_window_close_behavior: 'ask',
      background_close_notice_shown: true,
      workbench_preview_mode: 'offline',
      workbench_preview_notice_shown: true,
    },
    writable: false,
  },
  'known future-schema fields may be used but the source must remain read-only',
)
assert.deepEqual(
  normalizeDesktopPreferences({
    schema_version: 999,
    main_window_close_behavior: 'future-choice',
    background_close_notice_shown: 'yes',
    workbench_preview_mode: 'browser-like',
    workbench_preview_notice_shown: 1,
  }, 'linux'),
  {
    value: {
      schema_version: 2,
      main_window_close_behavior: 'quit',
      background_close_notice_shown: false,
      workbench_preview_mode: 'full',
      workbench_preview_notice_shown: false,
    },
    writable: false,
  },
  'invalid fields in a future schema fall back without granting write authority',
)

const preferences = {
  schema_version: 2,
  main_window_close_behavior: 'ask',
  background_close_notice_shown: true,
  workbench_preview_mode: 'offline',
  workbench_preview_notice_shown: true,
}
const serialized = serializeDesktopPreferences(preferences)
assert.equal(
  serialized,
  '{\n'
    + '  "schema_version": 2,\n'
    + '  "main_window_close_behavior": "ask",\n'
    + '  "background_close_notice_shown": true,\n'
    + '  "workbench_preview_mode": "offline",\n'
    + '  "workbench_preview_notice_shown": true\n'
    + '}\n',
)
assert.deepEqual(
  normalizeDesktopPreferences(JSON.parse(serialized), 'linux'),
  { value: preferences, writable: true },
)

for (const invalid of [
  null,
  {},
  { ...preferences, schema_version: 1 },
  { ...preferences, main_window_close_behavior: 'close' },
  { ...preferences, background_close_notice_shown: 1 },
  { ...preferences, workbench_preview_mode: 'browser-like' },
  { ...preferences, workbench_preview_notice_shown: 1 },
  { ...preferences, unexpected: true },
]) {
  assert.throws(
    () => serializeDesktopPreferences(invalid),
    /not a valid schema-v2 document/,
    'serialization must reject malformed, future, and non-canonical documents',
  )
}

function closeAction(overrides = {}) {
  return mainWindowCloseAction({
    platform: 'darwin',
    exitPhase: 'running',
    systemSessionEnding: false,
    onboardingOpen: false,
    behavior: 'background',
    windowsTrayReady: false,
    ...overrides,
  })
}

for (const phase of ['running', 'deferred', 'draining', 'committed']) {
  assert.equal(
    closeAction({
      exitPhase: phase,
      systemSessionEnding: true,
      onboardingOpen: true,
      behavior: 'ask',
    }),
    'allow',
    'an ending OS session always owns window closure',
  )
}
for (const platform of ['darwin', 'win32', 'linux']) {
  assert.equal(
    closeAction({
      platform,
      exitPhase: 'committed',
      onboardingOpen: true,
      behavior: 'background',
      windowsTrayReady: true,
    }),
    'allow',
    'a committed application exit must never be converted into backgrounding',
  )
}

for (const behavior of ['background', 'quit', 'ask']) {
  assert.equal(
    closeAction({ onboardingOpen: true, behavior, windowsTrayReady: true }),
    'focus-onboarding',
    'the modal must remain reachable while the application is running',
  )
}
assert.equal(
  closeAction({
    platform: 'win32',
    onboardingOpen: true,
    behavior: 'ask',
    windowsTrayReady: false,
  }),
  'quit',
  'an onboarding modal cannot justify backgrounding without a recovery surface',
)
assert.equal(
  closeAction({
    platform: 'linux',
    onboardingOpen: true,
    behavior: 'background',
    windowsTrayReady: true,
  }),
  'quit',
  'unsupported platforms do not acquire background behavior from a modal',
)

assert.equal(closeAction({ platform: 'darwin', windowsTrayReady: false }), 'hide')
assert.equal(closeAction({ platform: 'darwin', windowsTrayReady: true }), 'hide')
assert.equal(closeAction({ platform: 'win32', windowsTrayReady: true }), 'hide')
assert.equal(
  closeAction({ platform: 'win32', windowsTrayReady: false }),
  'quit',
  'Windows must not background without a working tray recovery surface',
)
assert.equal(
  closeAction({ platform: 'linux', windowsTrayReady: true }),
  'quit',
  'unsupported platforms must not become invisible even if a tray-like flag is supplied',
)

assert.equal(closeAction({ platform: 'darwin', behavior: 'quit' }), 'quit')
assert.equal(closeAction({ platform: 'darwin', behavior: 'ask' }), 'ask')
assert.equal(
  closeAction({ platform: 'win32', behavior: 'quit', windowsTrayReady: true }),
  'quit',
)
assert.equal(
  closeAction({ platform: 'win32', behavior: 'ask', windowsTrayReady: true }),
  'ask',
)
assert.equal(
  closeAction({ platform: 'win32', behavior: 'ask', windowsTrayReady: false }),
  'quit',
)
assert.equal(
  closeAction({ platform: 'linux', behavior: 'ask', windowsTrayReady: true }),
  'quit',
)

for (const phase of ['deferred', 'draining']) {
  assert.equal(
    closeAction({ exitPhase: phase, platform: 'darwin', behavior: 'background' }),
    'hide',
    'a deferred or draining quit is not committed and must preserve the live window',
  )
  assert.equal(closeAction({ exitPhase: phase, behavior: 'quit' }), 'hide')
  assert.equal(closeAction({ exitPhase: phase, behavior: 'ask' }), 'hide')
}

for (const platform of ['darwin', 'win32', 'linux']) {
  for (const exitPhase of ['running', 'deferred', 'draining', 'committed']) {
    for (const systemSessionEnding of [false, true]) {
      for (const onboardingOpen of [false, true]) {
        for (const behavior of ['background', 'quit', 'ask']) {
          for (const windowsTrayReady of [false, true]) {
            const backgroundSupported = platform === 'darwin'
              || (platform === 'win32' && windowsTrayReady)
            const expected = systemSessionEnding || exitPhase === 'committed'
              ? 'allow'
              : !backgroundSupported
                ? 'quit'
                : onboardingOpen
                  ? 'focus-onboarding'
                  : exitPhase !== 'running'
                    ? 'hide'
                  : behavior === 'background'
                    ? 'hide'
                    : behavior
            assert.equal(
              closeAction({
                platform,
                exitPhase,
                systemSessionEnding,
                onboardingOpen,
                behavior,
                windowsTrayReady,
              }),
              expected,
              JSON.stringify({
                platform,
                exitPhase,
                systemSessionEnding,
                onboardingOpen,
                behavior,
                windowsTrayReady,
              }),
            )
          }
        }
      }
    }
  }
}

assert.equal(canRevealDesktopApp('running'), true)
assert.equal(canRevealDesktopApp('deferred'), false)
assert.equal(canRevealDesktopApp('draining'), false)
assert.equal(canRevealDesktopApp('committed'), false)

console.log('desktop window lifecycle checks passed')
