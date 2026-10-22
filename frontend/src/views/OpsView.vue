<script setup lang="ts">
import type { EChartsOption } from 'echarts'
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '../api/http'
import { MODE_LABEL, STATUS_LABEL } from '../api/types'
import { fmtTime } from '../utils/format'
import EChart from '../components/EChart.vue'

const tab = ref('jobs')

// ---------------------------------------------------------------- 任务
const jobs = ref<{ counts: Record<string, number>; items: any[] }>({ counts: {}, items: [] })
const jobStatus = ref('')
async function loadJobs() {
  jobs.value = (await http.get('/ops/jobs', { params: { status: jobStatus.value || undefined } })).data
}
async function retry(row: any) {
  await http.post(`/ops/jobs/${row.id}/retry`)
  ElMessage.success('已重新投递')
  await loadJobs()
}
watch(jobStatus, loadJobs)

// ---------------------------------------------------------------- 成本
const groupBy = ref<'day' | 'user' | 'session'>('day')
const days = ref(30)
const costs = ref<any>({ items: [], by_model: [] })
async function loadCosts() {
  costs.value = (await http.get('/ops/costs', { params: { group_by: groupBy.value, days: days.value } })).data
}
watch([groupBy, days], loadCosts)
const costOption = computed<EChartsOption>(() => {
  const items = [...costs.value.items].reverse()
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { data: ['token', '费用'] },
    xAxis: { type: 'category', data: items.map((i: any) => i.key.slice(0, 10)) },
    yAxis: [{ type: 'value', name: 'token' }, { type: 'value', name: '费用' }],
    series: [
      { name: 'token', type: 'bar', data: items.map((i: any) => i.prompt_tokens + i.completion_tokens) },
      { name: '费用', type: 'line', yAxisIndex: 1, data: items.map((i: any) => i.cost) },
    ],
  }
})

// ---------------------------------------------------------------- 会话 trace
const sessions = ref<any[]>([])
const trace = ref<any>(null)
async function loadSessions() {
  sessions.value = (await http.get('/ops/sessions')).data
}
async function openTrace(id: string) {
  trace.value = (await http.get(`/ops/sessions/${id}/trace`)).data
}

// ---------------------------------------------------------------- 评测
const evals = ref<any[]>([])
async function loadEvals() {
  evals.value = (await http.get('/ops/eval-runs')).data
}

onMounted(() => Promise.all([loadJobs(), loadCosts(), loadSessions(), loadEvals()]))
const jobType = (s: string) => (s === 'done' ? 'success' : s === 'failed' ? 'danger' : s === 'running' ? 'warning' : 'info')
</script>

