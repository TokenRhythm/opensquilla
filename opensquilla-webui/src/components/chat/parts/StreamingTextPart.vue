<template>
  <div class="msg-ai-text streaming-text-part">
    <template v-for="block in committedBlocks" :key="block.key">
      <div
        v-if="block.kind === 'rich'"
        class="streaming-rich-block"
        v-html="block.html"
      />
      <span v-else class="streaming-plain-block">{{ block.text }}</span>
    </template>
    <span v-if="tail" class="streaming-tail">{{ tail }}</span>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import type { RenderMarkdownOptions } from '@/composables/chat/useChatTextRendering'

const OPEN_BLOCK_LIMIT = 16 * 1024
const PLAIN_CHUNK_SIZE = 8 * 1024

const props = defineProps<{
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

// Rich Markdown blocks and bounded plain-text fallbacks share one ordered
// sequence. A very long open block can be frozen as plain text and later close
// into rich Markdown; separate arrays would reorder that later rich suffix
// ahead of its already-displayed prefix.
const committedBlocks = ref<CommittedBlock[]>([])
const tail = ref('')

let acceptedRaw = ''
let committedOffset = 0
let scanOffset = 0
let inFence = false
let inDisplayMath = false
let blockSequence = 0

function resetState(): void {
  committedBlocks.value = []
  tail.value = ''
  acceptedRaw = ''
  committedOffset = 0
  scanOffset = 0
  inFence = false
  inDisplayMath = false
  blockSequence = 0
}

function appendRichBlock(raw: string, endOffset: number): void {
  if (endOffset <= committedOffset) return
  const blockText = raw.slice(committedOffset, endOffset)
  if (blockText) {
    committedBlocks.value.push({
      key: `rich-${blockSequence++}`,
      kind: 'rich',
      html: props.renderMarkdown(blockText, {
        highlight: false,
        cache: 'none',
        math: 'defer',
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

    if (/^(?:```|~~~)/.test(trimmed)) {
      inFence = !inFence
    } else if (!inFence) {
      const mathMarkers = line.match(/\$\$/g)?.length ?? 0
      if (mathMarkers % 2 === 1) inDisplayMath = !inDisplayMath
    }

    scanOffset = lineEnd
    if (!trimmed && !inFence && !inDisplayMath) appendRichBlock(raw, lineEnd)
  }

  freezeLongOpenTail(raw)
  tail.value = raw.slice(committedOffset)
}

watch(() => props.rawText, update, { immediate: true })
</script>

<style scoped>
.msg-ai-text {
  margin-bottom: 0.5rem;
  color: var(--text);
  font-size: 0.875rem;
  line-height: 1.6;
  word-break: break-word;
}

.streaming-rich-block :deep(p) { margin: 0.375rem 0; }
.streaming-rich-block :deep(p:first-child) { margin-top: 0; }
.streaming-rich-block :deep(ul),
.streaming-rich-block :deep(ol) { margin: 0.375rem 0; padding-left: 1.25rem; }
.streaming-rich-block :deep(pre) {
  margin: 0.375rem 0;
  overflow-x: auto;
  border: 1px solid var(--code-block-border);
  border-radius: var(--radius-md);
  background: var(--code-block-bg);
  padding: 0.625rem;
}
.streaming-rich-block :deep(code) {
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  padding: 0.0625rem 0.25rem;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 0.8125rem;
}

.streaming-plain-block,
.streaming-tail {
  white-space: pre-wrap;
}
</style>
