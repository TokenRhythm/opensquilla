import { describe, expect, it } from 'vitest'
import runModeSource from './ChatComposerRunMode.vue?raw'
import modelRoutingSource from './ChatComposerModelRouting.vue?raw'
import composerSource from './ChatComposer.vue?raw'
import viewSource from '../../views/ChatView.vue?raw'
import appearanceSource from '../settings/SettingsAppearancePanel.vue?raw'
import slashSource from '../../composables/chat/useChatSlashCommands.ts?raw'
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
    expect(zhHans.chat.composer.runModeStandardDesc)
      .toBe('访问项目外文件、网络或更改系统时会询问你。')
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