<template>
  <div class="page">
    <h1 class="page-title">运维</h1>
    <el-tabs v-model="tab">
      <el-tab-pane label="任务" name="jobs">
        <div class="flex" style="margin-bottom: 10px">
          <el-tag v-for="(n, k) in jobs.counts" :key="k" :type="jobType(String(k))">{{ STATUS_LABEL[k] || k }}：{{ n }}</el-tag>
          <span class="grow" />
          <el-select v-model="jobStatus" clearable placeholder="按状态筛选" style="width: 160px">
            <el-option v-for="s in ['queued', 'running', 'done', 'failed']" :key="s" :value="s" :label="STATUS_LABEL[s] || s" />
          </el-select>
          <el-button @click="loadJobs">刷新</el-button>
        </div>
        <el-table :data="jobs.items">
          <el-table-column type="expand">
            <template #default="{ row }">
              <pre class="mono err">{{ JSON.stringify(row.args, null, 2) }}</pre>
              <pre v-if="row.error" class="mono err">{{ row.error }}</pre>
            </template>
          </el-table-column>
          <el-table-column label="类型" prop="kind" width="140" />
          <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="jobType(row.status)" size="small">{{ STATUS_LABEL[row.status] || row.status }}</el-tag></template></el-table-column>
          <el-table-column label="用户" prop="user" width="120" />
          <el-table-column label="重试" prop="attempts" width="70" />
          <el-table-column label="创建"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
          <el-table-column label="结束"><template #default="{ row }">{{ fmtTime(row.finished_at) }}</template></el-table-column>
          <el-table-column width="80"><template #default="{ row }"><el-button v-if="row.status === 'failed'" link type="primary" @click="retry(row)">重试</el-button></template></el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="成本" name="costs">
        <div class="flex" style="margin-bottom: 10px">
          <el-radio-group v-model="groupBy">
            <el-radio-button value="day">按天</el-radio-button>
            <el-radio-button value="user">按用户</el-radio-button>
            <el-radio-button value="session">按会话</el-radio-button>
          </el-radio-group>
          <el-select v-model="days" style="width: 120px">
            <el-option :value="7" label="最近 7 天" /><el-option :value="30" label="最近 30 天" /><el-option :value="90" label="最近 90 天" />
          </el-select>
        </div>
        <el-card v-if="groupBy === 'day'" style="margin-bottom: 12px"><EChart :key="tab" :option="costOption" /></el-card>
        <el-table :data="costs.items">
          <el-table-column label="分组" prop="key" />
          <el-table-column label="输入 token" prop="prompt_tokens" />
          <el-table-column label="输出 token" prop="completion_tokens" />
          <el-table-column label="调用数" prop="calls" />
          <el-table-column label="失败" prop="errors" />
          <el-table-column label="费用" prop="cost" />
        </el-table>
        <h3>按模型</h3>
        <el-table :data="costs.by_model">
          <el-table-column label="模型" prop="model" />
          <el-table-column label="token" prop="tokens" />
          <el-table-column label="费用" prop="cost" />
          <el-table-column label="平均延迟（ms）" prop="avg_latency_ms" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="会话 trace" name="trace">
        <div class="trace">
          <el-table :data="sessions" highlight-current-row style="width: 520px" @row-click="(r: any) => openTrace(r.id)">
            <el-table-column label="用户" prop="user" width="100" />
            <el-table-column label="模式" width="110"><template #default="{ row }">{{ MODE_LABEL[row.mode] || row.mode }}</template></el-table-column>
            <el-table-column label="token" prop="total_tokens" width="80" />
            <el-table-column label="开始"><template #default="{ row }">{{ fmtTime(row.started_at) }}</template></el-table-column>
          </el-table>
          <el-card class="grow" v-if="trace">
            <template #header>
              token {{ trace.totals.tokens }} · 费用 {{ trace.totals.cost }} · 模型调用 {{ trace.totals.llm_calls }} · 工具调用 {{ trace.totals.tool_calls }}
            </template>
            <el-timeline style="max-height: 70vh; overflow: auto">
              <el-timeline-item
                v-for="(t, i) in trace.timeline"
                :key="i"
                :timestamp="`${fmtTime(t.at)} · ${t.node || ''} · ${t.latency_ms ?? '-'}ms`"
                :type="t.type === 'llm' ? (t.status === 'error' ? 'danger' : 'primary') : 'success'"
              >
                <template v-if="t.type === 'llm'">
                  <b>模型</b> {{ t.model }} · {{ t.prompt_tokens }}+{{ t.completion_tokens }} token
                  <span v-if="t.error" class="muted"> · {{ t.error }}</span>
                </template>
                <template v-else>
                  <b>工具</b> <span class="mono">{{ t.tool }}({{ JSON.stringify(t.args) }})</span>
                  <pre class="mono err">{{ t.result }}</pre>
                </template>
              </el-timeline-item>
            </el-timeline>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="评测" name="eval">
        <p class="muted">评测用命令行运行：<code>uv run python -m app.eval run --suite small</code>，结果会记录在这里。</p>
        <el-table :data="evals">
          <el-table-column type="expand">
            <template #default="{ row }"><pre class="mono err">{{ JSON.stringify(row.metrics, null, 2) }}</pre></template>
          </el-table-column>
          <el-table-column label="数据集" prop="suite" />
          <el-table-column label="状态" prop="status" />
          <el-table-column label="模型"><template #default="{ row }">{{ row.config?.llm_provider }} {{ row.config?.model || '' }}</template></el-table-column>
          <el-table-column label="报告" prop="report_path" />
          <el-table-column label="时间"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.err { background: var(--el-fill-color-light); padding: 8px; white-space: pre-wrap; max-height: 300px; overflow: auto; font-size: 12px; }
.trace { display: flex; gap: 12px; align-items: flex-start; }
</style>
