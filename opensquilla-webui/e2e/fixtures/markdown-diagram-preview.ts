import { computed, createApp, defineComponent, h, nextTick, onBeforeUnmount, ref } from 'vue'
import { createPinia } from 'pinia'
import TextPart from '@/components/chat/parts/TextPart.vue'
import StreamingTextPart from '@/components/chat/parts/StreamingTextPart.vue'
import { useChatTextRendering } from '@/composables/chat/useChatTextRendering'
import i18n, { loadLocaleMessages } from '@/i18n'
import { getManifest, selectableValueThemes } from '@/themes/registry'
import type { ChatPart } from '@/types/parts'
import 'katex/dist/katex.min.css'
import '@/assets/base.css'
import '@/themes/tokens'
import '@/styles/control-visual-system.css'
import '@/styles/chat-markdown.css'
import '@/styles/chat-diagrams.css'
import '@/styles/chat-shared.css'
import '@/styles/apple-modern.css'

// Development-only fixtures: these are sample Markdown messages, never tool
// calls or saved conversation records. Production build inputs exclude e2e/.
const fence = (language: string, source: string) => `\`\`\`${language}\n${source}\n\`\`\``
const flowchart = `### Mermaid · 流程图

完成的回答直接显示图表，原始 Markdown 保留不变。

${fence('mermaid', `flowchart LR
  A[收到请求] --> B[执行任务]
  B --> C{结果通过?}
  C -->|是| D[返回结果]
  C -->|否| E[修正并重试]
  E --> B`)}
`
const sequence = `### Mermaid · 时序图

${fence('mermaid', `sequenceDiagram
  participant User as 用户
  participant App as OpenSquilla
  participant Model as 模型
  User->>App: 提交请求
  App->>Model: 发起生成
  Model-->>App: 返回 Markdown
  App-->>User: 显示图表`)}
`
const graphviz = `### Graphviz · 依赖图

${fence('dot', `digraph Dependencies {
  rankdir=LR;
  node [shape=box];
  Chat [label="Chat"];
  Markdown [label="Markdown"];
  Mermaid [label="Mermaid"];
  Graphviz [label="Graphviz"];
  SVG [label="SVG"];
  Chat -> Markdown;
  Markdown -> Mermaid;
  Markdown -> Graphviz;
  Mermaid -> SVG;
  Graphviz -> SVG;
}`)}
`
const samples = {
  overview: [flowchart, sequence, graphviz].join('\n'),
  flowchart,
  sequence,
  graphviz,
  error: `### 语法错误示例\n\n保留源码，并显示低调提示。\n\n${fence('mermaid', 'flowchart TD\nA[没有闭合的节点')}\n\n${fence('graphviz', 'digraph { Start -> }')}`,
  oversized: `### 超出渲染预算示例\n\n此示例包含过多连线，保留源码。\n\n${fence('mermaid', `flowchart LR\n${Array.from({ length: 260 }, (_, index) => `n${index}`).join(' --> ')}`)}`,
} as const
type Sample = keyof typeof samples

declare global {
  interface Window {
    diagramPreview: {
      samples: Sample[]
      showSample: (sample: Sample) => Promise<void>
      setTheme: (theme: string) => Promise<void>
      setStreaming: (text: string) => Promise<void>
      appendStreaming: (text: string) => Promise<void>
      settle: (text?: string) => Promise<void>
      replay: (sample?: Sample, durationMs?: number) => void
      state: () => { sample: Sample; streaming: boolean; text: string; theme: string }
    }
  }
}

await loadLocaleMessages('zh-Hans')
i18n.global.locale.value = 'zh-Hans'

const Preview = defineComponent({
  setup() {
    const { renderMarkdown } = useChatTextRendering()
    const sample = ref<Sample>('overview')
    const raw = ref<string>(samples.overview)
    const streaming = ref(false)
    const theme = ref('light')
    let timer: ReturnType<typeof setInterval> | undefined
    const part = computed<Extract<ChatPart, { type: 'text' }>>(() => ({
      type: 'text', rawText: raw.value, html: renderMarkdown(raw.value),
    }))
    const stopReplay = () => { clearInterval(timer); timer = undefined }
    const setTheme = async (id: string) => {
      const manifest = getManifest(id)
      if (!manifest) throw new Error(`Unknown preview theme: ${id}`)
      await Promise.all([
        manifest.world?.styles?.(),
        ...(manifest.world?.fonts?.map(font => font.load()) ?? []),
      ])
      theme.value = id
      document.documentElement.dataset.theme = id
      document.documentElement.style.colorScheme = manifest.capabilities.colorScheme === 'light' ? 'light' : 'dark'
      await nextTick()
    }
    const showSample = async (id: Sample) => {
      if (!(id in samples)) throw new Error(`Unknown preview sample: ${id}`)
      stopReplay()
      sample.value = id
      raw.value = samples[id]
      streaming.value = false
      await nextTick()
    }
    const replay = (id: Sample = sample.value, durationMs = 4200) => {
      stopReplay()
      sample.value = id
      const source = samples[id]
      raw.value = ''
      streaming.value = true
      let offset = 0
      const stride = Math.max(1, Math.ceil(source.length / Math.max(1, durationMs / 80)))
      timer = setInterval(() => {
        offset = Math.min(source.length, offset + stride)
        raw.value = source.slice(0, offset)
        if (offset === source.length) {
          stopReplay()
          streaming.value = false
        }
      }, 80)
    }
    window.diagramPreview = {
      samples: Object.keys(samples) as Sample[], showSample, setTheme, replay,
      setStreaming: async text => { stopReplay(); raw.value = text; streaming.value = true; await nextTick() },
      appendStreaming: async text => { raw.value += text; streaming.value = true; await nextTick() },
      settle: async text => { stopReplay(); if (text !== undefined) raw.value = text; streaming.value = false; await nextTick() },
      state: () => ({ sample: sample.value, streaming: streaming.value, text: raw.value, theme: theme.value }),
    }
    onBeforeUnmount(stopReplay)
    return () => h('main', { class: 'diagram-preview' }, [
      h('header', { class: 'diagram-preview__header' }, [
        h('div', [
          h('h1', { class: 'diagram-preview__title' }, '正文图表'),
          h('p', { class: 'diagram-preview__caption' }, '示例数据 · 使用正式聊天组件 · 不发起模型或工具调用'),
        ]),
        h('div', { class: 'diagram-preview__controls' }, [
          h('select', { 'aria-label': '主题', value: theme.value,
            onChange: (event: Event) => { void setTheme((event.target as HTMLSelectElement).value) } },
          selectableValueThemes.map(manifest => h('option', { value: manifest.id },
            manifest.id === 'light' ? '浅色' : manifest.id === 'dark' ? '深色' : manifest.name))),
          h('button', { type: 'button', onClick: () => replay() }, streaming.value ? '重新播放' : '重播生成'),
        ]),
      ]),
      h('div', { class: 'diagram-preview__status', 'aria-live': 'polite', 'data-preview-status': streaming.value ? 'streaming' : 'settled' },
        streaming.value ? '模拟生成中 · 暂时显示源码' : '已完成 · 图表按可见区域渲染'),
      h('section', { 'data-preview-content': '' }, [streaming.value
        ? h(StreamingTextPart, { rawText: raw.value, renderMarkdown })
        : h(TextPart, { part: part.value })]),
    ])
  },
})

createApp(Preview).use(createPinia()).use(i18n).mount('#app')
