import type { MarkedExtension } from 'marked'

/**
 * GFM lets a single `~` open strikethrough, so `~12万和~510万` renders as
 * `<del>12万和</del>510万`. In assistant output a lone tilde is almost never
 * markup — it is "approximately" in front of a number, a `~/path`, or a range —
 * and striking the text through silently rewrites what the model said.
 *
 * Require the doubled delimiter. `~~gone~~` still renders with marked's native
 * delimiter rules, and everything a `<del>` can nest still nests; only the
 * single-tilde spelling stops being markup.
 */
export const strictStrikethrough: MarkedExtension = {
  tokenizer: {
    del(src: string) {
      // Returning false delegates to the tokenizer that marked registered
      // before this override. Returning undefined suppresses that fallback, so
      // a single tilde is consumed by the ordinary inline-text tokenizer.
      return src.startsWith('~~') ? false : undefined
    },
  },
}
