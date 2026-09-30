import { describe, expect, it } from 'vitest'
import { copyLocalPathReferences, localPathName, localPathPresentation } from './localPathReferences'

const paths = ['C:\\Users\\示例 用户\\report.pdf', '/tmp/report.pdf', '\\\\server\\share\\note.txt']

describe('explicit local path display metadata', () => {
  it('separates the marked suffix without changing the source text', () => {
    const message = `Compare\n${paths.join('\n')}`
    expect(localPathPresentation(message, paths)).toEqual({ text: 'Compare', paths })
    expect(localPathPresentation(paths.join('\n'), paths)).toEqual({ text: '', paths })
    expect(localPathName(paths[0]!)).toBe('report.pdf')
    expect(localPathName(paths[2]!)).toBe('note.txt')
    expect(message).toBe(`Compare\n${paths.join('\n')}`)
  })

  it('does not hide hand-written, embedded, stale or legacy path text', () => {
    expect(localPathPresentation(paths[0]!)).toEqual({ text: paths[0], paths: [] })
    expect(localPathPresentation(`Explain ${paths[0]}`, [paths[0]!]).paths).toEqual([])
    expect(localPathPresentation(`${paths[0]}\nKeep this instruction`, [paths[0]!]).paths).toEqual([])
    const text = `${paths[0]}\nThis is also written by the user\n${paths[0]}`
    expect(localPathPresentation(text, [paths[0]!]).text).toBe(`${paths[0]}\nThis is also written by the user`)
  })

  it.each([['relative.txt'], ['C:relative.txt'], ['\n/tmp/a'], ['/tmp/a\u0000'], [7], [' /tmp/a'], ['/tmp/a'.repeat(10_000)]])('ignores invalid display metadata %j', value => {
    expect(copyLocalPathReferences(value)).toEqual([])
  })
})
