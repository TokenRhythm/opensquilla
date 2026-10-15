/** Plain-text coordinates deliberately exclude the labels of capability references. */
export interface InlineSelection { start: number; end: number }
export interface InlineSnapshot {
  text: string
  positions: Record<string, number>
  offsets: WeakMap<Node, number[]>
}

export function readInlineEditor(root: HTMLElement): InlineSnapshot {
  let text = ''
  const positions: Record<string, number> = {}
  const offsets = new WeakMap<Node, number[]>()
  const visit = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const value = (node.textContent || '').replace(/\u00a0/g, ' ')
      offsets.set(node, Array.from({ length: value.length + 1 }, (_, i) => text.length + i))
      text += value
      return
    }
    if (!(node instanceof HTMLElement)) return
    const id = node.dataset.skillId
    if (id) {
      positions[id] = text.length
      offsets.set(node, Array(node.childNodes.length + 1).fill(text.length))
      return
    }
    if (node.tagName === 'BR') {
      offsets.set(node, [text.length])
      // Browsers add a terminal BR as an editing placeholder. It is not a
      // second newline, and an empty editor must stay an empty message.
      if (node.nextSibling) text += '\n'
      return
    }
    const boundaries = [text.length]
    for (const child of node.childNodes) {
      if (child instanceof HTMLElement && /^(DIV|P|LI)$/.test(child.tagName)
        && text.length && !text.endsWith('\n')) text += '\n'
      boundaries[boundaries.length - 1] = text.length
      visit(child)
      boundaries.push(text.length)
    }
    offsets.set(node, boundaries)
  }
  visit(root)
  return { text, positions, offsets }
}

export function editorSelection(root: HTMLElement, snapshot = readInlineEditor(root)): InlineSelection | null {
  const selection = root.ownerDocument.getSelection()
  if (!selection?.rangeCount) return null
  const range = selection.getRangeAt(0)
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null
  const start = snapshot.offsets.get(range.startContainer)?.[range.startOffset]
  const end = snapshot.offsets.get(range.endContainer)?.[range.endOffset]
  if (start === undefined || end === undefined) return null
  return { start, end }
}

/** Place a caret after references at the same text offset, so typing remains natural. */
export function setEditorSelection(root: HTMLElement, start: number, end = start): void {
  const snapshot = readInlineEditor(root)
  const point = (requested: number): { node: Node; offset: number } => {
    const target = Math.max(0, Math.min(requested, snapshot.text.length))
    let result: { node: Node; offset: number } = { node: root, offset: 0 }
    const visit = (node: Node) => {
      if (node instanceof HTMLElement && node.dataset.skillId) return
      const values = snapshot.offsets.get(node)
      if (values) values.forEach((value, offset) => {
        if (value === target) result = { node, offset }
      })
      node.childNodes.forEach(visit)
    }
    visit(root)
    return result
  }
  const from = point(start)
  const to = point(Math.max(start, end))
  const range = root.ownerDocument.createRange()
  range.setStart(from.node, from.offset)
  range.setEnd(to.node, to.offset)
  const selection = root.ownerDocument.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
}

export function shiftInlineOffset(offset: number, before: string, after: string): number {
  let prefix = 0
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) prefix++
  let suffix = 0
  while (suffix < before.length - prefix && suffix < after.length - prefix
    && before[before.length - 1 - suffix] === after[after.length - 1 - suffix]) suffix++
  if (offset <= prefix) return offset
  if (offset >= before.length - suffix) return Math.max(prefix, offset + after.length - before.length)
  return after.length - suffix
}
