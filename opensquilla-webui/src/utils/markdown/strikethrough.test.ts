// @vitest-environment happy-dom

import { describe, expect, it } from 'vitest'

import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import { renderArtifactMarkdown } from '@/utils/workbench/artifactPreview'

const { renderMarkdown } = useChatTextRendering()

describe('a lone tilde is text, not markup', () => {
  it('keeps both approximate figures the model actually wrote', () => {
    // The reported output: two "approximately" tildes in one sentence used to
    // pair up, so "12万和" rendered struck through and the reader saw a number
    // the model never retracted.
    const html = renderMarkdown('大约~12万和~510万')
    expect(html).not.toContain('<del>')
    expect(html).toContain('~12万和~510万')
  })

  it('leaves adjacent approximations alone', () => {
    expect(renderMarkdown('~12万~510万')).not.toContain('<del>')
  })

  it('leaves home-relative paths alone', () => {
    const html = renderMarkdown('Compare ~/Documents and ~/Downloads')
    expect(html).not.toContain('<del>')
    expect(html).toContain('~/Documents')
    expect(html).toContain('~/Downloads')
  })

  it('leaves a numeric range alone', () => {
    expect(renderMarkdown('takes ~3~5 minutes')).not.toContain('<del>')
  })
})

describe('the doubled delimiter still strikes through', () => {
  it('renders ~~text~~', () => {
    expect(renderMarkdown('这个真的~~没了~~')).toContain('<del>没了</del>')
  })

  it('still nests inline markup inside a strikethrough', () => {
    const html = renderMarkdown('~~gone **but bold**~~')
    expect(html).toContain('<del>')
    expect(html).toContain('<strong>but bold</strong>')
  })

  it('preserves marked nested-delimiter semantics', () => {
    const html = renderMarkdown('~~outer ~~inner~~ text~~')
    expect(html).toContain('<del>outer <del>inner</del> text</del>')
  })

  it('does not let an unmatched opener swallow a later valid span', () => {
    const html = renderMarkdown('~~a ~~b~~')
    expect(html).toContain('~~a <del>b</del>')
    expect(html).not.toContain('<del>a ~~b</del>')
  })

  it('keeps whitespace-sensitive delimiters separate', () => {
    const html = renderMarkdown('~~a ~~ b ~~c~~')
    expect(html).toContain('~~a ~~ b <del>c</del>')
  })

  it('mixes a real strikethrough with a literal tilde in one line', () => {
    const html = renderMarkdown('a ~~b~~ c ~d~ e')
    expect(html).toContain('<del>b</del>')
    expect(html).toContain('~d~')
  })

  it('shows an unclosed delimiter literally', () => {
    const html = renderMarkdown('~~never closed')
    expect(html).not.toContain('<del>')
    expect(html).toContain('~~never closed')
  })
})

describe('the rule reaches markdown artifacts too', () => {
  it('does not depend on the chat renderer being imported first', () => {
    const html = renderArtifactMarkdown('大约~12万和~510万')
    expect(html).not.toContain('<del>')
    expect(html).toContain('~12万和~510万')
  })

  it('still renders a doubled delimiter in an artifact', () => {
    expect(renderArtifactMarkdown('~~gone~~')).toContain('<del>gone</del>')
  })
})
