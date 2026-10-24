<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type Node from 'element-plus/es/components/tree/src/model/node'
import { http } from '../api/http'
import { jobEvents } from '../api/sse'
import { GENERIC_MODE_NOTE, KIND_LABEL, STATUS_LABEL, type Repo, type Snapshot, type Topic } from '../api/types'
import { fmtBytes, fmtTime, shortSha } from '../utils/format'
import CodeViewer from '../components/CodeViewer.vue'
import MarkdownView from '../components/MarkdownView.vue'
import EvidenceLink from '../components/EvidenceLink.vue'

const props = defineProps<{ id: string }>()
const route = useRoute()
const router = useRouter()

const repo = ref<Repo | null>(null)
const snapshots = ref<Snapshot[]>([])
const snapId = ref<string>('')
const snap = ref<Snapshot | null>(null)
const profile = ref<any>(null)
const summary = ref('')
const topics = ref<Topic[]>([])
const tab = ref((route.query.tab as string) || 'overview')
let es: EventSource | null = null

const USAGE_LABEL: Record<string, string> = {
  transaction: '事务', cache: '缓存', redis: 'Redis', mq: '消息', scheduled: '定时任务', async: '异步', concurrency: '并发',
}
// AI 应用画像（D-023）：八类写法 + 风险点，和"关键用法"一样按类折叠展示
const AI_LABEL: Record<string, string> = {
  risks: '风险点', tools: '工具调用', agent_loops: 'Agent 循环', graphs: '状态图', multi_agent: '多 Agent',
  mcp: 'MCP', rag: 'RAG', context: '上下文与安全', llm_calls: '调用工程化',
}

async function loadRepo() {
  const [r, s] = await Promise.all([http.get(`/repos/${props.id}`), http.get(`/repos/${props.id}/snapshots`)])
  repo.value = r.data
  snapshots.value = s.data
  if (!snapId.value) snapId.value = (route.query.snapshot as string) || r.data.latest_snapshot?.id || s.data[0]?.id
}

async function loadSnapshot() {
  es?.close()
  profile.value = null
  topics.value = []
  if (!snapId.value) return
  const { data } = await http.get<Snapshot>(`/snapshots/${snapId.value}`)
  snap.value = data
  if (data.status === 'ready') {
    await loadResults()
  } else if (data.status !== 'failed') {
    // 分析是后台任务：订阅进度 SSE，完成后再拉结果
    es = jobEvents(snapId.value, async (d) => {
      if (!snap.value) return
      Object.assign(snap.value, d)
      if (d.status === 'ready') await loadResults()
    })
  }
}

async function loadResults() {
  const [p, t] = await Promise.all([
    http.get(`/snapshots/${snapId.value}/profile`),
    http.get(`/snapshots/${snapId.value}/topics`),
  ])
  profile.value = p.data.profile
  summary.value = p.data.summary_md
  topics.value = t.data
  // 下拉框里的快照状态是进入页面时拉的，分析完成后刷新一下
  snapshots.value = (await http.get(`/repos/${props.id}/snapshots`)).data
}

onMounted(async () => {
  await loadRepo()
  await loadSnapshot()
})
onBeforeUnmount(() => es?.close())
watch(snapId, (_v, old) => old && loadSnapshot())
watch(tab, (t) => router.replace({ query: { ...route.query, tab: t } }))

async function refresh() {
  const { data } = await http.post(`/repos/${props.id}/refresh`)
  if (data.new_commit === false) ElMessage.info('远端没有新提交')
  else ElMessage.success('发现新提交，开始增量分析')
  await loadRepo()
  snapId.value = data.snapshot_id
}

function startExam(extra: Record<string, string> = {}) {
  router.push({ path: '/sessions/new', query: { snapshot: snapId.value, ...extra } })
}

// ---------------------------------------------------------------- 代码浏览
const file = ref<{ path: string; content: string; language: string } | null>(null)
const hl = ref<{ start: number | null; end: number | null }>({ start: null, end: null })
const fileLoading = ref(false)

async function openFile(path: string, start?: number | null, end?: number | null) {
  tab.value = 'code'
  fileLoading.value = true
  try {
    const { data } = await http.get(`/snapshots/${snapId.value}/file`, { params: { path } })
    file.value = data
    hl.value = { start: start ?? null, end: end ?? start ?? null }
    router.replace({ query: { ...route.query, tab: 'code', path, start: start || undefined, end: end || undefined } })
  } finally {
    fileLoading.value = false
  }
}

async function loadTree(node: Node, resolve: (d: any[]) => void) {
  const path = node.level === 0 ? '.' : node.data.path
  try {
    const { data } = await http.get(`/snapshots/${snapId.value}/tree`, { params: { path } })
    resolve(data.items.map((i: any) => ({ ...i, leaf: i.type === 'file' })))
  } catch {
    resolve([])
  }
}

