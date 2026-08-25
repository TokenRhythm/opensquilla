import { describe, expect, it } from 'vitest'
import runModeSource from './ChatComposerRunMode.vue?raw'
import modelRoutingSource from './ChatComposerModelRouting.vue?raw'
import composerSource from './ChatComposer.vue?raw'
import viewSource from '../../views/ChatView.vue?raw'
import appearanceSource from '../settings/SettingsAppearancePanel.vue?raw'
import slashSource from '../../composables/chat/useChatSlashCommands.ts?raw'
import en from '../../locales/en.json'
import zhHans from '../../locales/zh-Hans.json'

describe('ChatComposer control hierarchy', () => {
  it('keeps legacy execution mode choices out of the composer settings panel', () => {
    expect(composerSource).not.toContain('ChatComposerSettings')
    expect(composerSource).not.toContain('chat.composer.executionMode')
    expect(composerSource).not.toContain('composer-execution-mode')
    expect(composerSource).not.toContain('setElevatedMode')
  })

  it('threads the shield run-mode control through ChatComposer and ChatView', () => {
    expect(composerSource).toContain('ChatComposerRunMode')
    expect(composerSource).toContain('<Icon name="shield"')
    expect(composerSource).toContain('chat-run-mode-btn--${runMode}')
    expect(composerSource).toContain(':run-mode="runMode"')
    expect(composerSource).toContain(':safe-setup-available="safeSetupAvailable"')
    expect(composerSource).toContain('@set-run-mode="emit(\'setRunMode\', $event)"')
    expect(composerSource).toContain('setRunMode: [mode: SandboxRunMode]')

    expect(viewSource).toContain(':run-mode="runMode"')
    expect(viewSource).toContain(':allowed-run-modes="composerAllowedRunModes"')
    expect(viewSource).toContain(':safe-setup-available="composerSafeSetupAvailable"')
    expect(viewSource).toContain('@set-run-mode="setComposerRunMode"')
    expect(viewSource).toContain('useChatRunModePreference')
    expect(viewSource).toContain('setGlobalRunMode')
    expect(viewSource).toContain('async function setComposerRunMode(mode: SandboxRunMode)')
    expect(viewSource).toContain('await setGlobalRunMode(mode)')
    expect(viewSource).toContain(':run-mode-locked="runModeLocked"')
    expect(viewSource).toContain('|| activeRunModeLock.value !== null')
    expect(composerSource).toContain(':disabled="runModeLocked"')
    expect(composerSource).toContain('chat-run-mode-lock-tip')
    expect(composerSource).not.toContain('cursor: not-allowed')
    expect(composerSource).toContain('cursor: default')
    expect(zhHans.chat.composer.runModeLocked).toBe('运行中，当前会话无法修改')
  })

  it('shares the first-time Safe setup dialog with the composer', () => {
    expect(viewSource).toContain('<SandboxSetupDialog')
    expect(viewSource).toContain(':open="composerSandboxSetupOpen"')
    expect(viewSource).toContain('@confirm="void confirmComposerSandboxSetup()"')
    expect(viewSource).toContain('@background="runComposerSandboxSetupInBackground"')
    expect(viewSource).toContain('useSandboxSetupStore')
    expect(viewSource).toContain('sandboxSetupStore.startSafeSetup()')
    expect(viewSource).not.toContain('completeComposerSafeSetup')
    expect(viewSource).toContain('composerRunModeSelectionAction')
  })

  it('offers exactly Safe and Full from the shield popover', () => {
    expect(runModeSource).toContain("value: 'safe'")
    expect(runModeSource).toContain("value: 'full'")
    expect(runModeSource).not.toContain("value: 'standard'")
    expect(runModeSource).not.toContain("value: 'trusted'")
    expect(runModeSource).not.toContain("value: 'on'")
    expect(runModeSource).not.toContain("value: 'bypass'")
    expect(zhHans.chat.composer.runModeSafeDesc)
      .toBe('在沙箱中运行，并遵循你的安全规则。')
  })

  it('offers Safe from the composer only after sandbox setup is ready', () => {
    expect(viewSource).toContain("status.state !== 'ready'")
    expect(viewSource).toContain("allowedRunModes.value.filter((mode) => mode !== 'safe')")
  })

  it('moves visual effects to Appearance settings', () => {
    expect(appearanceSource).toContain('settings.appearance.visualEffectsLabel')
    expect(appearanceSource).toContain('name="appearance_visual_effects"')
    expect(appearanceSource).toContain('@change="setRouterVisualEffectsEnabled"')
    expect(composerSource).not.toContain('setVisualEffectsEnabled')
  })

  it('keeps Coding mode command-first while exposing its active global state', () => {
    expect(composerSource).toContain('v-if="codingModeEnabled"')
    expect(composerSource).toContain('chat-coding-mode-chip')
    expect(composerSource).toContain("emit('setCodingModeEnabled', false)")
    expect(slashSource).toContain("action === 'coding.mode'")
    expect(slashSource).toContain('const enabled = !options.codingModeEnabled.value')
    expect(viewSource).toContain('codingModeEnabled,')
    expect(viewSource).toContain('codingModeSettingsBusy,')
    expect(viewSource).toContain('setCodingModeEnabled,')
    expect(viewSource).toContain('@set-coding-mode-enabled="setComposerCodingModeEnabled"')
  })

  it('completes Slash candidates without executing them from the suggestion menu', () => {
    expect(viewSource).toContain('@click="completeSlashCmd(cmd)"')
    expect(viewSource).not.toContain('@click="selectSlashCmd(cmd)"')
    expect(slashSource).toContain('function completeSlashCmd')
    expect(slashSource).toContain('function activateSlashCmd')
  })
})

