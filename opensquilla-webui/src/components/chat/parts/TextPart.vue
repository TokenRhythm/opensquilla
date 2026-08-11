<template>
  <div>
    <div ref="rootEl" class="msg-ai-text" v-html="part.html" />
    <p v-if="missingCitationLabel" class="msg-ai-citation-warning">
      Some citations do not map to available sources: {{ missingCitationLabel }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChatPart, SourcePart } from '@/types/parts'
import { decorateCitations } from '@/utils/chat/citations'
import { copyTextWithFallback } from '@/utils/browser'
import { usePlatform } from '@/platform'
import { requestBrowserWorkbenchOpen } from '@/workbench/browserItems'

const props = withDefaults(
  defineProps<{
    part: Extract<ChatPart, { type: 'text' }>
    sources?: SourcePart[]
  }>(),
  { sources: () => [] },
)

const emit = defineEmits<{ citation: [sourceId: number] }>()

const { t } = useI18n()
const platform = usePlatform()
const rootEl = ref<HTMLDivElement | null>(null)
const missingCitationIds = ref<number[]>([])

const missingCitationLabel = computed(() =>
  missingCitationIds.value.map(id => `[${id}]`).join(', '),
)

function labelFor(sourceId: number): string {
  const source = props.sources[sourceId - 1]
  return source ? source.title || source.domain : ''
}

function codeText(pre: HTMLPreElement): string {
  const code = pre.querySelector('code')
  return code?.textContent || ''
}

function hasCodeCopyButton(pre: HTMLPreElement): boolean {
  return Array.from(pre.children).some(child => child.classList.contains('code-copy-btn'))
}

function setCodeCopyButtonState(button: HTMLButtonElement, state: 'idle' | 'copied' | 'error') {
  const label = state === 'copied'
    ? t('chat.copied')
    : state === 'error'
      ? t('chat.toast.copyFailed')
      : t('chat.copy')
  button.replaceChildren(createCodeCopyIcon(state))
  button.title = label
  button.setAttribute('aria-label', label)
}

function createCodeCopyIcon(state: 'idle' | 'copied' | 'error'): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('width', '15')
  svg.setAttribute('height', '15')
  svg.setAttribute('aria-hidden', 'true')
  svg.setAttribute('focusable', 'false')
  svg.setAttribute('fill', 'none')
  svg.setAttribute('stroke', 'currentColor')
  svg.setAttribute('stroke-width', '2')
  svg.setAttribute('stroke-linecap', 'round')
  svg.setAttribute('stroke-linejoin', 'round')

  if (state === 'copied') {
    svg.appendChild(svgNode('polyline', { points: '20 6 9 17 4 12' }))
    return svg
  }
  if (state === 'error') {
    svg.appendChild(svgNode('path', { d: 'M18 6 6 18' }))
    svg.appendChild(svgNode('path', { d: 'm6 6 12 12' }))
    return svg
  }

  svg.appendChild(svgNode('rect', { width: '14', height: '14', x: '8', y: '8', rx: '2', ry: '2' }))
  svg.appendChild(svgNode('path', { d: 'M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2' }))
  return svg
}

function svgNode(tag: string, attrs: Record<string, string>): SVGElement {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag)
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value)
  return node
}

function decorateCodeBlocks() {
  const root = rootEl.value
  if (!root) return
  for (const pre of root.querySelectorAll<HTMLPreElement>('pre')) {
    if (hasCodeCopyButton(pre)) continue
    const text = codeText(pre)
    if (!text) continue

    pre.classList.add('code-block')
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'code-copy-btn'
    setCodeCopyButtonState(button, 'idle')
    button.addEventListener('click', async event => {
      event.preventDefault()
      event.stopPropagation()
      try {
        await copyTextWithFallback(codeText(pre))
        setCodeCopyButtonState(button, 'copied')
        button.classList.add('is-copied')
        window.setTimeout(() => {
          if (!button.isConnected) return
          setCodeCopyButtonState(button, 'idle')
          button.classList.remove('is-copied')
        }, 1600)
      } catch {
        setCodeCopyButtonState(button, 'error')
        button.classList.add('is-error')
        window.setTimeout(() => {
          if (!button.isConnected) return
          setCodeCopyButtonState(button, 'idle')
          button.classList.remove('is-error')
        }, 1600)
      }
    })
    pre.appendChild(button)
  }
}

function decorateBrowserLinks() {
  const root = rootEl.value
  if (
    !root
    || platform.id !== 'desktop'
    || !platform.capabilities.hasNativeWorkbenchSurfaces
  ) return
  for (const anchor of root.querySelectorAll<HTMLAnchorElement>('a[href]')) {
    if (!/^https?:/i.test(anchor.href)) continue
    const next = anchor.nextElementSibling
    if (next?.classList.contains('link-side-browser-btn')) continue
    const button = document.createElement('button')
    button.type = 'button'
    button.className = 'link-side-browser-btn'
    button.textContent = '↗'
    const label = t('workbench.browser.openSide')
    button.title = label
    button.setAttribute('aria-label', label)
    button.addEventListener('click', event => {
      event.preventDefault()
      event.stopPropagation()
      requestBrowserWorkbenchOpen(anchor.href)
    })
    anchor.insertAdjacentElement('afterend', button)
  }
}

