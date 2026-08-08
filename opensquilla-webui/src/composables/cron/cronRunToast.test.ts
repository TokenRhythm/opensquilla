import { describe, expect, it } from 'vitest'

import { cronRunFinishedToast, cronToastSummary } from './cronRunToast'

// Mirrors vue-i18n's interpolation closely enough to assert on the rendered
// string rather than on which key was picked.
function t(key: string, params: Record<string, unknown> = {}): string {
  const templates: Record<string, string> = {
    'cronSkills.jobs.unnamedTask': 'Unnamed task',
    'cronSkills.jobs.toastBackgroundComplete':
      'Task “{name}” completed. View the result in run history.',
    'cronSkills.jobs.toastBackgroundCompleteWithSummary': 'Task “{name}” completed: {summary}',
    'cronSkills.jobs.toastBackgroundFailed':
      'Task “{name}” failed. Use the retry button on its card.',
  }
  const template = templates[key]
  if (template === undefined) throw new Error(`missing translation: ${key}`)
  return template.replace(/\{(\w+)\}/g, (_match, name: string) => String(params[name] ?? ''))
}

describe('cronRunFinishedToast', () => {
  it('shows the reminder text a static-reminder job exists to deliver', () => {
    // The scheduler sends the configured text as the run summary. Reporting only
    // "completed" and pointing at run history hides the one thing the user
    // scheduled.
    const toast = cronRunFinishedToast(
      { jobName: 'Standup', success: true, summary: 'Post the standup notes' },
      t,
    )

    expect(toast.message).toBe('Task “Standup” completed: Post the standup notes')
    expect(toast.tone).toBe('ok')
  })

  it('keeps the history wording when the run carried no summary', () => {
    const toast = cronRunFinishedToast({ jobName: 'Nightly', success: true }, t)

    expect(toast.message).toBe('Task “Nightly” completed. View the result in run history.')
    expect(toast.tone).toBe('ok')
  })

  it('treats a blank or non-string summary as absent', () => {
    for (const summary of ['', '   ', '\n\t', undefined, null, 42]) {
      const toast = cronRunFinishedToast(
        { jobName: 'Nightly', success: true, summary: summary as string | undefined },
        t,
      )
      expect(toast.message).toBe('Task “Nightly” completed. View the result in run history.')
    }
  })

  it('flattens a multi-line reminder into one toast line', () => {
    const toast = cronRunFinishedToast(
      { jobName: 'Standup', success: true, summary: '  Call the vet\n\nthen book a slot  ' },
      t,
    )

    expect(toast.message).toBe('Task “Standup” completed: Call the vet then book a slot')
  })

  it('trims a long summary instead of letting it fill the screen', () => {
    const toast = cronRunFinishedToast(
      { jobName: 'Digest', success: true, summary: 'x'.repeat(500) },
      t,
    )
    const rendered = toast.message.replace('Task “Digest” completed: ', '')

    expect(rendered).toHaveLength(160)
    expect(rendered.endsWith('…')).toBe(true)
  })

  it('leaves the failure path reporting the retry affordance', () => {
    // A failed run's summary is not the user's reminder, and an error string is
    // its own decision; this change deliberately does not touch that copy.
    const toast = cronRunFinishedToast(
      { jobName: 'Nightly', success: false, summary: 'boom' },
      t,
    )

    expect(toast.message).toBe('Task “Nightly” failed. Use the retry button on its card.')
    expect(toast.tone).toBe('danger')
    expect(toast.duration).toBe(9_000)
  })

  it('falls back to the unnamed-task label without dropping the summary', () => {
    const toast = cronRunFinishedToast({ success: true, summary: 'Water the plants' }, t)

    expect(toast.message).toBe('Task “Unnamed task” completed: Water the plants')
  })
})

describe('cronToastSummary', () => {
  it('returns an empty string for everything that is not usable text', () => {
    for (const value of [undefined, null, 0, false, {}, [], '   ']) {
      expect(cronToastSummary(value)).toBe('')
    }
  })

  it('passes a short single-line summary through untouched', () => {
    expect(cronToastSummary('Take the bins out')).toBe('Take the bins out')
  })
})
