<template>
  <div ref="rootEl" class="msg-ai-text streaming-text-part">
    <template v-for="block in displayedBlocks" :key="block.key">
      <VisualizeCanvas
        v-if="block.kind === 'visualize'"
        :receipt="block.receipt"
        :session-key="sessionKey ?? ''"
      />
      <div
        v-else-if="block.kind === 'rich'"
        class="streaming-rich-block"
        v-html="block.html"
      />
      <span v-else class="streaming-plain-block">{{ block.text }}</span>
    </template>
    <span v-if="tail" class="streaming-tail">{{ tail }}</span>
  </div>
</template>

<script setup lang="ts">
import VisualizeCanvas from '@/components/chat/VisualizeCanvas.vue'
import { splitVisualizeBody, type VisualizeBodySegment } from '@/visualize/receipts'
import type { VisualizeReceipt } from '@/visualize/types'
import { computed, ref, watch } from 'vue'
import type { RenderMarkdownOptions } from '@/composables/chat/useChatTextRendering'
import { useMarkdownEnhancements } from '@/composables/chat/useMarkdownEnhancements'
import { createMarkdownRenderBudget } from '@/utils/markdown/renderCore'
import { preloadMermaid } from '@/utils/markdown/mermaidRenderer'

const OPEN_BLOCK_LIMIT = 16 * 1024
const PLAIN_CHUNK_SIZE = 8 * 1024

const props = defineProps<{
  sessionKey?: string
  visualizeReceipts?: VisualizeReceipt[]
  rawText: string
  renderMarkdown: (text: string, opts?: RenderMarkdownOptions) => string
}>()

interface RichBlock {
  key: string
  kind: 'rich'
  html: string
}

interface PlainBlock {
  key: string
  kind: 'plain'
  text: string
}

type CommittedBlock = RichBlock | PlainBlock
type DisplayedBlock = CommittedBlock | { key: string; kind: 'visualize'; receipt: VisualizeReceipt }

// Rich Markdown blocks and bounded plain-text fallbacks share one ordered
// sequence. A very long open block can be frozen as plain text and later close
// into rich Markdown; separate arrays would reorder that later rich suffix
// ahead of its already-displayed prefix.
const committedBlocks = ref<CommittedBlock[]>([])
const tail = ref('')
const rootEl = ref<HTMLDivElement | null>(null)
const resetEpoch = ref(0)
let renderBudget = createMarkdownRenderBudget()
const receiptVersion = computed(() => JSON.stringify(props.visualizeReceipts ?? []))
const splitCache = new Map<string, { version: string; segments: VisualizeBodySegment[] }>()
const displayedBlocks = computed<DisplayedBlock[]>(() => {
  const displayed: DisplayedBlock[] = []
  const used = new Set<string>()
  for (const block of committedBlocks.value) {
    if (block.kind !== 'rich' || !props.visualizeReceipts?.length) {
      displayed.push(block)
      continue
    }
    let cached = splitCache.get(block.key)
    if (!cached || cached.version !== receiptVersion.value) {
      cached = { version: receiptVersion.value, segments: splitVisualizeBody(block.html, props.visualizeReceipts, false) }
      splitCache.set(block.key, cached)
    }
    for (const segment of cached.segments) {
      if (segment.receipt) {
        if (used.has(segment.receipt.marker)) continue
        used.add(segment.receipt.marker)
        displayed.push({ key: `${block.key}:${segment.key}`, kind: 'visualize', receipt: segment.receipt })
      } else {
        displayed.push({ key: `${block.key}:${segment.key}`, kind: 'rich', html: segment.html })
      }
    }
  }
  return displayed
})

const enhancements = useMarkdownEnhancements(rootEl, {
  version: () => committedBlocks.value.length,
  incremental: true,
  resetVersion: () => resetEpoch.value,
})
// A late receipt can split an already committed subtree. Restore existing
// diagrams before re-admitting it; no Markdown parse or fresh render budget.
watch(receiptVersion, () => enhancements.refresh(), { flush: 'post' })

let acceptedRaw = ''
let committedOffset = 0
let scanOffset = 0
let fence: { character: string; length: number } | null = null
let displayMath: '$$' | '\\[' | null = null
let blockSequence = 0

function resetState(): void {
  committedBlocks.value = []
  tail.value = ''
  acceptedRaw = ''
  committedOffset = 0
  scanOffset = 0
  fence = null
  displayMath = null
  resetEpoch.value += 1
  renderBudget = createMarkdownRenderBudget()
  splitCache.clear()
}

function appendRichBlock(raw: string, endOffset: number): void {
  if (endOffset <= committedOffset) return
  const blockText = raw.slice(committedOffset, endOffset)
  if (blockText) {
    committedBlocks.value.push({
      key: `rich-${blockSequence++}`,
      kind: 'rich',
      // Immutable commits share one budget for the entire live answer. Once
      // exhausted, later blocks retain complete escaped source.
      html: props.renderMarkdown(blockText, {
        highlight: true,
        cache: 'none',
        math: 'full',
        budget: renderBudget,
      }),
    })
  }
  committedOffset = endOffset
}

