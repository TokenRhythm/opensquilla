import { computed, createApp, defineComponent, h, nextTick, provide, ref } from 'vue'
import { createPinia } from 'pinia'
import TextPart from '@/components/chat/parts/TextPart.vue'
import StreamingTextPart from '@/components/chat/parts/StreamingTextPart.vue'
import ActivityNarration from '@/components/chat/ActivityNarration.vue'
import ArtifactPreviewPanel from '@/components/workbench/ArtifactPreviewPanel.vue'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import { createV4ArtifactPreviews } from '@/adapters/gateway/artifactPreviewsV4'
import { ARTIFACT_WORKBENCH_KEY, type ArtifactWorkbench } from '@/modules/artifactWorkbench'
import { httpBinaryResponse, httpTransportTestDouble } from '@/testing/httpTransport.test-helper'
import i18n, { loadLocaleMessages } from '@/i18n'
import { getManifest, selectableValueThemes } from '@/themes/registry'
import type { ChatStreamTimelineItem } from '@/types/chat'
import type { ChatPart } from '@/types/parts'
import 'katex/dist/katex.min.css'
import '@/assets/base.css'
import '@/themes/tokens'
import '@/styles/control-visual-system.css'
import '@/styles/chat-markdown.css'
import '@/styles/chat-diagrams.css'
import '@/styles/chat-shared.css'
import '@/styles/apple-modern.css'

declare global {
  interface Window {
    richtextPreview: {
      sample: string; diagram: string;
      setTheme: (id: string) => Promise<void>;
      setSurface: (value: 'chat' | 'process' | 'file') => Promise<void>;
      setContent: (value: string) => Promise<void>;
      setStreaming: (value: boolean) => Promise<void>;
    }
    richtextCopied?: string;
    richtextCatalogRequests: Array<{ session: string; limit?: number; singlePage?: boolean }>;
  }
}

// Test-only samples and transport. No profile, model, saved history, or external HTTP.
const tick = String.fromCharCode(96)
const fence = (language: string, source: string) => [tick.repeat(3) + language, source, tick.repeat(3)].join('\n')
const diagram = 'flowchart LR\n  A[解析内容] --> B[安全渲染]\n  B --> C[保留原文]'
const sample = [
  '## 数学公式',
  '数字开头：$2^n$；含括号：\\(f(x)=x^2\\)。',
  '$$\\sqrt{x}+\\overbrace{a+b}^{c}+\\widehat{ABC}$$',
  '## 代码原文',
  ['~~~latex', '$x^2$ 和 \\(f(x)\\) 保持源码', '~~~'].join('\n'),
  '## 表格与编号',
  '| 左对齐 | 居中 | 右对齐 |\n| :--- | :---: | ---: |\n| 文本 | 已确认 | 128.50 |\n| 第二行 | 待处理 | 9.00 |',
  '3. 第三步\n4. 第四步',
  '- [x] 已完成\n- [ ] 待处理',
  '## 脚注',
  '正文引用[^note]，相同引用再次出现[^note]。',
  '[^note]: 注释正文保留，支持 **强调** 和 [来源链接](https://example.com)。',
  '## 图表与复制',
  fence('mermaid', diagram),
].join('\n\n')
const fileImage = '\n\n## 文件内配图\n\n![附件配图](chart.png)'

const canvas = document.createElement('canvas')
canvas.width = 400; canvas.height = 100
const context = canvas.getContext('2d')!
context.fillStyle = '#f4f4f4'; context.fillRect(0, 0, 400, 100)
context.fillStyle = '#333333'; context.font = '20px sans-serif'; context.fillText('报告配图 · 当前会话附件', 24, 56)
const imageBlob = await new Promise<Blob>(resolve => canvas.toBlob(blob => resolve(blob!), 'image/png'))
await loadLocaleMessages('zh-Hans')
i18n.global.locale.value = 'zh-Hans'