watch(
  () => profile.value,
  (p) => {
    // 从别的页面带着 ?path=&start=&end= 跳过来时，直接打开那个文件并高亮
    if (p && route.query.path) {
      openFile(route.query.path as string, Number(route.query.start) || null, Number(route.query.end) || null)
    }
  },
)

const aiRows = computed(() => {
  const a = profile.value?.ai || {}
  return Object.keys(AI_LABEL).map((k) => ({ key: k, label: AI_LABEL[k], items: a[k] || [] }))
})
const aiCount = computed(() => aiRows.value.reduce((n, r) => n + r.items.length, 0))
const usageRows = computed(() => {
  const u = profile.value?.usages || {}
  return Object.keys(USAGE_LABEL).map((k) => ({ key: k, label: USAGE_LABEL[k], items: u[k] || [] }))
})
const statusType = (s?: string) => (s === 'ready' ? 'success' : s === 'failed' ? 'danger' : 'warning')
const LAYER_LABEL: Record<string, string> = {
  entry: '入口', dependency: '依赖注入', controller: 'Controller', service: 'Service', business: '业务', data: '数据访问',
  repository: '数据访问', middleware: '中间件', other: '其他',
}
</script>

<template>
  <div class="page">
    <div class="flex" style="margin-bottom: 12px; flex-wrap: wrap">
      <h1 class="page-title grow" style="margin: 0">{{ repo ? `${repo.owner}/${repo.name}` : '' }}</h1>
      <el-select v-model="snapId" style="width: 300px" placeholder="选择快照">
        <el-option
          v-for="s in snapshots"
          :key="s.id"
          :value="s.id"
          :label="`${shortSha(s.commit_sha)} · ${s.ref || ''} · ${STATUS_LABEL[s.status] || s.status}`"
        />
      </el-select>
      <el-button @click="refresh">检查新提交</el-button>
      <el-button type="primary" :disabled="snap?.status !== 'ready'" @click="startExam()">开始考核</el-button>
    </div>

    <el-card v-if="snap && snap.status !== 'ready'" style="margin-bottom: 16px">
      <div class="flex">
        <el-tag :type="statusType(snap.status)">{{ STATUS_LABEL[snap.status] || snap.status }}</el-tag>
        <el-progress class="grow" :percentage="snap.progress || 0" :status="snap.status === 'failed' ? 'exception' : undefined" />
      </div>
      <p class="muted">{{ snap.message }}</p>
      <el-alert v-if="snap.error" type="error" :title="snap.error" :closable="false" />
    </el-card>

    <el-alert
      v-if="profile?.analysis_mode === 'generic'"
      type="warning"
      title="通用模式"
      :description="GENERIC_MODE_NOTE"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
    />

    <el-tabs v-if="profile" v-model="tab">
      <el-tab-pane label="概览" name="overview">
        <el-descriptions :column="4" border style="margin-bottom: 16px">
          <el-descriptions-item label="提交"><span class="mono">{{ shortSha(snap?.commit_sha) }}</span></el-descriptions-item>
          <el-descriptions-item label="文件数">{{ snap?.file_count }}</el-descriptions-item>
          <el-descriptions-item label="大小">{{ fmtBytes(snap?.size_bytes) }}</el-descriptions-item>
          <el-descriptions-item label="分析时间">{{ fmtTime(snap?.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="类型数">{{ profile.stats?.types }}</el-descriptions-item>
          <el-descriptions-item label="函数和方法">{{ profile.stats?.methods }}</el-descriptions-item>
          <el-descriptions-item label="调用数">{{ profile.stats?.calls }}</el-descriptions-item>
          <el-descriptions-item label="已解析调用">{{ profile.stats?.resolved_calls }}</el-descriptions-item>
        </el-descriptions>
        <el-card header="技术栈" style="margin-bottom: 16px">
          <el-tag v-for="t in profile.stack.tech" :key="t" style="margin: 0 6px 6px 0">{{ t }}</el-tag>
          <div class="muted">构建工具：{{ profile.stack.build || '未知' }}，依赖 {{ profile.stack.dependencies.length }} 个</div>
          <div v-if="profile.modules?.length" class="muted" style="margin-top: 6px">
            模块：<span v-for="m in profile.modules" :key="m.name" style="margin-right: 10px">{{ m.name }}（{{ m.files }}）</span>
          </div>
        </el-card>
        <el-card header="画像摘要"><MarkdownView :source="summary" /></el-card>
      </el-tab-pane>

      <el-tab-pane :label="`接口（${profile.endpoints.length}）`" name="endpoints">
        <el-table :data="profile.endpoints" max-height="70vh">
          <el-table-column label="方法" width="90"><template #default="{ row }"><el-tag size="small">{{ row.method }}</el-tag></template></el-table-column>
          <el-table-column label="路径" prop="path" class-name="mono" />
          <el-table-column label="处理方法">
            <template #default="{ row }">
              <el-link class="mono" @click="openFile(row.file, row.start_line || row.line, row.end_line)">{{ row.handler }}</el-link>
            </template>
          </el-table-column>
          <el-table-column label="" width="120">
            <template #default="{ row }">
              <el-button link type="primary" @click="startExam({ mode: 'chain', endpoint: `${row.method} ${row.path}` })">学这条链路</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="aiCount" :label="`AI 应用（${aiCount}）`" name="ai">
        <el-collapse :model-value="['risks']">
          <el-collapse-item v-for="u in aiRows" :key="u.key" :name="u.key" :title="`${u.label}（${u.items.length}）`">
            <el-empty v-if="!u.items.length" description="没有发现" :image-size="40" />
            <div v-for="(it, i) in u.items" :key="i" class="usage">
              <el-link class="mono" :type="u.key === 'risks' ? 'danger' : 'default'" @click="openFile(it.file, it.start_line || it.line, it.end_line || it.line)">
                {{ it.title || it.symbol }}
              </el-link>
              <span class="muted">
                {{ it.file }}:{{ it.line }}
                <template v-if="it.kind"> · {{ it.kind }}</template>
                <template v-if="it.detail"> · {{ it.detail }}</template>
                <template v-if="it.nodes?.length"> · 节点：{{ it.nodes.join('、') }}</template>
              </span>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>
      <el-tab-pane label="关键用法" name="usages">
        <el-collapse>
          <el-collapse-item v-for="u in usageRows" :key="u.key" :name="u.key" :title="`${u.label}（${u.items.length}）`">
            <el-empty v-if="!u.items.length" description="没有发现" :image-size="40" />
            <div v-for="(it, i) in u.items" :key="i" class="usage">
              <el-link class="mono" @click="openFile(it.file, it.line, it.end_line && it.line === it.start_line ? it.end_line : it.line)">
                {{ it.symbol }}
              </el-link>
              <span class="muted">
                {{ it.file }}:{{ it.line }}
                <template v-if="it.annotation"> · @{{ it.annotation }}</template>
                <template v-if="it.propagation"> · {{ it.propagation }}</template>
                <template v-if="it.kind"> · {{ it.kind }}</template>
                <template v-if="it.op"> · {{ it.op }}</template>
              </span>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>

      <el-tab-pane :label="`调用链（${profile.chains.length}）`" name="chains">
        <el-collapse>
          <el-collapse-item v-for="c in profile.chains" :key="c.endpoint" :name="c.endpoint">
            <template #title><span class="mono">{{ c.endpoint }}</span></template>
            <el-timeline>
              <el-timeline-item v-for="(st, i) in c.steps" :key="i" :timestamp="LAYER_LABEL[st.layer] || st.layer" placement="top">
                <span :style="{ marginLeft: st.depth * 16 + 'px' }">
                  <el-link class="mono" @click="openFile(st.file, st.start_line, st.end_line)">{{ st.symbol }}</el-link>
                  <span class="muted"> {{ st.file }}:{{ st.start_line }}</span>
                </span>
              </el-timeline-item>
            </el-timeline>
            <el-button size="small" @click="startExam({ mode: 'chain', endpoint: c.endpoint })">按这条链路逐站学习</el-button>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>

      <el-tab-pane :label="`考点（${topics.length}）`" name="topics">
        <el-card v-for="t in topics" :key="t.id" style="margin-bottom: 12px">
          <div class="flex">
            <el-tag size="small">{{ KIND_LABEL[t.kind] || t.kind }}</el-tag>
            <b class="grow">{{ t.title }}</b>
            <el-rate :model-value="t.importance" disabled :max="5" size="small" />
          </div>
          <p>{{ t.description }}</p>
          <div class="flex" style="flex-wrap: wrap">
            <span class="muted">证据：</span>
            <EvidenceLink v-for="(e, i) in t.evidence" :key="i" :snapshot-id="snapId" :ev="e" />
          </div>
        </el-card>
      </el-tab-pane>

      <el-tab-pane label="代码" name="code">
        <div class="code-layout">
          <el-card class="tree" body-style="padding: 6px">
            <el-tree :key="snapId" lazy :load="loadTree" :props="{ label: 'name', isLeaf: 'leaf' }" @node-click="(d: any) => d.type === 'file' && openFile(d.path)" />
          </el-card>
          <el-card class="grow" v-loading="fileLoading" body-style="padding: 0">
            <template #header><span class="mono">{{ file?.path || '选择一个文件' }}</span></template>
            <CodeViewer v-if="file" :content="file.content" :language="file.language" :start="hl.start" :end="hl.end" />
          </el-card>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.usage { padding: 4px 0; display: flex; gap: 10px; align-items: baseline; }
.code-layout { display: flex; gap: 12px; align-items: flex-start; }
.tree { width: 300px; max-height: 75vh; overflow: auto; flex-shrink: 0; }
</style>
