<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'

const props = defineProps<{ source: string }>()

// html: false —— 不渲染 Markdown 里的原始 HTML；再过一遍 DOMPurify 兜底。
// 面试官和报告的内容来自模型，模型又读过不可信的仓库，所以要当作不可信输入处理（SPEC 18.1）。
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const html = computed(() => DOMPurify.sanitize(md.render(props.source || '')))
</script>

<template>
  <div class="md-body" v-html="html" />
</template>
