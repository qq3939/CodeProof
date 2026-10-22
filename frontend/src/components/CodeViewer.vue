<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

const props = defineProps<{
  content: string
  language?: string
  start?: number | null
  end?: number | null
  maxHeight?: string
}>()

const html = ref('')
const box = ref<HTMLElement>()

const LANG: Record<string, string> = {
  java: 'java', kt: 'kotlin', xml: 'xml', yml: 'yaml', yaml: 'yaml', properties: 'ini', md: 'markdown',
  json: 'json', sql: 'sql', js: 'javascript', ts: 'typescript', vue: 'vue', py: 'python', sh: 'bash',
  gradle: 'groovy', html: 'html', css: 'css', go: 'go',
}

function escape(s: string) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// 超大的文件不做语法高亮：Shiki 在浏览器里给几万行上色会卡住页面
const HIGHLIGHT_LIMIT = 300 * 1024

async function render() {
  const s = props.start ?? 0
  const e = props.end ?? s
  const inRange = (n: number) => s > 0 && n >= s && n <= e
  const lang = LANG[props.language || ''] || 'text'
  if (props.content.length > HIGHLIGHT_LIMIT || lang === 'text') {
    const lines = props.content.split('\n').map(
      (l, i) => `<span class="line${inRange(i + 1) ? ' hl' : ''}">${escape(l)}</span>`,
    )
    html.value = `<pre class="shiki"><code>${lines.join('\n')}</code></pre>`
  } else {
    // 动态导入：Shiki 体积大，只有打开代码时才加载
    const { codeToHtml } = await import('shiki')
    html.value = await codeToHtml(props.content, {
      lang,
      themes: { light: 'github-light', dark: 'github-dark' },
      transformers: [
        {
          line(node, n) {
            if (inRange(n)) this.addClassToHast(node, 'hl')
          },
        },
      ],
    })
  }
  await nextTick()
  // 把证据行滚动到视口中间，让用户一眼看到"考官说的是哪几行"
  box.value?.querySelector('.line.hl')?.scrollIntoView({ block: 'center' })
}

watch(() => [props.content, props.start, props.end, props.language], render, { immediate: true })
</script>

<template>
  <div ref="box" class="code-view" :style="{ maxHeight: maxHeight || '70vh', overflow: 'auto' }" v-html="html" />
</template>
