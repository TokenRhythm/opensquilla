import { describe, expect, it } from 'vitest'
import { mergeExecutionIo, projectExecutionIo, projectExecutionIoForCall } from './executionIo'

describe('projectExecutionIo', () => {
  it('projects a successful PTY execution from the top-level result', () => {
    expect(projectExecutionIo(JSON.stringify({
      execution_id: 'exec-1',
      io_mode_requested: 'pty',
      io_mode_used: 'pty',
    }))).toMatchObject({ kind: 'pty', entries: [{ executionId: 'exec-1' }] })
  })

  it('requires io_mode_used and does not infer PTY from the request', () => {
    expect(projectExecutionIo(JSON.stringify({
      execution_id: 'exec-1',
      io_mode_requested: 'pty',
      io_mode: 'pty',
    }))).toMatchObject({ kind: 'unknown' })
  })

  it('projects fallback metadata nested in a process session', () => {
    expect(projectExecutionIo(JSON.stringify({
      execution_id: 'exec-1',
      session: {
        execution_id: 'exec-1',
        io_mode_requested: 'pty',
        io_mode_used: 'pipe',
        fallback_reason: 'backend unavailable',
      },
    }))).toMatchObject({ kind: 'fallback', fallbackReason: 'backend unavailable' })
  })

  it('deduplicates process sessions by execution id', () => {
    const result = projectExecutionIo(JSON.stringify({
      execution_ids: ['exec-1', 'exec-2'],
      sessions: [
        { execution_id: 'exec-1', io_mode_used: 'pipe' },
        { execution_id: 'exec-1', io_mode_used: 'pipe' },
        { execution_id: 'exec-2', io_mode_used: 'pty' },
      ],
    }))
    expect(result.kind).toBe('mixed')
    expect(result.entries).toHaveLength(2)
  })

  it('keeps an execution with no actual mode from being labelled as TTY', () => {
    const result = projectExecutionIo(JSON.stringify({
      sessions: [
        { execution_id: 'exec-pty', io_mode_used: 'pty' },
        { execution_id: 'exec-legacy', io_mode_requested: 'pty' },
      ],
    }))
    expect(result.kind).toBe('mixed')
  })

  it('limits and sanitizes the diagnostic shown with a fallback', () => {
    const result = projectExecutionIo(JSON.stringify({
      execution_id: 'exec-1',
      io_mode_requested: 'pty',
      io_mode_used: 'pipe',
      fallback_reason: `failed at /private/synthetic/project\n${'x'.repeat(400)}`,
    }))
    expect(result.kind).toBe('fallback')
    if (result.kind === 'fallback') {
      expect(result.fallbackReason).toContain('<path>')
      expect(result.fallbackReason).not.toContain('\n')
      expect(result.fallbackReason?.length).toBeLessThanOrEqual(240)
    }
  })
})

describe('mergeExecutionIo', () => {
  it('does not collapse mixed executions into a TTY label', () => {
    const merged = mergeExecutionIo([
      projectExecutionIo(JSON.stringify({ execution_id: 'pipe', io_mode_used: 'pipe' })),
      projectExecutionIo(JSON.stringify({ execution_id: 'pty', io_mode_used: 'pty' })),
    ])
    expect(merged.kind).toBe('mixed')
  })

  it('only projects execution tools', () => {
    expect(projectExecutionIoForCall({
      name: 'read_file',
      result: JSON.stringify({ execution_id: 'fake', io_mode_used: 'pty' }),
      resultPreview: '',
    })).toEqual({ kind: 'unknown', entries: [] })
  })

  it('keeps a fallback warning in a mixed group and redacts credentials', () => {
    const merged = mergeExecutionIo([
      projectExecutionIo({ execution_id: 'pty', io_mode_used: 'pty' }),
      projectExecutionIo({
        execution_id: 'fallback', io_mode_requested: 'pty', io_mode_used: 'pipe',
        fallback_reason: 'token=synthetic-private-value backend unavailable',
      }),
    ])
    expect(merged.kind).toBe('fallback')
    expect(JSON.stringify(merged)).not.toContain('synthetic-private-value')
  })

  it('does not turn a group of actual and legacy executions into TTY', () => {
    expect(mergeExecutionIo([
      projectExecutionIo({ execution_id: 'pty', io_mode_used: 'pty' }),
      projectExecutionIo({ execution_id: 'legacy', io_mode: 'pty' }),
    ]).kind).toBe('mixed')
  })
})
