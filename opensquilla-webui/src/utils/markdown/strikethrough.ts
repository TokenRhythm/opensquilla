import type { MarkedExtension, Tokens } from 'marked'

/**
 * GFM lets a single `~` open strikethrough, so `~12万和~510万` renders as
 * `<del>12万和</del>510万`. In assistant output a lone tilde is almost never
 * markup — it is "approximately" in front of a number, a `~/path`, or a range —
 * and striking the text through silently rewrites what the model said.
 *
 * Require the doubled delimiter. `~~gone~~` still renders, and everything a
 * `<del>` can nest still nests; only the single-tilde spelling stops being
 * markup. Anything this tokenizer declines falls through to inline text and is
 * shown literally.
 */
const DOUBLE_TILDE = /^~~(?=[^\s~])((?:\\.|[^\\])*?(?:\\.|[^\s~\\]))~~/

export const strictStrikethrough: MarkedExtension = {
  tokenizer: {
    del(src: string): Tokens.Del | undefined {
      const match = DOUBLE_TILDE.exec(src)
      if (!match) return undefined
      return {
        type: 'del',
        raw: match[0],
        text: match[1],
        tokens: this.lexer.inlineTokens(match[1]),
      }
    },
  },
}
