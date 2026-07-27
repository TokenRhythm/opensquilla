import { describe, expect, it } from 'vitest'
import source from './ChatComposerSettings.vue?raw'
import runModeSource from './ChatComposerRunMode.vue?raw'
import modelRoutingSource from './ChatComposerModelRouting.vue?raw'
import composerSource from './ChatComposer.vue?raw'
import viewSource from '../../views/ChatView.vue?raw'
import zhHans from '../../locales/zh-Hans.json'

function controlSwitchBlock(label: string) {
  const labelIndex = source.indexOf(`label="${label}"`)
  if (labelIndex === -1) return ''
  const start = source.lastIndexOf('<ControlSwitch', labelIndex)
  const end = source.indexOf('/>', labelIndex)
  return source.slice(start, end)
}

describe('ChatComposerSettings coding mode contract', () => {
  it('keeps legacy execution mode choices out of the composer settings panel', () => {
    expect(source).not.toContain('chat.composer.executionMode')
    expect(source).not.toContain('composer-execution-mode')
    expect(source).not.toContain('setElevatedMode')
    expect(source).not.toContain('chat.composer.execOff')
    expect(source).not.toContain('chat.composer.execPrompt')
    expect(source).not.toContain('chat.composer.execBypass')
    expect(source).not.toContain('chat.composer.execFull')
  })

  it('threads the shield run-mode control through ChatComposer and ChatView', () => {
    expect(composerSource).toContain('ChatComposerRunMode')
    expect(composerSource).toContain('<Icon name="shield"')
    expect(composerSource).toContain('chat-run-mode-btn--${runMode}')
    expect(composerSource).toContain(':run-mode="runMode"')
    expect(composerSource).toContain('@set-run-mode="emit(\'setRunMode\', $event)"')
    expect(composerSource).toContain("setRunMode: [mode: 'standard' | 'trusted' | 'full']")

    expect(viewSource).toContain(':run-mode="runMode"')
    expect(viewSource).toContain(':allowed-run-modes="allowedRunModes"')
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

  it('offers exactly the three sandbox run modes from the shield popover', () => {
    expect(runModeSource).toContain("value: 'standard'")
    expect(runModeSource).toContain("value: 'trusted'")
    expect(runModeSource).toContain("value: 'full'")
    expect(runModeSource).not.toContain("value: 'on'")
    expect(runModeSource).not.toContain("value: 'bypass'")
  })

  it('places Coding mode after Visual effects', () => {
    const visualEffectsIndex = source.indexOf('label="Visual effects"')
    const codingModeIndex = source.indexOf('label="Coding mode"')

    expect(visualEffectsIndex).toBeGreaterThanOrEqual(0)
    expect(codingModeIndex).toBeGreaterThan(visualEffectsIndex)
  })

  it('binds Coding mode checked and busy state to typed props', () => {
    const block = controlSwitchBlock('Coding mode')

    expect(block).toContain(':checked="codingModeEnabled"')
    expect(block).toContain(':busy="codingModeSettingsBusy"')
    expect(source).toContain('codingModeEnabled: boolean')
    expect(source).toContain('codingModeSettingsBusy: boolean')
  })

  it('emits Coding mode changes through the typed settings event', () => {
    const block = controlSwitchBlock('Coding mode')

    expect(block).toContain('@change="$emit(\'setCodingModeEnabled\', $event)"')
    expect(source).toContain('setCodingModeEnabled: [enabled: boolean]')
  })

  it('threads Coding mode props and events through ChatComposer and ChatView', () => {
    expect(composerSource).toContain(':coding-mode-enabled="codingModeEnabled"')
    expect(composerSource).toContain(':coding-mode-settings-busy="codingModeSettingsBusy"')
    expect(composerSource).toContain('@set-coding-mode-enabled="emit(\'setCodingModeEnabled\', $event)"')
    expect(composerSource).toContain('setCodingModeEnabled: [enabled: boolean]')

    expect(viewSource).toContain(':coding-mode-enabled="codingModeEnabled"')
    expect(viewSource).toContain(':coding-mode-settings-busy="codingModeSettingsBusy"')
    expect(viewSource).toContain('@set-coding-mode-enabled="setComposerCodingModeEnabled"')
    expect(viewSource).toContain('async function setComposerCodingModeEnabled(enabled: boolean)')
    expect(viewSource).toContain('await setCodingModeEnabled(enabled)')
  })
})

describe('ChatComposer model routing contract', () => {
  it('keeps model-routing choices out of the generic composer settings panel', () => {
    expect(source).not.toContain('label="Squilla Router"')
    expect(source).not.toContain('label="LLM Ensemble"')
    expect(source).not.toContain('routerEnabled: boolean')
    expect(source).not.toContain('llmEnsembleEnabled: boolean')
    expect(source).not.toContain('setRouterEnabled')
    expect(source).not.toContain('setLlmEnsembleEnabled')
  })

  it('threads the independent model-routing control through ChatComposer and ChatView', () => {
    expect(composerSource).toContain('ChatComposerModelRouting')
    expect(composerSource).toContain('<Icon name="router"')
    expect(composerSource).toContain('chat-model-routing-btn--${modelRoutingMode}')
    expect(composerSource).toContain("'is-active': modelRoutingOpen || modelRoutingMode !== 'off'")
    expect(composerSource).toContain(':model-routing-mode="modelRoutingMode"')
    expect(composerSource).toContain(':busy="modelRoutingSettingsBusy"')
    expect(composerSource).toContain('@set-model-routing-mode="emit(\'setModelRoutingMode\', $event)"')
    expect(composerSource).toContain('modelRoutingMode: ModelRoutingMode')
    expect(composerSource).toContain('setModelRoutingMode: [mode: ModelRoutingMode]')

    expect(viewSource).toContain(':model-routing-mode="modelRoutingMode"')
    expect(viewSource).toContain(':model-routing-settings-busy="modelRoutingSettingsBusy"')
    expect(viewSource).toContain('@set-model-routing-mode="setComposerModelRoutingMode"')
    expect(viewSource).toContain('async function setComposerModelRoutingMode(mode: ModelRoutingMode)')
    expect(viewSource).toContain('await setModelRoutingMode(mode)')
  })

  it('offers exactly the three mutually-exclusive model-routing modes', () => {
    expect(modelRoutingSource).toContain("value: 'off'")
    expect(modelRoutingSource).toContain("value: 'squilla_router'")
    expect(modelRoutingSource).toContain("value: 'llm_ensemble'")
    expect(modelRoutingSource).not.toContain('setRouterEnabled')
    expect(modelRoutingSource).not.toContain('setLlmEnsembleEnabled')
  })
})
