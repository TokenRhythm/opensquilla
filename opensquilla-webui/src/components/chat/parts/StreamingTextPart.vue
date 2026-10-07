<template>
  <div ref="rootEl" class="msg-ai-text streaming-text-part">
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
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { RenderMarkdownOptions } from '@/composables/chat/useChatTextRendering'
import { decorateCodeBlocks } from '@/utils/chat/codeBlockChrome'
import { decorateMarkdownImages } from '@/utils/markdown/imageLightbox'
import { preloadMermaid, renderMermaidBlocks } from '@/utils/markdown/mermaidRenderer'

const OPEN_BLOCK_LIMIT = 16 * 1024
const PLAIN_CHUNK_SIZE = 8 * 1024

const props = defineProps<{
  rawText: string
  renderMarkdown: (text: string, opts?: RenderMarkdownOptions) => string
}>()

const { t } = useI18n()

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
const rootEl = ref<HTMLDivElement | null>(null)

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
      // A committed block renders exactly once — never re-parsed on later
      // flushes — so the one-time highlight/math cost is bounded per block
      // (oversized code falls back to plain text inside the renderer).
      html: props.renderMarkdown(blockText, {
        highlight: true,
        cache: 'none',
        math: 'full',
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

  // Start loading the mermaid chunk while the fence is still streaming so it
  // has usually arrived by the time the block commits and renders.
  if (raw.includes('```mermaid')) preloadMermaid()

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

// Committed blocks are stable (appended, keyed, never re-rendered), so one
// idempotent pass after each DOM flush adds the same chrome the settled
// TextPart gets: copy buttons, mermaid diagrams, click-to-zoom images.
function mermaidLabels() {
  return {
    zoomIn: t('chat.mermaid.zoomIn'),
    zoomOut: t('chat.mermaid.zoomOut'),
    reset: t('chat.mermaid.reset'),
    copyCode: t('chat.mermaid.copyCode'),
    copied: t('chat.copied'),
    copyFailed: t('chat.toast.copyFailed'),
    exportPng: t('chat.mermaid.exportPng'),
    loadFailed: t('chat.mermaid.loadFailed'),
    viewSource: t('chat.mermaid.viewSource'),
    syntaxErrorAt: (line: number, excerpt: string) => t('chat.mermaid.syntaxErrorAt', { line, excerpt }),
  }
}

function decorate(): void {
  const root = rootEl.value
  if (!root) return
  const labels = {
    copy: t('chat.copy'),
    copied: t('chat.copied'),
    copyFailed: t('chat.toast.copyFailed'),
  }
  for (const block of root.querySelectorAll<HTMLElement>('.streaming-rich-block')) {
    decorateCodeBlocks(block, labels)
    decorateMarkdownImages(block, t('chat.closePreview'))
    if (block.querySelector('code.language-mermaid')) {
      void renderMermaidBlocks(block, mermaidLabels()).catch(() => { /* failed fences keep their source */ })
    }
  }
}

watch(() => committedBlocks.value.length, decorate, { flush: 'post' })
onMounted(decorate)

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
