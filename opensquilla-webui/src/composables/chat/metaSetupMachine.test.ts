import { describe, expect, it } from 'vitest'

import type { MetaSetupJob, MetaSetupReadiness, MetaSetupState } from '@/types/metaSetup'

import {
  META_SETUP_PROVIDER_HANDOFF_TTL_MS,
  createMetaSetupConfirmationState,
  metaSetupResumeRequestId,
  projectMetaSetupJob,
  transitionMetaSetupState,
  validMetaSetupProviderHandoff,
} from './metaSetupMachine'

const SESSION = 'agent:main:webchat:setup-machine'

function readiness(overrides: Partial<MetaSetupReadiness> = {}): MetaSetupReadiness {
  return {
    ready: false,
    status: 'needs_setup',
    setup_actions: [{ id: 'install-tool', available: true }],
    ...overrides,
  }
}

function state(overrides: Partial<MetaSetupState> = {}): MetaSetupState {
  return {
    name: 'meta-paper-write',
    sessionKey: SESSION,
    launchText: '/meta meta-paper-write -- topic',
    phase: 'confirm',
    readiness: readiness(),
    actionIds: ['install-tool'],
    completedActions: [],
    ...overrides,
  }
}

function job(overrides: Partial<MetaSetupJob> = {}): MetaSetupJob {
  return {
    job_id: 'job-1',
    name: 'meta-paper-write',
    sessionKey: SESSION,
    action_ids: ['install-tool'],
    status: 'running',
    phase: 'installing',
    completed_actions: [],
    readiness: null,
    ...overrides,
  }
}

describe('meta setup state machine', () => {
  it('derives confirm and blocked states from readiness without side effects', () => {
    const confirm = createMetaSetupConfirmationState(
      'meta-paper-write',
      readiness(),
      SESSION,
      '/meta meta-paper-write -- topic',
    )
    expect(confirm).toMatchObject({
      phase: 'confirm',
      actionIds: ['install-tool'],
      launchText: '/meta meta-paper-write -- topic',
    })

    const blocked = createMetaSetupConfirmationState(
      'meta-paper-write',
      { reasons: ['Connect a provider'] },
      SESSION,
      '/meta meta-paper-write unsafe trailing text',
    )
    expect(blocked).toMatchObject({
      phase: 'blocked',
      blockedReason: 'no_actions',
      retryMode: 'readiness',
      error: 'Connect a provider',
      launchText: '/meta meta-paper-write',
    })
  })

  it('moves through install, verify, failure, and session-change states immutably', () => {
    const initial = state({ message: 'old', error: 'old', retryMode: 'install' })
    const installing = transitionMetaSetupState(initial, { type: 'install_started' })
    const verifying = transitionMetaSetupState(installing, {
      type: 'verification_started',
      readiness: { ready: true },
    })
    const failed = transitionMetaSetupState(verifying, {
      type: 'failed',
      error: 'launch failed',
      retryMode: 'launch',
    })
    const blocked = transitionMetaSetupState(failed, {
      type: 'session_changed',
      clearRetryMode: true,
    })

    expect(initial.phase).toBe('confirm')
    expect(installing).toMatchObject({ phase: 'installing', message: '', error: '' })
    expect(verifying).toMatchObject({ phase: 'verifying', readiness: { ready: true } })
    expect(failed).toMatchObject({ phase: 'failed', retryMode: 'launch' })
    expect(blocked).toMatchObject({ phase: 'blocked', blockedReason: 'session_changed' })
    expect(blocked.retryMode).toBeUndefined()
  })

  it('preserves the durable request identity across provider handoff cancellation', () => {
    const handoff = {
      kind: 'provider_settings' as const,
      providerId: 'openai',
      startedAtMs: 100,
      clientRequestId: 'request-1',
    }
    const handedOff = transitionMetaSetupState(
      state({ suppressAutoResume: true }),
      { type: 'provider_handoff_started', handoff },
    )
    const cancelled = transitionMetaSetupState(handedOff, {
      type: 'provider_handoff_cancelled',
    })

    expect(handedOff.suppressAutoResume).toBeUndefined()
    expect(metaSetupResumeRequestId(handedOff)).toBe('request-1')
    expect(cancelled.providerHandoff).toBeUndefined()
    expect(cancelled.resumeRequestId).toBe('request-1')
  })

  it('accepts only current, available provider handoffs', () => {
    const providerReadiness = readiness({
      manual_setup_actions: [{
        id: 'connect-provider',
        kind: 'provider_connection',
        provider_id: 'OpenAI',
        available: true,
      }],
    })
    const now = 1_000_000
    const handoff = {
      kind: 'provider_settings',
      providerId: 'OPENAI',
      startedAtMs: now - META_SETUP_PROVIDER_HANDOFF_TTL_MS,
      clientRequestId: ' request-1 ',
    }

    expect(validMetaSetupProviderHandoff(handoff, providerReadiness, now)).toEqual({
      kind: 'provider_settings',
      providerId: 'openai',
      startedAtMs: handoff.startedAtMs,
      clientRequestId: 'request-1',
    })
    expect(validMetaSetupProviderHandoff(
      { ...handoff, startedAtMs: handoff.startedAtMs - 1 },
      providerReadiness,
      now,
    )).toBeUndefined()
  })

  it('projects active and completed server jobs while retaining setup coordinates', () => {
    const active = projectMetaSetupJob(
      state(),
      job({ downloaded_bytes: 12, download_total_bytes: 20 }),
      '/meta meta-paper-write -- topic',
    )
    expect(active).toMatchObject({
      kind: 'active',
      state: {
        phase: 'installing',
        jobId: 'job-1',
        downloadedBytes: 12,
        downloadTotalBytes: 20,
      },
    })

    const completed = projectMetaSetupJob(
      active.state,
      job({ status: 'completed', phase: 'completed', completed_actions: ['install-tool'] }),
      '/meta meta-paper-write -- topic',
    )
    expect(completed).toMatchObject({
      kind: 'completed',
      state: { phase: 'verifying', completedActions: ['install-tool'], error: '' },
    })
  })

  it('projects blocked and failed jobs with the correct recovery mode', () => {
    const identified = state({
      resumeRequestId: 'request-1',
      providerHandoff: {
        kind: 'provider_settings',
        providerId: 'openai',
        startedAtMs: 100,
        clientRequestId: 'request-1',
      },
    })
    const blocked = projectMetaSetupJob(
      identified,
      job({
        status: 'blocked',
        phase: 'blocked',
        readiness: { manual_setup_actions: [] },
        error: 'Provider still missing',
      }),
      '/meta meta-paper-write -- topic',
    )
    expect(blocked).toMatchObject({
      kind: 'blocked',
      state: {
        blockedReason: 'requirements_remaining',
        resumeRequestId: 'request-1',
        error: 'Provider still missing',
      },
    })

    const failed = projectMetaSetupJob(
      state(),
      job({
        status: 'failed',
        phase: 'failed',
        completed_actions: [],
        error: 'Install failed',
      }),
      '/meta meta-paper-write -- topic',
    )
    expect(failed).toMatchObject({
      kind: 'failed',
      state: { phase: 'failed', retryMode: 'install', error: 'Install failed' },
    })
  })
})
