<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox, type UploadRequestOptions } from 'element-plus'
import { http } from '../api/http'
import { fmtTime } from '../utils/format'

const docs = ref<any[]>([])
const q = ref('')
const results = ref<any[]>([])
const searching = ref(false)

async function load() {
  docs.value = (await http.get('/kb/documents')).data
}
onMounted(load)

async function upload(opt: UploadRequestOptions) {
  const fd = new FormData()
  fd.append('file', opt.file)
  await http.post('/kb/documents', fd)
  ElMessage.success('已上传，正在后台切分和向量化')
  await load()
  // 切分是后台任务：过几秒再刷新一次，看到分块数
  setTimeout(load, 3000)
}

async function remove(row: any) {
  await ElMessageBox.confirm(`删除「${row.title}」？`, '删除文档', { type: 'warning' })
  await http.delete(`/kb/documents/${row.id}`)
  await load()
}

async function search() {
  if (!q.value.trim()) return
  searching.value = true
  try {
    results.value = (await http.get('/kb/search', { params: { q: q.value } })).data
  } finally {
    searching.value = false
  }
}
</script>

<template>
  <div class="page">
    <h1 class="page-title">知识库</h1>
    <p class="muted">上传你自己的八股笔记（Markdown）。「八股联系项目」模式会检索这些笔记，把知识点和你的项目代码联系起来提问。</p>
    <el-upload :http-request="upload" :show-file-list="false" accept=".md,.markdown,.txt">
      <el-button type="primary">上传 Markdown</el-button>
    </el-upload>
    <el-table :data="docs" style="margin-top: 12px" empty-text="还没有文档">
      <el-table-column label="标题" prop="title" />
      <el-table-column label="文件名" prop="filename" />
      <el-table-column label="分块数" prop="chunk_count" width="90" />
      <el-table-column label="上传时间" width="160"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
      <el-table-column width="80"><template #default="{ row }"><el-button link type="danger" @click="remove(row)">删除</el-button></template></el-table-column>
    </el-table>

    <h2 class="page-title" style="margin-top: 24px">检索测试</h2>
    <div class="flex">
      <el-input v-model="q" placeholder="例如：事务失效的场景" @keyup.enter="search" />
      <el-button :loading="searching" @click="search">检索</el-button>
    </div>
    <p class="muted">关键词（BM25）和向量两路检索，用 RRF 合并排名。</p>
    <el-card v-for="r in results" :key="r.chunk_id" style="margin-bottom: 10px">
      <div class="flex">
        <b class="grow">{{ r.title }} › {{ r.heading_path }}</b>
        <span class="muted">关键词第 {{ r.bm25_rank ?? '-' }} · 向量第 {{ r.vector_rank ?? '-' }} · RRF {{ r.score }}</span>
      </div>
      <div style="white-space: pre-wrap; margin-top: 8px" class="muted">{{ r.content }}</div>
    </el-card>
  </div>
</template>
