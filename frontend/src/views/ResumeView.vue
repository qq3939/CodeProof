<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type UploadRequestOptions } from 'element-plus'
import { http } from '../api/http'
import type { Repo } from '../api/types'
import { fmtTime, shortSha } from '../utils/format'
import EvidenceLink from '../components/EvidenceLink.vue'

const router = useRouter()
const resumes = ref<any[]>([])
const repos = ref<Repo[]>([])
const current = ref<any>(null)
const mapping = ref<Record<string, string | null>>({})
const verification = ref<any>(null)
const pasted = ref('')
let timer: number | undefined

const VERDICT: Record<string, { text: string; type: 'success' | 'warning' | 'danger' | 'info' }> = {
  supported: { text: '有证据', type: 'success' },
  partial: { text: '部分有证据', type: 'warning' },
  not_found: { text: '代码里找不到', type: 'danger' },
  contradicted: { text: '与代码矛盾', type: 'danger' },
  unverifiable: { text: '代码中无法验证', type: 'info' },
}

async function load() {
  const [r, rp] = await Promise.all([http.get('/resumes'), http.get('/repos')])
  resumes.value = r.data
  repos.value = rp.data.filter((x: Repo) => x.latest_snapshot?.status === 'ready')
}
onMounted(load)
onBeforeUnmount(() => clearTimeout(timer))

async function open(id: string) {
  const { data } = await http.get(`/resumes/${id}`)
  current.value = data
  mapping.value = { ...Object.fromEntries(data.projects.map((p: string) => [p, null])), ...data.resume.mapping }
  await loadVerification()
}

async function loadVerification() {
  clearTimeout(timer)
  const { data } = await http.get(`/resumes/${current.value.resume.id}/verification`)
  verification.value = data
  if (data.status === 'verifying') timer = window.setTimeout(loadVerification, 2500)
}

async function upload(opt: UploadRequestOptions) {
  const fd = new FormData()
  fd.append('file', opt.file)
  const { data } = await http.post('/resumes', fd)
  await load()
  await open(data.id)
}

async function submitText() {
  if (!pasted.value.trim()) return
  const fd = new FormData()
  fd.append('text', pasted.value)
  const { data } = await http.post('/resumes', fd)
  pasted.value = ''
  await load()
  await open(data.id)
}

async function verify() {
  await http.put(`/resumes/${current.value.resume.id}/mapping`, { mapping: mapping.value })
  await http.post(`/resumes/${current.value.resume.id}/verify`)
  ElMessage.success('开始核验：逐条在代码里找证据')
  verification.value = { status: 'verifying', items: [], counts: {} }
  timer = window.setTimeout(loadVerification, 2000)
}

const snapshotOf = computed(() => {
  // 简历追问需要一个快照作为会话的主仓库：取第一个已映射的项目
  const v = Object.values(mapping.value).find(Boolean)
  return v || null
})

function startSession() {
  router.push({ path: '/sessions/new', query: { mode: 'resume', snapshot: snapshotOf.value || undefined, resume_id: current.value.resume.id } })
}
</script>

<template>
  <div class="page">
    <h1 class="page-title">简历核验</h1>
    <p class="muted">
      上传简历后，系统会抽取每个项目里的具体说法（"用 Redis 做了点赞计数"），把项目对应到你提交过的仓库，再逐条在代码里找证据。
      找不到证据的说法，面试时最容易被问倒。
    </p>
    <div class="layout">
      <el-card class="left" header="我的简历">
        <el-upload :http-request="upload" :show-file-list="false" accept=".pdf,.txt,.md">
          <el-button type="primary">上传 PDF</el-button>
        </el-upload>
        <el-input v-model="pasted" type="textarea" :rows="4" placeholder="或者直接粘贴简历文本" style="margin-top: 10px" />
        <el-button size="small" style="margin-top: 6px" @click="submitText">提交文本</el-button>
        <el-divider />
        <div v-for="r in resumes" :key="r.id" class="item" :class="{ on: current?.resume.id === r.id }" @click="open(r.id)">
          <div>{{ r.filename || '粘贴的文本' }}</div>
          <div class="muted">{{ fmtTime(r.created_at) }} · {{ r.status }}</div>
        </div>
      </el-card>

      <div class="grow" v-if="current">
        <el-card header="项目和仓库对应关系" style="margin-bottom: 16px">
          <el-empty v-if="!current.projects.length" description="没有抽取到项目经历" :image-size="50" />
          <div v-for="p in current.projects" :key="p" class="flex" style="margin-bottom: 8px">
            <b style="width: 200px">{{ p }}</b>
            <el-select v-model="mapping[p]" clearable placeholder="选择对应的仓库（可不选）" style="width: 360px">
              <el-option
                v-for="r in repos"
                :key="r.id"
                :value="r.latest_snapshot!.id"
                :label="`${r.owner}/${r.name} @ ${shortSha(r.latest_snapshot!.commit_sha)}`"
              />
            </el-select>
          </div>
          <el-button type="primary" :loading="verification?.status === 'verifying'" @click="verify">保存并核验</el-button>
          <el-button :disabled="!snapshotOf" @click="startSession">用这份简历开始追问</el-button>
        </el-card>

        <el-card v-if="verification?.items?.length" header="核验结果">
          <div class="flex" style="margin-bottom: 12px; flex-wrap: wrap">
            <el-tag v-for="(n, k) in verification.counts" :key="k" :type="VERDICT[k]?.type">{{ VERDICT[k]?.text || k }}：{{ n }}</el-tag>
          </div>
          <div v-for="it in verification.items" :key="it.claim.id" class="claim">
            <div class="flex">
              <el-tag :type="VERDICT[it.verdict]?.type || 'info'" size="small">{{ VERDICT[it.verdict]?.text || '待核验' }}</el-tag>
              <span class="muted">{{ it.claim.project_name }}</span>
            </div>
            <div style="margin: 6px 0"><b>{{ it.claim.claim_text }}</b></div>
            <div v-if="it.explanation">{{ it.explanation }}</div>
            <div v-if="it.suggestion" class="muted">建议：{{ it.suggestion }}</div>
            <div v-if="it.claim.snapshot_id" class="flex" style="flex-wrap: wrap; gap: 8px">
              <EvidenceLink v-for="(e, j) in it.evidence" :key="j" :snapshot-id="it.claim.snapshot_id" :ev="e" />
            </div>
          </div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.layout { display: flex; gap: 16px; align-items: flex-start; }
.left { width: 300px; flex-shrink: 0; }
.item { padding: 8px; border-radius: 6px; cursor: pointer; }
.item.on, .item:hover { background: var(--el-fill-color-light); }
.claim { border-bottom: 1px solid var(--pb-border); padding: 10px 0; }
</style>