const Preview = defineComponent({
  setup() {
    const { renderMarkdown } = useChatTextRendering()
    const raw = ref(sample)
    const surface = ref<'chat' | 'process' | 'file'>('chat')
    const streaming = ref(false)
    const theme = ref('light')
    const version = ref(0)
    const http = httpTransportTestDouble({
      requestBinary: async endpoint => {
        if (endpoint.includes('richtext-image')) return httpBinaryResponse(imageBlob, { contentType: 'image/png' })
        if (endpoint.includes('richtext-report')) return httpBinaryResponse(raw.value + fileImage, { contentType: 'text/markdown' })
        throw new Error('Unexpected fixture resource: ' + endpoint)
      },
    })
    provide(ARTIFACT_WORKBENCH_KEY, {
      previews: createV4ArtifactPreviews(http, { baseOrigin: () => location.origin }),
      resources: {
        list: async (session: string, options: { limit?: number; singlePage?: boolean } = {}) => {
          window.richtextCatalogRequests.push({ session, limit: options.limit, singlePage: options.singlePage })
          return { totalCount: 1, resources: [{
            resource: { type: 'deliverable', artifactId: 'richtext-image' },
            name: 'docs/chart.png', mime: 'image/png', downloadUrl: '/api/v1/artifacts/richtext-image',
            capabilities: { download: true }, relations: {},
          }] }
        },
      },
    } as ArtifactWorkbench)
    window.richtextCatalogRequests = []
    const part = computed<Extract<ChatPart, { type: 'text' }>>(() => ({ key: 'preview-text', type: 'text', rawText: raw.value, html: renderMarkdown(raw.value) }))
    const narration = computed(() => ({ key: 'preview-process', type: 'text', text: raw.value, html: renderMarkdown(raw.value), activityOrder: 1 }) as Extract<ChatStreamTimelineItem, { type: 'text' }>)
    const setTheme = async (id: string) => {
      const manifest = getManifest(id)
      if (!manifest) throw new Error('Unknown theme')
      await Promise.all([manifest.world?.styles?.(), ...(manifest.world?.fonts?.map(font => font.load()) ?? [])])
      theme.value = id
      document.documentElement.dataset.theme = id
      document.documentElement.style.colorScheme = manifest.capabilities.colorScheme === 'light' ? 'light' : 'dark'
      await nextTick()
    }
    const controls = {
      sample, diagram,
      setTheme,
      setSurface: async (value: typeof surface.value) => { surface.value = value; await nextTick() },
      setContent: async (value: string) => { raw.value = value; version.value++; await nextTick() },
      setStreaming: async (value: boolean) => { streaming.value = value; await nextTick() },
    }
    Object.assign(window, { richtextPreview: controls })
    return () => h('main', { class: 'richtext-preview' }, [
      h('header', [
        h('h1', '富文本验收'),
        h('select', { 'aria-label': '验收位置', value: surface.value, onChange: (event: Event) => controls.setSurface((event.target as HTMLSelectElement).value as typeof surface.value) }, [h('option', { value: 'chat' }, '聊天正文'), h('option', { value: 'process' }, '过程正文'), h('option', { value: 'file' }, 'Markdown 文件')]),
        h('select', { 'aria-label': '主题', value: theme.value, onChange: (event: Event) => setTheme((event.target as HTMLSelectElement).value) }, selectableValueThemes.map(manifest => h('option', { value: manifest.id }, ['light', 'dark'].includes(manifest.id) ? i18n.global.t('chrome.themeMode.' + manifest.id) : manifest.name))),
        h('p', '示例数据 · 正式组件与预览适配器 · 不调用模型、不读写聊天记录'),
      ]),
      h('section', { 'data-surface': surface.value }, [surface.value === 'file'
        ? h(ArtifactPreviewPanel, { key: version.value, artifact: { id: 'richtext-report', name: 'docs/report.md', mime: 'text/markdown', download_url: '/api/v1/artifacts/richtext-report' }, sessionKey: 'richtext-preview', showHeader: false })
        : surface.value === 'process' ? h(ActivityNarration, { item: narration.value })
        : streaming.value ? h(StreamingTextPart, { rawText: raw.value, renderMarkdown })
        : h(TextPart, { part: part.value })]),
    ])
  },
})
createApp(Preview).use(createPinia()).use(i18n).mount('#app')