function unicodeSafeChunkEnd(raw: string, startOffset: number, requestedEnd: number): number {
  let endOffset = Math.min(raw.length, requestedEnd)
  if (endOffset <= startOffset || endOffset >= raw.length) return endOffset
  const preceding = raw.charCodeAt(endOffset - 1)
  const following = raw.charCodeAt(endOffset)
  const splitsSurrogatePair = preceding >= 0xD800 && preceding <= 0xDBFF
    && following >= 0xDC00 && following <= 0xDFFF
  if (splitsSurrogatePair) endOffset -= 1
  return endOffset
}

function freezeLongOpenTail(raw: string): void {
  while (raw.length - committedOffset > OPEN_BLOCK_LIMIT) {
    const endOffset = unicodeSafeChunkEnd(
      raw,
      committedOffset,
      committedOffset + PLAIN_CHUNK_SIZE,
    )
    committedBlocks.value.push({
      key: `plain-${blockSequence++}`,
      kind: 'plain',
      text: raw.slice(committedOffset, endOffset),
    })
    committedOffset = endOffset
    // The scanner has already consumed complete lines. If the frozen chunk
    // cuts across its pending incomplete line, resume at the new boundary.
    scanOffset = Math.max(scanOffset, committedOffset)
  }
}

function scanDisplayMath(line: string): void {
  for (const marker of line.matchAll(/\$\$|\\\[|\\\]/g)) {
    const offset = marker.index ?? 0
    let escapes = 0
    for (let index = offset - 1; index >= 0 && line[index] === '\\'; index--) escapes += 1
    if (escapes % 2 === 1) continue
    if (!displayMath && (marker[0] === '$$' || marker[0] === '\\[')) displayMath = marker[0]
    else if ((displayMath === '$$' && marker[0] === '$$') || (displayMath === '\\[' && marker[0] === '\\]')) displayMath = null
  }
}

function update(raw: string): void {
  raw = String(raw || '')
  if (!raw.startsWith(acceptedRaw)) resetState()
  acceptedRaw = raw

  while (scanOffset < raw.length) {
    const newline = raw.indexOf('\n', scanOffset)
    if (newline < 0) break
    const lineEnd = newline + 1
    const line = raw.slice(scanOffset, newline)
    const trimmed = line.trim()

    if (fence) {
      const closing = /^ {0,3}(`+|~+)[ \t\r]*$/.exec(line)?.[1]
      if (closing && closing[0] === fence.character && closing.length >= fence.length) fence = null
    } else if (displayMath) {
      scanDisplayMath(line)
    } else {
      const opening = /^ {0,3}(`{3,}|~{3,})([^\r]*)\r?$/.exec(line)
      if (opening && (opening[1]![0] !== '`' || !opening[2]!.includes('`'))) {
        fence = { character: opening[1]![0]!, length: opening[1]!.length }
        if (/^\s*mermaid(?:\s|$)/i.test(opening[2]!)) preloadMermaid()
      } else {
        scanDisplayMath(line)
      }
    }

    scanOffset = lineEnd
    // Keep the separator boundary: closing a fence alone can still be inside
    // a continuous Markdown list, whose next item must share the same parse.
    if (!trimmed && !fence && !displayMath) appendRichBlock(raw, lineEnd)
  }

  freezeLongOpenTail(raw)
  tail.value = raw.slice(committedOffset)
}

watch(() => props.rawText, update, { immediate: true })
</script>

<style scoped>
.msg-ai-text {
  margin-bottom: var(--sp-3);
  color: var(--text);
  font-size: var(--fs-md);
  line-height: 1.7;
  word-break: break-word;
}

/* A committed block ends with the shared 12px Markdown margin, including at
   the boundary with the next block. Do not add a second container gap. */
.streaming-rich-block :deep(> :first-child) { margin-top: 0; }
/* Settled prose starts a paragraph with a 12px margin. Keep that same
   boundary before the open/plain tail; shorter table/heading margins would
   otherwise move the text when the live answer becomes canonical. */
.streaming-rich-block:has(+ .streaming-plain-block, + .streaming-tail) {
  margin-bottom: 0.75rem;
}
.streaming-rich-block :deep(pre) {
  margin: 0.75rem 0;
  padding: 0.75rem 0.875rem 0.875rem;
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  line-height: 1.65;
  overflow-x: auto;
  border: 1px solid var(--code-block-border);
  border-radius: var(--radius-control);
  background: var(--code-block-bg);
}
.streaming-rich-block :deep(:not(pre) > code) {
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  padding: 0.0625rem 0.25rem;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 0.9em;
}
.streaming-rich-block :deep(pre code) {
  font: inherit;
  background: transparent;
  padding: 0;
  color: var(--text);
}

.streaming-plain-block,
.streaming-tail {
  white-space: pre-wrap;
}
</style>