// After `v-html` has applied the sanitized body, upgrade any `[n]` that maps to
// a real source into a focusable citation pill. The pass works on already-clean
// text nodes only (createElement/textContent — never innerHTML), so it adds no
// HTML sink and re-runs idempotently when the body re-renders during streaming.
function decorate() {
  const root = rootEl.value
  if (!root) return
  missingCitationIds.value = []
  decorateCitations(root, props.sources, {
    onActivate: n => emit('citation', n),
    labelFor,
    onMissingCitations: ids => {
      missingCitationIds.value = props.sources.length > 0 ? ids : []
    },
  })
  decorateCodeBlocks()
  decorateBrowserLinks()
}

onMounted(decorate)
watch(() => props.part.html, decorate, { flush: 'post' })
watch(() => props.sources, decorate, { flush: 'post' })
</script>

<style scoped>
.msg-ai-text {
  font-size: 0.875rem;
  line-height: 1.6;
  color: var(--text);
  word-break: break-word;
  margin-bottom: 0.5rem;
}

.msg-ai-text :deep(p) { margin: 0.375rem 0; }
.msg-ai-text :deep(p:first-child) { margin-top: 0; }
.msg-ai-text :deep(ul), .msg-ai-text :deep(ol) { margin: 0.375rem 0; padding-left: 1.25rem; }
.msg-ai-text :deep(li) { margin: 0.125rem 0; }
.msg-ai-text :deep(code) {
  background: var(--bg-hover);
  padding: 0.0625rem 0.25rem;
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  color: var(--text-muted);
}
.msg-ai-text :deep(pre) {
  background: var(--code-block-bg);
  border: 1px solid var(--code-block-border);
  border-radius: var(--radius-md);
  padding: 0.625rem;
  overflow-x: auto;
  margin: 0.375rem 0;
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--text) 4%, transparent);
}
.msg-ai-text :deep(pre.code-block) {
  position: relative;
  padding-top: 2.375rem;
  background: linear-gradient(
    to bottom,
    var(--code-block-header-bg) 0,
    var(--code-block-header-bg) 1.75rem,
    var(--code-block-bg) 1.75rem,
    var(--code-block-bg) 100%
  );
}

.msg-ai-text :deep(pre.code-block > .code-lang) {
  top: 0.375rem;
  right: 2.5rem;
  line-height: 1rem;
  background: transparent;
  color: var(--text-dim);
}

.msg-ai-text :deep(.code-copy-btn) {
  position: absolute;
  top: 0;
  right: 0.25rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.75rem;
  height: 1.75rem;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  opacity: 0.78;
  cursor: pointer;
  transition: color var(--transition), background var(--transition), opacity var(--transition);
}

.msg-ai-text :deep(.link-side-browser-btn) {
  display: inline-flex;
  width: 1.35rem;
  height: 1.35rem;
  align-items: center;
  justify-content: center;
  margin-left: 0.2rem;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  font: inherit;
  line-height: 1;
  vertical-align: text-bottom;
}

.msg-ai-text :deep(.link-side-browser-btn:hover),
.msg-ai-text :deep(.link-side-browser-btn:focus-visible) {
  background: var(--bg-hover);
  color: var(--accent);
}

.msg-ai-text :deep(.code-copy-btn svg) {
  display: block;
  width: 0.9375rem;
  height: 0.9375rem;
}

.msg-ai-text :deep(.code-copy-btn:hover) {
  color: var(--text);
  opacity: 1;
  background: var(--bg-hover);
}

.msg-ai-text :deep(.code-copy-btn:focus-visible) {
  outline: none;
  box-shadow: var(--focus-ring);
}

.msg-ai-text :deep(.code-copy-btn.is-copied) {
  color: var(--ok);
  opacity: 1;
}

.msg-ai-text :deep(.code-copy-btn.is-error) {
  color: var(--danger);
  opacity: 1;
}
.msg-ai-text :deep(pre code) {
  background: transparent;
  padding: 0;
}

.msg-ai-citation-warning {
  margin: 0.25rem 0 0.5rem;
  font-size: 0.75rem;
  line-height: 1.4;
  color: var(--text-muted);
}

/* Citation pills are injected outside Vue's template (built by decorateCitations
   with createElement), so the scoped data-v hash never lands on them — target
   them through :deep, the same mechanism the markdown elements above use. */
.msg-ai-text :deep(.citation-pill) {
  display: inline-flex;
  align-items: center;
  padding: 0 0.25rem;
  margin: 0 0.0625rem;
  font: inherit;
  font-family: var(--font-mono);
  font-size: 0.6875rem;
  line-height: 1.2;
  vertical-align: baseline;
  color: var(--text-muted);
  background: var(--bg-hover);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: color var(--transition), background var(--transition), border-color var(--transition);
}

.msg-ai-text :deep(.citation-pill:hover) {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--accent) 10%, var(--bg-hover));
}

.msg-ai-text :deep(.citation-pill:focus-visible) {
  outline: none;
  box-shadow: var(--focus-ring);
}

@media (prefers-reduced-motion: reduce) {
  .msg-ai-text :deep(.citation-pill) {
    transition: none;
  }
}
</style>
