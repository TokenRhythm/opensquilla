// Theme-aware syntax styling for the markdown ANSWER body.
//
// @opentui/core's MarkdownRenderable colors plain paragraph text from the
// SyntaxStyle "default" token (it resolves each chunk via
// getStyle(group) || getStyle("default")). A bare SyntaxStyle.create() registers
// NOTHING, so unstyled markdown text gets fg:undefined and falls back to a
// built-in light foreground — invisible on light themes (the answer body looked
// blank under opensquilla-light). Registering a full token set from the active
// THEME makes the body (and fenced code) honor the theme on every surface, and
// re-registering on a live /theme switch keeps it correct without recreating the
// shared SyntaxStyle object (so existing blocks pick the new colors up too).

// Map the active THEME tokens onto the syntax-style names the markdown renderer
// and tree-sitter scopes resolve. "default" is the critical one — it is the base
// color for all otherwise-unstyled paragraph text.
function themeStyleDefs(t) {
  const defs = {
    default: { fg: t.text },
    text: { fg: t.text },
    paragraph: { fg: t.text },
    // Markdown inline markup scopes. The bundled tree-sitter grammar emits
    // DOTTED capture names (markup.heading.1…6, markup.link.label/url,
    // markup.raw.block, markup.list.checked/unchecked) and the style lookup
    // falls back only to the FIRST dotted segment ("markup", unregistered), so
    // every emitted name needs its own registration — "markup.heading" alone
    // styles only pipe-table header cells, and "markup.raw" only inline code.
    "markup.heading": { fg: t.brandAccent, bold: true },
    "markup.strong": { fg: t.text, bold: true },
    "markup.italic": { fg: t.text, italic: true },
    "markup.strikethrough": { fg: t.muted },
    "markup.link": { fg: t.routeText, underline: true },
    "markup.link.label": { fg: t.routeText, underline: true },
    "markup.link.url": { fg: t.routeText, underline: true },
    "markup.raw": { fg: t.brandAccentSoft }, // inline `code`
    "markup.raw.block": { fg: t.brandAccentSoft }, // indented/fenced containers
    "markup.quote": { fg: t.muted, italic: true },
    "markup.list": { fg: t.text },
    "markup.list.unchecked": { fg: t.text },
    "markup.list.checked": { fg: t.text },
    // Common tree-sitter code scopes for fenced blocks — brand-forward and
    // legible on any background (each theme's tokens already clear WCAG AA).
    keyword: { fg: t.brandAccent },
    string: { fg: t.success },
    number: { fg: t.warning },
    constant: { fg: t.warning },
    comment: { fg: t.detailText, italic: true },
    function: { fg: t.routeText },
    type: { fg: t.brandAccentSoft },
    variable: { fg: t.text },
    operator: { fg: t.muted },
    punctuation: { fg: t.muted },
  };
  // ATX/setext headings are captured per level (markup.heading.N), all styled
  // like the base heading scope.
  for (let level = 1; level <= 6; level += 1) {
    defs[`markup.heading.${level}`] = { fg: t.brandAccent, bold: true };
  }
  return defs;
}

// Register/refresh every theme-derived style on a SyntaxStyle in place. Safe to
// call repeatedly (on each /theme switch); unknown token names are ignored so a
// library change can never break theming.
export function registerThemeStyles(syntaxStyle, t) {
  if (!syntaxStyle || typeof syntaxStyle.registerStyle !== "function") return syntaxStyle;
  for (const [name, def] of Object.entries(themeStyleDefs(t))) {
    try {
      syntaxStyle.registerStyle(name, def);
    } catch {
      // Ignore a style the installed @opentui/core build does not accept.
    }
  }
  syntaxStyle.clearCache?.();
  return syntaxStyle;
}

export { themeStyleDefs };
