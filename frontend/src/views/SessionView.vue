<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { http } from '../api/http'
import { postStream, type StreamEvent } from '../api/sse'
import { KIND_LABEL, MODE_LABEL, STATUS_LABEL, type SessionDetail, type Turn } from '../api/types'
import MarkdownView from '../components/MarkdownView.vue'

const props = defineProps<{ id: string }>()

const detail = ref<SessionDetail | null>(null)
const turns = ref<Turn[]>([])
const answer = ref('')
const busy = ref(false)
const toolMsg = ref('')
const live = ref<{ role: Turn['role']; content: string; tag?: string } | null>(null)
const finished = ref<string | null>(null)
const chatBox = ref<HTMLElement>()
let timer: number | undefined

const state = computed(() => detail.value?.state)
const sess = computed(() => detail.value?.session)
const active = computed(() => sess.value?.status === 'active')

async function load() {
  const { data } = await http.get<SessionDetail>(`/sessions/${props.id}`)
  apply(data)
  // 改动理解模式要先在后台拉取 diff、生成题目，状态是 preparing，这里轮询到它就绪
  if (data.session.status === 'preparing') timer = window.setTimeout(load, 2000)
}

function apply(data: SessionDetail) {
  detail.value = data
  turns.value = data.turns
  scrollDown()
}

onMounted(load)
onBeforeUnmount(() => clearTimeout(timer))

function scrollDown() {
  nextTick(() => chatBox.value?.scrollTo({ top: chatBox.value.scrollHeight, behavior: 'smooth' }))
}

function onEvent(e: StreamEvent) {
  const d = e.data
  switch (e.event) {
    case 'tool':
      // 面试官核对代码时告诉用户它在看哪里，而不是干等
      toolMsg.value = d.message
      break
    case 'next_question':
      flush()
      turns.value.push(sysTurn(`第 ${d.index} / ${d.total} 题 · ${KIND_LABEL[d.kind] || d.kind || ''} · ${d.topic || ''}`))
      live.value = { role: 'examiner', content: '' }
      toolMsg.value = ''
      break
    case 'followup':
      flush()
      live.value = { role: 'examiner', content: '', tag: `追问 ${d.depth}` }
      toolMsg.value = ''
      break
    case 'token':
      if (!live.value) live.value = { role: 'examiner', content: '' }
      live.value.content += d.text
      scrollDown()
      break
    case 'hint':
      flush()
      turns.value.push({ ...sysTurn(d.text), role: 'hint', meta: { level: d.level, penalty: d.penalty } })
      break
    case 'session_finished':
      flush()
      finished.value = d.reason
      break
    case 'error':
      ElMessage.error(d.message)
      break
    case 'done':
      if (detail.value) detail.value.state = d
      break
  }
}

function sysTurn(content: string): Turn {
  return { seq: -1, role: 'system', content, question_id: null, depth: 0, meta: null, created_at: '' }
}

function flush() {
  if (live.value?.content) {
    turns.value.push({ ...sysTurn(live.value.content), role: live.value.role, meta: live.value.tag ? { tag: live.value.tag } : null })
  }
  live.value = null
}

async function stream(url: string, body?: unknown) {
  busy.value = true
  toolMsg.value = ''
  try {
    await postStream(url, body, onEvent)
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    flush()
    busy.value = false
    toolMsg.value = ''
    // 流里的内容只用于实时展示；结束后以服务端记录为准，避免本地和数据库不一致
    const { data } = await http.get<SessionDetail>(`/sessions/${props.id}`)
    apply(data)
  }
}

async function send() {
  const text = answer.value.trim()
  if (!text || busy.value) return
  answer.value = ''
  turns.value.push({ ...sysTurn(text), role: 'user' })
  scrollDown()
  await stream(`/sessions/${props.id}/answer`, { text })
}

async function hint() {
  const next = (state.value?.hint_level || 0) + 1
  const penalty = [5, 10, 20][next - 1]
  await ElMessageBox.confirm(`要第 ${next} 级提示吗？这道题会扣 ${penalty} 分。`, '使用提示', { type: 'warning' })
  await stream(`/sessions/${props.id}/hint`)
}

async function skip() {
  await ElMessageBox.confirm('跳过这道题？跳过的题记 0 分。', '跳过', { type: 'warning' })
  busy.value = true
  try {
    const { data } = await http.post<SessionDetail>(`/sessions/${props.id}/skip`)
    apply(data)
  } finally {
    busy.value = false
  }
}

async function finish() {
  await ElMessageBox.confirm('提前结束考核？已经作答的题会照常阅卷。', '结束考核', { type: 'warning' })
  busy.value = true
  try {
    const { data } = await http.post<SessionDetail>(`/sessions/${props.id}/finish`)
    apply(data)
  } finally {
    busy.value = false
  }
}

