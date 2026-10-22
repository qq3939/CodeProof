<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { http } from '../api/http'
import { MODE_LABEL, type Repo } from '../api/types'
import { shortSha } from '../utils/format'

const route = useRoute()
const router = useRouter()
const repos = ref<Repo[]>([])
const resumes = ref<any[]>([])
const endpoints = ref<string[]>([])
const loading = ref(false)

const form = reactive({
  snapshot_id: (route.query.snapshot as string) || '',
  mode: (route.query.mode as string) || 'deep_dive',
  difficulty: 'medium',
  question_count: 5,
  max_depth: 2,
  allow_hints: true,
  style: 'plain',
  endpoint: (route.query.endpoint as string) || '',
  resume_id: (route.query.resume_id as string) || '',
  change_by: 'pr',
  pr_url: '',
  base: '',
  head: '',
})

const MODE_DESC: Record<string, string> = {
  deep_dive: '围绕项目里的设计决策、风险点、关键机制提问，每个回答都要能在代码里找到依据。',
  kb_link: '先问八股知识点，再追问"你的项目里哪里用到了、和标准做法有什么不同"。可以结合知识库里的笔记。',
  chain: '选一个接口，从 Controller 到数据库逐站讲解，每一站都问你"这里做了什么、为什么"。',
  resume: '把简历里的项目经历和代码对照，追问有证据的说法，指出代码里找不到的说法。',
  change: '给一段提交区间或公开 PR，考你对这次改动的理解：改了什么、影响哪些调用方、风险在哪。',
}

onMounted(async () => {
  const [r, rs] = await Promise.all([http.get('/repos'), http.get('/resumes')])
  repos.value = r.data
  resumes.value = rs.data
  if (!form.snapshot_id) form.snapshot_id = readyRepos.value[0]?.latest_snapshot?.id || ''
})

const readyRepos = computed(() => repos.value.filter((r) => r.latest_snapshot?.status === 'ready'))

watch(
  () => [form.snapshot_id, form.mode],
  async () => {
    if (form.mode !== 'chain' || !form.snapshot_id) return
    const { data } = await http.get(`/snapshots/${form.snapshot_id}/profile`)
    endpoints.value = (data.profile.chains || []).map((c: any) => c.endpoint)
    if (!form.endpoint) form.endpoint = endpoints.value[0] || ''
  },
  { immediate: true },
)

async function submit() {
  loading.value = true
  const config: Record<string, any> = {}
  if (form.mode === 'chain') config.endpoint = form.endpoint
  if (form.mode === 'resume') config.resume_id = form.resume_id
  if (form.mode === 'change') {
    if (form.change_by === 'pr') config.pr_url = form.pr_url
    else Object.assign(config, { base: form.base, head: form.head || null })
  }
  try {
    const { data } = await http.post('/sessions', {
      snapshot_id: form.snapshot_id,
      mode: form.mode,
      difficulty: form.difficulty,
      question_count: form.question_count,
      max_depth: form.max_depth,
      allow_hints: form.allow_hints,
      style: form.style,
      config,
    })
    router.push(`/sessions/${data.session.id}`)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page" style="max-width: 820px">
    <h1 class="page-title">开始考核</h1>
    <el-card>
      <el-form label-width="110px" @submit.prevent="submit">
        <el-form-item label="仓库">
          <el-select v-model="form.snapshot_id" placeholder="选择已分析完成的仓库" style="width: 100%">
            <el-option
              v-for="r in readyRepos"
              :key="r.id"
              :value="r.latest_snapshot!.id"
              :label="`${r.owner}/${r.name} @ ${shortSha(r.latest_snapshot!.commit_sha)}`"
            />
            <el-option
              v-if="form.snapshot_id && !readyRepos.some((r) => r.latest_snapshot?.id === form.snapshot_id)"
              :value="form.snapshot_id"
              :label="`快照 ${shortSha(form.snapshot_id)}`"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="模式">
          <el-radio-group v-model="form.mode">
            <el-radio-button v-for="(v, k) in MODE_LABEL" :key="k" :value="k">{{ v }}</el-radio-button>
          </el-radio-group>
          <div class="muted" style="width: 100%; margin-top: 6px">{{ MODE_DESC[form.mode] }}</div>
        </el-form-item>

        <el-form-item v-if="form.mode === 'chain'" label="接口">
          <el-select v-model="form.endpoint" filterable allow-create style="width: 100%">
            <el-option v-for="e in endpoints" :key="e" :value="e" :label="e" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.mode === 'resume'" label="简历">
          <el-select v-model="form.resume_id" style="width: 100%" placeholder="先在「简历」页上传并核验">
            <el-option v-for="r in resumes" :key="r.id" :value="r.id" :label="r.filename || r.id" />
          </el-select>
        </el-form-item>
        <template v-if="form.mode === 'change'">
          <el-form-item label="改动来源">
            <el-radio-group v-model="form.change_by">
              <el-radio value="pr">公开 PR</el-radio>
              <el-radio value="range">提交区间</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item v-if="form.change_by === 'pr'" label="PR 地址">
            <el-input v-model="form.pr_url" placeholder="https://github.com/owner/repo/pull/123" />
          </el-form-item>
          <template v-else>
            <el-form-item label="base 提交"><el-input v-model="form.base" placeholder="SHA 或分支" /></el-form-item>
            <el-form-item label="head 提交"><el-input v-model="form.head" placeholder="SHA 或分支" /></el-form-item>
          </template>
        </template>

        <el-form-item label="难度">
          <el-radio-group v-model="form.difficulty">
            <el-radio-button value="easy">简单</el-radio-button>
            <el-radio-button value="medium">中等</el-radio-button>
            <el-radio-button value="hard">困难</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="题目数"><el-input-number v-model="form.question_count" :min="1" :max="20" /></el-form-item>
        <el-form-item label="最多追问"><el-input-number v-model="form.max_depth" :min="0" :max="5" /> <span class="muted" style="margin-left: 8px">层</span></el-form-item>
        <el-form-item label="允许提示"><el-switch v-model="form.allow_hints" /><span class="muted" style="margin-left: 8px">用提示会按级别扣分</span></el-form-item>
        <el-form-item label="语气">
          <el-radio-group v-model="form.style">
            <el-radio value="plain">直接提问</el-radio>
            <el-radio value="interviewer">模拟面试官</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="loading" :disabled="!form.snapshot_id">开始</el-button>
          <span v-if="loading" class="muted" style="margin-left: 10px">面试官正在准备第一题……</span>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>
