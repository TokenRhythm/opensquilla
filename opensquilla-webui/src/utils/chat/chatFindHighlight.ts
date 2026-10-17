/** Read-only DOM projection of literal message matches. Never wrap framework-owned text nodes. */
export interface ChatFindHighlights {
  ranges: Range[]
  totalMatches: number
  activeRange: Range | null
  activeRect: DOMRect | null
  supported: boolean
  /** The caller may add a Vue-owned outline when CSS Highlights are unavailable. */
  fallbackTarget: HTMLElement | null
}

interface HighlightRegistry {
  get(name: string): unknown
  set(name: string, highlight: unknown): void
  delete(name: string): void
}
interface HighlightLike { priority?: number }
type HighlightConstructor = new (...ranges: Range[]) => HighlightLike

const MATCH_NAME = 'opensquilla-chat-find'
const ACTIVE_NAME = 'opensquilla-chat-find-active'
const STYLE_ID = 'opensquilla-chat-find-style'
const SKIP = 'button,input,textarea,select,script,style,noscript,[hidden],[aria-hidden="true"],[contenteditable]:not([contenteditable="false"]),[data-chat-find-ignore]'
const BLOCK_TAGS = new Set(['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'BR', 'DD', 'DIV', 'DL', 'DT', 'FIGCAPTION', 'FIGURE', 'FOOTER', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER', 'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION', 'TABLE', 'TD', 'TH', 'TR', 'UL'])

export const CHAT_FIND_RANGE_LIMIT = 1200
const TEXT_BATCH = 32 * 1024
const SLICE_MS = 6
interface TextGroup { nodes: Text[]; text: string }
interface Projection { groups: TextGroup[]; bodies: HTMLElement[] }
interface Collected { ranges: Map<number, Range>; totalMatches: number }

function bodyRoots(root: HTMLElement) {
  const answer = root.querySelector<HTMLElement>('.assistant-answer [data-chat-find-body]')
  const bodies = answer ? [answer] : Array.from(root.querySelectorAll<HTMLElement>('[data-chat-find-body]'))
  return bodies.length ? bodies : root.matches('.msg-ai,.msg-user') ? [] : [root]
}

function hiddenBody(body: HTMLElement) {
  if (body.closest(SKIP)) return true
  let parent: HTMLElement | null = body
  while ((parent = parent.parentElement)) {
    if (parent.tagName === 'DETAILS' && !parent.hasAttribute('open')) {
      const summary = Array.from(parent.children).find(child => child.tagName === 'SUMMARY')
      if (!summary?.contains(body)) return true
    }
  }
  return false
}

function* project(root: HTMLElement): Generator<void, Projection> {
  const groups: TextGroup[] = [], bodies = bodyRoots(root)
  let nodes: Text[] = [], visited = 0
  function flush() {
    if (nodes.length) groups.push({ nodes, text: nodes.length === 1 ? nodes[0]!.data : nodes.map(node => node.data).join('') })
    nodes = []
  }
  // An explicit stack avoids recursive overflow for deeply nested Markdown.
  for (const body of bodies) {
    if (hiddenBody(body)) continue
    const stack: Array<Node | null> = [body]
    while (stack.length) {
      const node = stack.pop()
      if (!node) { flush(); continue }
      if (++visited % 128 === 0) yield
      if (node.nodeType === 3) { if (node.textContent) nodes.push(node as Text); continue }
      if (node.nodeType !== 1) continue
      const element = node as HTMLElement
      if (element.matches(SKIP)) { flush(); continue }
      const boundary = BLOCK_TAGS.has(element.tagName)
      if (boundary) { flush(); stack.push(null) }
      const children = element.tagName === 'DETAILS' && !element.hasAttribute('open')
        ? Array.from(element.children).filter(child => child.tagName === 'SUMMARY') : Array.from(element.childNodes)
      for (let i = children.length - 1; i >= 0; i--) stack.push(children[i]!)
    }
    flush()
  }
  return { groups, bodies }
}

