<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { http } from '../api/http'
import { MODE_LABEL, STATUS_LABEL, type Repo, type SessionInfo } from '../api/types'
import { fmtTime, fromNow, shortSha } from '../utils/format'

const repos = ref<Repo[]>([])
const sessions = ref<SessionInfo[]>([])
const mastery = ref<any[]>([])
const loading = ref(true)

onMounted(async () => {
  try {
    const [r, s, m] = await Promise.all([http.get('/repos'), http.get('/sessions'), http.get('/mastery')])
    repos.value = r.data
    sessions.value = s.data
    mastery.value = m.data
  } finally {
    loading.value = false
  }
})

const due = computed(() => mastery.value.filter((m) => m.due))
const statusType = (s?: string) => (s === 'ready' ? 'success' : s === 'failed' ? 'danger' : 'warning')
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="flex" style="margin-bottom: 16px">
      <h1 class="page-title grow" style="margin: 0">我的仓库</h1>
      <el-button type="primary" @click="$router.push('/repos/new')">提交仓库</el-button>
    </div>

    <el-alert
      v-if="due.length"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 16px"
      :title="`有 ${due.length} 个考点到了复习时间`"
    >
      <el-button size="small" @click="$router.push('/mastery')">去复习</el-button>
    </el-alert>

    <el-empty v-if="!loading && !repos.length" description="还没有仓库。提交一个公开的 Spring Boot 仓库开始吧。" />
    <div class="card-grid">
      <el-card v-for="r in repos" :key="r.id" shadow="hover" class="repo" @click="$router.push(`/repos/${r.id}`)">
        <div class="flex">
          <b class="grow">{{ r.owner }}/{{ r.name }}</b>
          <el-tag v-if="r.latest_snapshot" :type="statusType(r.latest_snapshot.status)" size="small">
            {{ STATUS_LABEL[r.latest_snapshot.status] || r.latest_snapshot.status }}
          </el-tag>
        </div>
        <div class="muted" style="margin-top: 8px">
          {{ r.ref || r.default_branch || '默认分支' }} · <span class="mono">{{ shortSha(r.latest_snapshot?.commit_sha) }}</span>
          · {{ fromNow(r.latest_snapshot?.created_at) }}
        </div>
      </el-card>
    </div>

    <h2 class="page-title" style="margin-top: 28px">最近的考核</h2>
    <el-table :data="sessions.slice(0, 20)" empty-text="还没有考核记录">
      <el-table-column label="仓库" prop="repo_name" />
      <el-table-column label="模式"><template #default="{ row }">{{ MODE_LABEL[row.mode] || row.mode }}</template></el-table-column>
      <el-table-column label="状态">
        <template #default="{ row }">{{ STATUS_LABEL[row.status] || row.status }}</template>
      </el-table-column>
      <el-table-column label="开始时间"><template #default="{ row }">{{ fmtTime(row.started_at) }}</template></el-table-column>
      <el-table-column label="" width="180">
        <template #default="{ row }">
          <el-button v-if="row.status === 'active'" link type="primary" @click="$router.push(`/sessions/${row.id}`)">继续</el-button>
          <el-button v-else link type="primary" @click="$router.push(`/sessions/${row.id}/report`)">报告</el-button>
          <el-button link @click="$router.push(`/sessions/${row.id}`)">记录</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.repo { cursor: pointer; }
</style>