describe('ChatComposer model routing contract', () => {
  it('keeps model-routing choices out of the generic composer settings panel', () => {
    expect(composerSource).not.toContain('label="Squilla Router"')
    expect(composerSource).not.toContain('label="LLM Ensemble"')
    expect(composerSource).not.toContain('routerEnabled: boolean')
    expect(composerSource).not.toContain('llmEnsembleEnabled: boolean')
  })

  it('threads the independent model-routing control through ChatComposer and ChatView', () => {
    expect(composerSource).toContain('ChatComposerModelRouting')
    expect(composerSource).toContain('<Icon name="router"')
    expect(composerSource).toContain('chat-model-routing-btn--${sessionRoutingMode}')
    expect(composerSource).toContain("'is-active': modelRoutingOpen || sessionRoutingMode !== 'off'")
    expect(composerSource).toContain(':model-routing-mode="sessionRoutingMode"')
    expect(composerSource).toContain(':busy="sessionRoutingBusy || sessionRoutingControlBlocked"')
    expect(composerSource).toContain('@set-session-routing-mode="emit(\'setSessionRoutingMode\', $event)"')
    expect(composerSource).toContain('sessionRoutingMode: ModelRoutingMode')
    expect(composerSource).toContain('setSessionRoutingMode: [mode: ModelRoutingMode]')

    expect(viewSource).toContain(':session-routing-mode="modelRoutingMode"')
    expect(viewSource).toContain(':session-routing-busy="modelRoutingSettingsBusy"')
    expect(viewSource).toContain(':session-routing-control-blocked="goalBusy"')
    expect(viewSource).toContain(':session-routing-available="sessionRoutingAvailable"')
    expect(viewSource).toContain("auth?.principal?.authState === 'authenticated'")
    expect(viewSource).toContain("rpc.supportsMethod('sessions.routing.get')")
    expect(viewSource).toContain("rpc.supportsMethod('sessions.routing.set')")
    expect(composerSource).toContain('v-if="sessionRoutingAvailable"')
    expect(viewSource).toContain('@set-session-routing-mode="setComposerSessionRoutingMode"')
    expect(viewSource).toContain('async function setComposerSessionRoutingMode(mode: ModelRoutingMode)')
    expect(viewSource).toContain('if (goalBusy.value) return')
    expect(viewSource).toContain('await chatSessionRouting.setMode(mode)')
    expect(viewSource).toContain('useChatSessionRouting')
    expect(viewSource).toContain('isDraft: isDraftSurface')
    expect(viewSource).toContain("return pendingSessionIntent.value === 'new_chat'")
    expect(viewSource).toContain('if (!isProvisionalDraftSession()) return')

    const setterStart = viewSource.indexOf(
      'async function setComposerSessionRoutingMode(mode: ModelRoutingMode)',
    )
    const setterEnd = viewSource.indexOf(
      '\nasync function setComposerCodingModeEnabled',
      setterStart,
    )
    const setterSource = viewSource.slice(setterStart, setterEnd)
    expect(setterStart).toBeGreaterThanOrEqual(0)
    expect(setterEnd).toBeGreaterThan(setterStart)
    expect(setterSource).not.toContain('scheduleHistorySync()')
  })

  it('blocks ordinary sends while this chat route is changing', () => {
    const start = viewSource.indexOf('async function onComposerSend()')
    const end = viewSource.indexOf('\nsendCurrentInput = onComposerSend', start)
    const sendSource = viewSource.slice(start, end)
    const routingBusyGate = sendSource.indexOf(
      'if (modelRoutingSettingsBusy.value || planModeBusy.value) return',
    )
    const ordinarySend = sendSource.indexOf('onSend()')

    expect(start).toBeGreaterThanOrEqual(0)
    expect(end).toBeGreaterThan(start)
    expect(routingBusyGate).toBeGreaterThanOrEqual(0)
    expect(ordinarySend).toBeGreaterThan(routingBusyGate)
    expect(viewSource).toContain('sendBlockedReason: effectiveSendBlockedReason,')
    expect(viewSource).toContain('&& !deliveryBlockedReason.value')
    expect(viewSource).toContain('sessionRoutingSendBlockedReason.value || liveSendBlockedReason.value')
    expect(viewSource).toContain(
      'deliveryBlockedReason.value || promptAnnotationSendBlockedReason.value',
    )
    expect(viewSource).not.toContain('\n  || sessionRoutingSendBlockedReason.value\n')
    expect(composerSource).toContain('|| sessionRoutingBusy || inputDisabled')
    expect(composerSource).toContain(":aria-busy=\"sessionRoutingBusy ? 'true' : 'false'\"")
    expect(composerSource).toContain(".chat-send-btn[aria-busy='true']:disabled")
    expect(composerSource).toContain("'is-ready': hasSendContent && !sendBlockedMessage && !inputDisabled")
    expect(en.chat.composer.routingUpdateBlocked)
      .toBe('Model routing is being updated. Wait before sending.')
  })

  it('offers exactly the three mutually-exclusive model-routing modes', () => {
    expect(modelRoutingSource).toContain("value: 'off'")
    expect(modelRoutingSource).toContain("value: 'squilla_router'")
    expect(modelRoutingSource).toContain("value: 'llm_ensemble'")
    expect(modelRoutingSource).not.toContain('setRouterEnabled')
    expect(modelRoutingSource).not.toContain('setLlmEnsembleEnabled')
    expect(modelRoutingSource).not.toContain(':disabled="busy"')
    expect(modelRoutingSource).toContain(":aria-disabled=\"busy ? 'true' : 'false'\"")
    const disabledStart = modelRoutingSource.indexOf(
      '.composer-model-routing__option[aria-disabled="true"] {',
    )
    const disabledEnd = modelRoutingSource.indexOf(
      '.composer-model-routing__option-main',
      disabledStart,
    )
    expect(modelRoutingSource.slice(disabledStart, disabledEnd)).not.toContain('opacity:')
    expect(modelRoutingSource).not.toContain(':disabled:hover')
  })
})
