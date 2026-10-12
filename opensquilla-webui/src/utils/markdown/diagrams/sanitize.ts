import createDOMPurify from 'dompurify'
import { MAX_SVG_CHARS } from './budget'
import { DiagramRenderError } from './types'

const SAFE_FRAGMENT = /^#[A-Za-z_][\w:.-]*$/

function safeCss(value: string): boolean {
  // Engines supply CSS for diagram styling; only local paint-server/marker
  // references may survive. Escapes and comments cannot obscure a URL.
  if (/[\\@<>]|\/\*|expression\s*\(|(?:javascript|https?|data|file|blob)\s*:|image(?:-set)?\s*\(/i.test(value)) return false
  for (const match of value.matchAll(/url\s*\(([^)]*)\)/gi)) {
    if (!SAFE_FRAGMENT.test(match[1].trim().replace(/^(['"])(.*)\1$/, '$2'))) return false
  }
  return true
}

function sanitizeStylesheet(source: string): string {
  // Mermaid includes animation keyframes even for static diagrams. Dropping
  // an entire sheet would also drop its theme and make nodes solid black.
  // Parse without adopting the sheet, then retain ordinary safe rules only.
  const sheet = new CSSStyleSheet()
  sheet.replaceSync(source)
  const rules: string[] = []
  for (const rule of Array.from(sheet.cssRules)) {
    if (rule.type !== CSSRule.STYLE_RULE) continue
    const styleRule = rule as CSSStyleRule
    // `>` is a normal child combinator (not markup) in parsed selectors;
    // Mermaid uses it for sequence actor text and nested label styling.
    if (/[\\@<]/.test(styleRule.selectorText)) continue
    const declarations: string[] = []
    for (const property of Array.from(styleRule.style)) {
      if (/^(?:animation|transition|behavior|-moz-binding)(?:-|$)/i.test(property)) continue
      const value = styleRule.style.getPropertyValue(property)
      if (safeCss(value)) declarations.push(`${property}:${value}${styleRule.style.getPropertyPriority(property) ? '!important' : ''}`)
    }
    if (declarations.length) rules.push(`${styleRule.selectorText}{${declarations.join(';')}}`)
  }
  return rules.join('\n')
}

export function sanitizeDiagramSvg(raw: string): string {
  if (raw.length > MAX_SVG_CHARS) throw new DiagramRenderError('budget')
  // Use a private instance: the Markdown purifier intentionally drops SVG
  // classes and has different hooks, which must not affect diagram labels.
  const purifier = createDOMPurify(window)
  purifier.addHook('uponSanitizeAttribute', (_node, data) => {
    const name = data.attrName.toLowerCase()
    if (name === 'href' || name === 'xlink:href') data.keepAttr = SAFE_FRAGMENT.test(data.attrValue)
    else if (name === 'style' || /url\s*\(/i.test(data.attrValue)) data.keepAttr = safeCss(data.attrValue)
  })
  const cleaned = purifier.sanitize(raw.replace(/<\?xml[^>]*>|<!DOCTYPE[^>]*>/gi, ''), {
    USE_PROFILES: { svg: true },
    ADD_TAGS: ['style', 'use'],
    FORBID_TAGS: ['foreignObject', 'script', 'image', 'a', 'animate', 'animateMotion', 'animateTransform', 'set'],
    FORBID_ATTR: ['target', 'tabindex'],
    ALLOW_DATA_ATTR: false,
  })
  const document = new DOMParser().parseFromString(cleaned, 'image/svg+xml')
  const svg = document.documentElement
  if (svg.localName !== 'svg' || document.querySelector('parsererror')) throw new DiagramRenderError('render')
  for (const style of svg.querySelectorAll('style')) {
    const css = sanitizeStylesheet(style.textContent ?? '')
    if (css) style.textContent = css
    else style.remove()
  }
  const viewBox = (svg.getAttribute('viewBox') ?? '').trim().split(/[\s,]+/).map(Number)
  if (viewBox.length !== 4 || !viewBox.every(Number.isFinite)) throw new DiagramRenderError('render')
  const [, , width, height] = viewBox
  if (width <= 0 || height <= 0 || width > 8_192 || height > 8_192 || width * height > 16_000_000) {
    throw new DiagramRenderError('budget')
  }
  // Avoid percentage-sized SVGs having a tiny/default intrinsic image size.
  svg.setAttribute('width', String(width))
  svg.setAttribute('height', String(height))
  svg.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  return new XMLSerializer().serializeToString(svg)
}