/** Chunk overlap preserves literal matches spanning batches, without allocating all matches. */
function* literalMatches(text: string, literal: string): Generator<{ offset: number; length: number } | null> {
  const expression = new RegExp(literal.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu')
  let cursor = 0, work = 0
  while (cursor < text.length) {
    const end = Math.min(text.length, cursor + TEXT_BATCH)
    const part = text.slice(cursor, Math.min(text.length, end + literal.length - 1))
    expression.lastIndex = 0
    let consumed = end, match: RegExpExecArray | null
    while ((match = expression.exec(part))) {
      if (cursor + match.index >= end) break
      const offset = cursor + match.index
      consumed = Math.max(consumed, offset + match[0].length)
      yield { offset, length: match[0].length }
      if (++work % 256 === 0) yield null
    }
    cursor = consumed
    yield null
  }
}

function* collect(projection: Projection, query: string, activeIndex: number): Generator<void, Collected> {
  const ranges = new Map<number, Range>()
  let totalMatches = 0
  for (const group of projection.groups) {
    let nodeIndex = 0, nodeOffset = 0
    for (const match of literalMatches(group.text, query)) {
      if (!match) { yield; continue }
      const index = totalMatches++
      if (index >= CHAT_FIND_RANGE_LIMIT && index !== activeIndex) continue
      if (index === CHAT_FIND_RANGE_LIMIT - 1 && activeIndex >= CHAT_FIND_RANGE_LIMIT) continue
      while (nodeIndex < group.nodes.length - 1 && nodeOffset + group.nodes[nodeIndex]!.length <= match.offset) {
        nodeOffset += group.nodes[nodeIndex++]!.length
      }
      const start = group.nodes[nodeIndex]!
      const startOffset = match.offset - nodeOffset
      let endIndex = nodeIndex, endOffset = nodeOffset
      const to = match.offset + match.length
      while (endIndex < group.nodes.length - 1 && endOffset + group.nodes[endIndex]!.length < to) {
        endOffset += group.nodes[endIndex++]!.length
      }
      const range = start.ownerDocument.createRange()
      range.setStart(start, startOffset)
      range.setEnd(group.nodes[endIndex]!, to - endOffset)
      if (ranges.size === CHAT_FIND_RANGE_LIMIT) ranges.delete(CHAT_FIND_RANGE_LIMIT - 1)
      ranges.set(index, range)
    }
  }
  return { ranges, totalMatches }
}

function run<T>(work: Generator<void, T>): T {
  let step = work.next()
  while (!step.done) step = work.next()
  return step.value
}

async function runCooperatively<T>(work: Generator<void, T>, current: () => boolean): Promise<T | undefined> {
  let started = performance.now()
  while (current()) {
    const step = work.next()
    if (step.done) return step.value
    if (performance.now() - started >= SLICE_MS) {
      await new Promise<void>(resolve => setTimeout(resolve, 0))
      started = performance.now()
    }
  }
  work.return(undefined as T)
  return undefined
}

/** Matches across Markdown inline elements, while keeping separate paragraphs/messages separate. */
export function collectChatFindRanges(root: HTMLElement, query: string): Range[] {
  const literal = query.trim()
  if (!literal || root.matches(SKIP)) return []
  return [...run(collect(run(project(root)), literal, 0)).ranges.values()]
}

function ensureHighlightStyle(document: Document) {
  if (document.getElementById(STYLE_ID)) return
  const style = document.createElement('style')
  style.id = STYLE_ID
  style.textContent = `
::highlight(${MATCH_NAME}) { background-color: color-mix(in srgb, var(--warn) 28%, transparent); color: var(--text); }
::highlight(${ACTIVE_NAME}) { background-color: color-mix(in srgb, var(--accent) 48%, var(--bg-elevated)); color: var(--text); }
`
  document.head.append(style)
}

export function createChatFindHighlighter(onInvalidate?: () => void) {
  let owned: { registry: HighlightRegistry; matches: unknown; active: unknown } | undefined
  let cache: { root: HTMLElement; query: string; projection: Projection; collected: Collected; source?: string; sourceCount?: number } | undefined
  let observer: MutationObserver | undefined, version = 0
  function unpublish() {
    if (!owned) return
    // A departing cached view must not erase highlights published by the next session.
    if (owned.registry.get(MATCH_NAME) === owned.matches) owned.registry.delete(MATCH_NAME)
    if (owned.registry.get(ACTIVE_NAME) === owned.active) owned.registry.delete(ACTIVE_NAME)
    owned = undefined
  }
  function clear(preserveCache = false) {
    version++
    unpublish()
    if (!preserveCache) { observer?.disconnect(); observer = undefined; cache = undefined }
  }
  function relevant(records: MutationRecord[]) {
    if (!cache) return true
    const bodies = cache.projection.bodies
    return records.some(record => {
      const target = record.target.nodeType === 1 ? record.target as HTMLElement : record.target.parentElement
      if (!target) return false
      if (bodies.some(body => body === target || body.contains(target))) return true
      if (record.type === 'attributes' && bodies.some(body => target.contains(body))) return true
      return record.type === 'childList' && [...record.addedNodes, ...record.removedNodes].some(node =>
        node.nodeType === 1 && ((node as Element).matches('[data-chat-find-body],.assistant-answer')
          || (node as Element).querySelector('[data-chat-find-body]') || bodies.some(body => node.contains(body))))
    })
  }
  function invalidate(records: MutationRecord[]) {
    if (!relevant(records)) return
    version++; cache = undefined
    onInvalidate?.()
  }
  function watch(root: HTMLElement) {
    observer?.disconnect()
    observer = new MutationObserver(invalidate)
    observer.observe(root, { childList: true, subtree: true, characterData: true, attributes: true,
      attributeFilter: ['hidden', 'aria-hidden', 'open', 'class'] })
  }
  function publish(root: HTMLElement, collected: Collected, activeIndex: number): ChatFindHighlights {
    unpublish()
    const ranges = [...collected.ranges.values()]
    const activeRange = collected.ranges.get(activeIndex) ?? null
    const view = root.ownerDocument.defaultView as (Window & {
      CSS?: { highlights?: HighlightRegistry }
      Highlight?: HighlightConstructor
    }) | null
    const registry = view?.CSS?.highlights
    const Highlight = view?.Highlight
    const supported = Boolean(registry && Highlight)
    if (registry && Highlight && ranges.length) {
      ensureHighlightStyle(root.ownerDocument)
      const matches = new Highlight(...ranges)
      const active = new Highlight(...(activeRange ? [activeRange] : []))
      matches.priority = 0
      active.priority = 1
      registry.set(MATCH_NAME, matches)
      registry.set(ACTIVE_NAME, active)
      owned = { registry, matches, active }
    }
    return {
      ranges,
      totalMatches: collected.totalMatches,
      activeRange,
      activeRect: activeRange && typeof activeRange.getBoundingClientRect === 'function'
        ? activeRange.getBoundingClientRect() : null,
      supported,
      fallbackTarget: !supported && activeRange ? root : null,
    }
  }
  function update(root: HTMLElement, query: string, activeIndex = 0): ChatFindHighlights {
    const records = observer?.takeRecords()
    if (records?.length) invalidate(records)
    const literal = query.trim()
    if (!literal || root.matches(SKIP)) { clear(); return publish(root, { ranges: new Map(), totalMatches: 0 }, -1) }
    if (!cache || cache.root !== root || cache.query !== literal) {
      const projection = run(project(root))
      cache = { root, query: literal, projection, collected: run(collect(projection, literal, activeIndex)) }
      watch(root)
    } else if (activeIndex >= 0 && activeIndex < cache.collected.totalMatches && !cache.collected.ranges.has(activeIndex)) {
      cache.collected = run(collect(cache.projection, literal, activeIndex))
    }
    return publish(root, cache.collected, activeIndex)
  }
  async function updateAsync(root: HTMLElement, query: string, activeIndex = 0, options: { sourceText?: string; signal?: AbortSignal } = {}): Promise<ChatFindHighlights | null> {
    const records = observer?.takeRecords()
    if (records?.length) invalidate(records)
    const ownVersion = ++version, literal = query.trim()
    const current = () => version === ownVersion && !options.signal?.aborted
    if (!literal || root.matches(SKIP)) { clear(); return publish(root, { ranges: new Map(), totalMatches: 0 }, -1) }
    if (!cache || cache.root !== root || cache.query !== literal) {
      // Observe before yielding, so streamed content cannot publish stale node offsets.
      cache = undefined
      watch(root)
      const projection = await runCooperatively(project(root), current)
      if (!projection || !current()) return null
      const collected = await runCooperatively(collect(projection, literal, activeIndex), current)
      if (!collected || !current()) return null
      cache = { root, query: literal, projection, collected }
    } else if (activeIndex >= 0 && activeIndex < cache.collected.totalMatches && !cache.collected.ranges.has(activeIndex)) {
      const collected = await runCooperatively(collect(cache.projection, literal, activeIndex), current)
      if (!collected || !current()) return null
      cache.collected = collected
    }
    if (options.sourceText !== undefined && (cache.source !== options.sourceText || cache.sourceCount === undefined)) {
      function* countSource() {
        let count = 0
        for (const match of literalMatches(options.sourceText!, literal)) { if (match) count++; else yield }
        return count
      }
      const count = await runCooperatively(countSource(), current)
      if (count === undefined || !current() || !cache) return null
      cache.source = options.sourceText; cache.sourceCount = count
    }
    if (!current() || !cache) return null
    const exact = options.sourceText === undefined || cache.sourceCount === cache.collected.totalMatches
    return publish(root, cache.collected, exact ? activeIndex : -1)
  }
  return { update, updateAsync, clear }
}
