import { describe, expect, it } from 'vitest'

import type { MetaSetupState } from '@/types/metaSetup'

import { META_SETUP_PROVIDER_HANDOFF_TTL_MS } from './metaSetupMachine'
import {
  createMetaSetupRepository,
  metaSetupLaunchStorageKey,
  metaSetupManualStorageKey,
  metaSetupStorageKey,
  type MetaSetupStorage,
} from './metaSetupRepository'

const SESSION = 'agent:main:webchat:setup repository'

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial))
  const storage: MetaSetupStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  return { storage, values }
}

function checkpoint(overrides: Partial<MetaSetupState> = {}): MetaSetupState {
  return {
    name: 'meta-paper-write',
    sessionKey: SESSION,
    launchText: '/meta meta-paper-write -- topic',
    phase: 'installing',
    readiness: {
      setup_actions: [{ id: 'install-tool', available: true }],
      manual_setup_actions: [{
        id: 'connect-provider',
        kind: 'provider_connection',
        provider_id: 'openai',
        available: true,
      }],
    },
    actionIds: ['install-tool'],
    jobId: 'ephemeral-job',
    jobStatus: 'running',
    message: 'Installing',
    currentAction: 'install-tool',
    completedActions: [],
    ...overrides,
  }
}

describe('meta setup repository', () => {
  it('keeps the existing encoded storage-key contract', () => {
    expect(metaSetupStorageKey(SESSION)).toBe(
      'opensquilla.chat.metaSetupJob:agent%3Amain%3Awebchat%3Asetup%20repository',
    )
    expect(metaSetupLaunchStorageKey(SESSION)).toContain('metaSetupLaunch:agent%3Amain')
    expect(metaSetupManualStorageKey(SESSION)).toContain('metaSetupManual:agent%3Amain')
  })

  it('persists only stable replay coordinates, never server job progress', () => {
    const { storage, values } = memoryStorage()
    const repository = createMetaSetupRepository(storage)
    const current = checkpoint({
      resumeRequestId: 'request-1',
      providerHandoff: {
        kind: 'provider_settings',
        providerId: 'openai',
        startedAtMs: Date.now(),
        clientRequestId: 'request-1',
      },
    })

    expect(repository.persistCheckpoint(current)).toBe(true)
    const persisted = JSON.parse(values.get(metaSetupManualStorageKey(SESSION)) || '{}')
    expect(persisted).toEqual({
      name: current.name,
      launchText: current.launchText,
      readiness: current.readiness,
      providerHandoff: current.providerHandoff,
      resumeRequestId: 'request-1',
    })
    expect(persisted).not.toHaveProperty('jobId')
    expect(persisted).not.toHaveProperty('currentAction')
  })

  it('sanitizes an expired provider handoff but preserves its durable request identity', () => {
    const startedAtMs = Date.now() - META_SETUP_PROVIDER_HANDOFF_TTL_MS - 1
    const original = checkpoint({
      resumeRequestId: 'request-1',
      providerHandoff: {
        kind: 'provider_settings',
        providerId: 'openai',
        startedAtMs,
        clientRequestId: 'request-1',
      },
    })
    const { storage, values } = memoryStorage({
      [metaSetupManualStorageKey(SESSION)]: JSON.stringify({
        name: original.name,
        launchText: original.launchText,
        readiness: original.readiness,
        providerHandoff: original.providerHandoff,
        resumeRequestId: original.resumeRequestId,
      }),
    })
    const restored = createMetaSetupRepository(storage).readCheckpoint(SESSION)

    expect(restored?.providerHandoff).toBeUndefined()
    expect(restored?.resumeRequestId).toBe('request-1')
    expect(restored?.suppressAutoResume).toBe(true)
    const sanitized = values.get(metaSetupManualStorageKey(SESSION)) || ''
    expect(sanitized).not.toContain('providerHandoff')
    expect(sanitized).toContain('suppressAutoResume')
  })

  it('clears only the missing job pointer and restores a stable checkpoint', () => {
    const current = checkpoint({ phase: 'confirm', jobId: undefined })
    const { storage, values } = memoryStorage({
      [metaSetupStorageKey(SESSION)]: 'missing-job',
      [metaSetupManualStorageKey(SESSION)]: JSON.stringify({
        name: current.name,
        launchText: current.launchText,
        readiness: current.readiness,
        resumeRequestId: 'request-1',
      }),
    })
    const restored = createMetaSetupRepository(storage).recoverFromMissingJob(SESSION)

    expect(values.has(metaSetupStorageKey(SESSION))).toBe(false)
    expect(values.has(metaSetupManualStorageKey(SESSION))).toBe(true)
    expect(restored).toMatchObject({
      name: 'meta-paper-write',
      launchText: '/meta meta-paper-write -- topic',
      resumeRequestId: 'request-1',
    })
  })

  it('upgrades a legacy launch-only record into a readiness checkpoint', () => {
    const launchText = '/meta meta-paper-write -- legacy topic'
    const { storage, values } = memoryStorage({
      [metaSetupStorageKey(SESSION)]: 'missing-job',
      [metaSetupLaunchStorageKey(SESSION)]: launchText,
    })
    const restored = createMetaSetupRepository(storage).recoverFromMissingJob(SESSION)

    expect(restored).toMatchObject({
      name: 'meta-paper-write',
      phase: 'blocked',
      retryMode: 'readiness',
      launchText,
    })
    expect(values.has(metaSetupManualStorageKey(SESSION))).toBe(true)
  })

  it('fails open when browser storage is unavailable', () => {
    const storage: MetaSetupStorage = {
      getItem: () => { throw new Error('blocked') },
      setItem: () => { throw new Error('blocked') },
      removeItem: () => { throw new Error('blocked') },
    }
    const repository = createMetaSetupRepository(storage)

    expect(repository.readJob(SESSION)).toBe('')
    expect(repository.readCheckpoint(SESSION)).toBeNull()
    expect(repository.persistCheckpoint(checkpoint())).toBe(false)
    expect(() => repository.clearJob(SESSION)).not.toThrow()
  })
})