function onKey(e: KeyboardEvent) {
  // Ctrl/Cmd + Enter 提交；单独回车换行，方便写长回答
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send()
}
</script>

<template>
  <div class="page session" v-if="detail">
    <div class="chat-col">
      <div class="flex" style="margin-bottom: 10px">
        <h1 class="page-title grow" style="margin: 0">
          {{ MODE_LABEL[sess!.mode] || sess!.mode }}
          <el-tag size="small" style="margin-left: 8px">{{ STATUS_LABEL[sess!.status] || sess!.status }}</el-tag>
        </h1>
        <el-button v-if="!active && sess!.status !== 'preparing'" type="primary" @click="$router.push(`/sessions/${id}/report`)">查看报告</el-button>
      </div>

      <el-alert v-if="sess!.status === 'preparing'" type="info" :closable="false" show-icon title="正在分析这次改动并出题，请稍候……" />
      <el-alert v-if="sess!.status === 'failed'" type="error" :closable="false" show-icon :title="sess!.finish_reason || '准备失败'" />

      <div ref="chatBox" class="chat">
        <div v-for="(t, i) in turns" :key="i" class="chat-row" :class="t.role">
          <div class="chat-bubble">
            <div v-if="t.role === 'hint'" class="muted">💡 {{ t.meta?.level }} 级提示（扣 {{ t.meta?.penalty }} 分）</div>
            <div v-if="t.meta?.tag || (t.role === 'examiner' && t.depth > 0)" class="muted">{{ t.meta?.tag || `追问 ${t.depth}` }}</div>
            <div v-if="t.role === 'system'">{{ t.content }}</div>
            <MarkdownView v-else-if="t.role !== 'user'" :source="t.content" />
            <div v-else style="white-space: pre-wrap">{{ t.content }}</div>
          </div>
        </div>
        <div v-if="live" class="chat-row examiner">
          <div class="chat-bubble">
            <div v-if="live.tag" class="muted">{{ live.tag }}</div>
            <MarkdownView :source="live.content || '……'" />
          </div>
        </div>
        <div v-if="toolMsg" class="chat-row system"><div class="chat-bubble">🔍 {{ toolMsg }}……</div></div>
        <div v-else-if="busy && !live" class="chat-row system"><div class="chat-bubble">面试官正在思考……</div></div>
        <el-result v-if="finished" icon="success" :title="`考核结束：${finished}`" sub-title="正在后台阅卷，稍后可以查看报告">
          <template #extra><el-button type="primary" @click="$router.push(`/sessions/${id}/report`)">查看报告</el-button></template>
        </el-result>
      </div>

      <div v-if="active" class="input">
        <el-input
          v-model="answer"
          type="textarea"
          :rows="4"
          resize="none"
          placeholder="写下你的回答，尽量指出具体的类和方法。Ctrl + Enter 发送"
          :disabled="busy || !state?.awaiting_answer"
          @keydown="onKey"
        />
        <div class="flex" style="margin-top: 8px">
          <el-button type="primary" :loading="busy" :disabled="!answer.trim() || !state?.awaiting_answer" @click="send">发送</el-button>
          <el-button v-if="state?.allow_hints" :disabled="busy || (state?.hint_level || 0) >= 3" @click="hint">
            提示（{{ state?.hint_level || 0 }}/3）
          </el-button>
          <el-button :disabled="busy" @click="skip">跳过这题</el-button>
          <span class="grow" />
          <el-button type="danger" plain :disabled="busy" @click="finish">结束考核</el-button>
        </div>
      </div>
    </div>

    <el-card class="side" header="进度">
      <template v-if="state">
        <el-progress :percentage="state.total ? Math.round((Math.min(state.index, state.total) / state.total) * 100) : 0" :format="() => `${state!.index}/${state!.total}`" />
        <el-descriptions :column="1" size="small" style="margin-top: 12px">
          <el-descriptions-item label="当前考点">{{ state.current_topic || '-' }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ KIND_LABEL[state.current_kind || ''] || state.current_kind || '-' }}</el-descriptions-item>
          <el-descriptions-item label="追问深度">{{ state.depth }} / {{ state.max_depth }}</el-descriptions-item>
          <el-descriptions-item label="本题提示">{{ state.hint_level }} 级</el-descriptions-item>
        </el-descriptions>
      </template>
      <el-divider />
      <div class="muted">已用 token：{{ sess!.total_tokens }}</div>
      <div class="muted">费用：¥{{ sess!.cost.toFixed(4) }}</div>
    </el-card>
  </div>
</template>

<style scoped>
.session { display: flex; gap: 16px; align-items: flex-start; }
.chat-col { flex: 1; min-width: 0; display: flex; flex-direction: column; height: calc(100vh - 100px); }
.chat { flex: 1; overflow-y: auto; padding: 4px; }
.input { border-top: 1px solid var(--pb-border); padding-top: 10px; }
.side { width: 280px; flex-shrink: 0; }
</style>
