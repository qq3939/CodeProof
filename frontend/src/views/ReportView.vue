<script setup lang="ts">
import type { EChartsOption } from 'echarts'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { http } from '../api/http'
import { MODE_LABEL, VERDICT_LABEL } from '../api/types'
import { scoreType } from '../utils/format'
import EChart from '../components/EChart.vue'
import EvidenceLink from '../components/EvidenceLink.vue'
import MarkdownView from '../components/MarkdownView.vue'

const props = defineProps<{ id: string }>()
const rep = ref<any>(null)
let timer: number | undefined

async function load() {
  const { data } = await http.get(`/sessions/${props.id}/report`)
  rep.value = data
  // 阅卷和生成报告是后台任务，还没好就过几秒再问
  if (!data.ready && data.status !== 'active') timer = window.setTimeout(load, 2500)
}
onMounted(load)
onBeforeUnmount(() => clearTimeout(timer))

const radarOption = computed<EChartsOption>(() => {
  const r: Record<string, number> = rep.value?.scores?.radar || {}
  const keys = Object.keys(r)
  if (keys.length < 3) {
    // 雷达图至少要 3 个维度才有意义，不够就画柱状图
    return {
      xAxis: { type: 'category', data: keys },
      yAxis: { type: 'value', max: 100 },
      series: [{ type: 'bar', data: keys.map((k) => r[k]), barWidth: 40 }],
      backgroundColor: 'transparent',
    }
  }
  return {
    backgroundColor: 'transparent',
    radar: { indicator: keys.map((k) => ({ name: k, max: 100 })) },
    series: [{ type: 'radar', data: [{ value: keys.map((k) => r[k]), name: '得分', areaStyle: { opacity: 0.25 } }] }],
  }
})

function exportMd() {
  location.href = `/api/sessions/${props.id}/report.md`
}
</script>

<template>
  <div class="page" style="max-width: 1100px">
    <template v-if="!rep" />
    <el-result v-else-if="rep.status === 'active'" icon="info" title="考核还在进行中">
      <template #extra><el-button type="primary" @click="$router.push(`/sessions/${id}`)">回到考核</el-button></template>
    </el-result>
    <el-result v-else-if="!rep.ready" icon="info" title="正在阅卷……" sub-title="每道题会单独评分，通常需要几十秒" />

    <template v-else>
      <div class="flex" style="margin-bottom: 16px">
        <h1 class="page-title grow" style="margin: 0">考核报告 · {{ MODE_LABEL[rep.mode] || rep.mode }}</h1>
        <el-button @click="$router.push(`/sessions/${id}`)">查看对话</el-button>
        <el-button type="primary" @click="exportMd">导出 Markdown</el-button>
      </div>

      <div class="top">
        <el-card class="total">
          <div class="muted">总分</div>
          <div class="score" :class="scoreType(rep.scores.total)">{{ rep.scores.total }}</div>
          <div class="muted">共 {{ rep.scores.count }} 题</div>
          <el-tag v-if="rep.scores.verdict" :type="rep.scores.total >= 60 ? 'success' : 'danger'" style="margin-top: 8px">
            {{ rep.scores.verdict }}
          </el-tag>
        </el-card>
        <el-card class="grow" header="各类题目得分"><EChart :option="radarOption" height="240px" /></el-card>
      </div>

      <el-card v-if="rep.weak_topics?.length" header="需要加强" style="margin-bottom: 16px">
        <div v-for="w in rep.weak_topics" :key="w.topic_key" style="margin-bottom: 6px">
          <el-tag type="danger" size="small">{{ w.score }}</el-tag>
          <b style="margin: 0 8px">{{ w.title }}</b>
          <span class="muted">建议复习：{{ (w.review || []).join('、') || '-' }}</span>
        </div>
      </el-card>

      <el-card v-for="it in rep.items" :key="it.question_id" style="margin-bottom: 16px">
        <template #header>
          <div class="flex">
            <b class="grow">第 {{ it.index }} 题</b>
            <el-tag v-if="it.skipped" type="info">已跳过</el-tag>
            <el-tag v-if="it.hint_level" type="warning">用了 {{ it.hint_level }} 级提示（原始 {{ it.raw_score }}）</el-tag>
            <el-tag :type="scoreType(it.score)" size="large">{{ it.score }} 分</el-tag>
          </div>
        </template>
        <MarkdownView :source="it.text" />

        <h4>要点</h4>
        <div v-for="(kp, i) in it.key_points" :key="i" class="point">
          <el-tag :type="VERDICT_LABEL[it.point_results[i]?.verdict]?.type || 'info'" size="small">
            {{ VERDICT_LABEL[it.point_results[i]?.verdict]?.text || '-' }}
          </el-tag>
          <div class="grow">
            <div>{{ kp.point }}</div>
            <div class="muted">{{ it.point_results[i]?.reason }}</div>
            <div class="flex" style="flex-wrap: wrap; gap: 8px">
              <EvidenceLink v-for="(e, j) in kp.evidence" :key="j" :snapshot-id="rep.snapshot_id" :ev="e" />
            </div>
          </div>
        </div>

        <template v-if="it.contradictions?.length">
          <h4>与代码矛盾的说法</h4>
          <el-alert v-for="(c, i) in it.contradictions" :key="i" type="error" :closable="false" style="margin-bottom: 8px">
            <template #title>“{{ c.claim }}”</template>
            <div>{{ c.explanation }}</div>
            <div class="flex" style="flex-wrap: wrap; gap: 8px">
              <EvidenceLink v-for="(e, j) in c.evidence" :key="j" :snapshot-id="rep.snapshot_id" :ev="e" />
            </div>
          </el-alert>
        </template>

        <h4>反馈</h4>
        <MarkdownView :source="it.feedback_md" />
        <el-collapse style="margin-top: 10px">
          <el-collapse-item title="参考答案（先自己想清楚再看）"><MarkdownView :source="it.reference_md" /></el-collapse-item>
        </el-collapse>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.top { display: flex; gap: 16px; margin-bottom: 16px; align-items: stretch; }
.total { width: 220px; text-align: center; }
.score { font-size: 56px; font-weight: 700; line-height: 1.2; }
.score.success { color: var(--el-color-success); }
.score.warning { color: var(--el-color-warning); }
.score.danger { color: var(--el-color-danger); }
.point { display: flex; gap: 10px; align-items: flex-start; margin-bottom: 10px; }
h4 { margin: 16px 0 8px; }
</style>
