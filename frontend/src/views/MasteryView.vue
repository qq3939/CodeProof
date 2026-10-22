<script setup lang="ts">
import type { EChartsOption } from 'echarts'
import { computed, onMounted, ref } from 'vue'
import { http } from '../api/http'
import { fmtTime, scoreType } from '../utils/format'
import EChart from '../components/EChart.vue'

const rows = ref<any[]>([])
const filter = ref<'all' | 'due' | 'weak'>('all')
const hist = ref<any>(null)

onMounted(async () => {
  rows.value = (await http.get('/mastery')).data
})

const shown = computed(() =>
  rows.value.filter((r) => filter.value === 'all' || (filter.value === 'due' ? r.due : r.weak)),
)

async function showHistory(row: any) {
  const { data } = await http.get(`/mastery/${encodeURIComponent(row.topic_key)}/history`, {
    params: { repository_id: row.repository_id },
  })
  hist.value = data
}

const option = computed<EChartsOption>(() => {
  const h = hist.value?.history || []
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { data: ['本次得分', '掌握度（EMA）'] },
    xAxis: { type: 'category', data: h.map((x: any) => fmtTime(x.at)) },
    yAxis: { type: 'value', min: 0, max: 100 },
    series: [
      { name: '本次得分', type: 'scatter', data: h.map((x: any) => x.score) },
      { name: '掌握度（EMA）', type: 'line', smooth: true, data: h.map((x: any) => x.ema) },
    ],
  }
})
</script>

<template>
  <div class="page">
    <div class="flex" style="margin-bottom: 12px">
      <h1 class="page-title grow" style="margin: 0">掌握度</h1>
      <el-radio-group v-model="filter">
        <el-radio-button value="all">全部</el-radio-button>
        <el-radio-button value="due">到期复习</el-radio-button>
        <el-radio-button value="weak">薄弱</el-radio-button>
      </el-radio-group>
    </div>
    <p class="muted">每次答题后用指数移动平均更新掌握度（新分数占一半权重），分数越低复习间隔越短。下次「项目深挖」会优先出到期和薄弱的考点。</p>
    <el-table :data="shown" empty-text="完成一次考核后这里会出现记录" @row-click="showHistory">
      <el-table-column label="仓库" prop="repo_name" width="160" />
      <el-table-column label="考点" prop="title" />
      <el-table-column label="掌握度" width="100">
        <template #default="{ row }"><el-tag :type="scoreType(row.score)">{{ row.score }}</el-tag></template>
      </el-table-column>
      <el-table-column label="作答次数" prop="attempts" width="90" />
      <el-table-column label="上次" width="150"><template #default="{ row }">{{ fmtTime(row.last_seen_at) }}</template></el-table-column>
      <el-table-column label="下次复习" width="170">
        <template #default="{ row }">
          {{ fmtTime(row.next_review_at) }} <el-tag v-if="row.due" type="warning" size="small">到期</el-tag>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog :model-value="!!hist" :title="hist?.title" width="720px" @close="hist = null">
      <EChart v-if="hist" :option="option" />
    </el-dialog>
  </div>
</template>
