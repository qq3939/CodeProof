<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { http } from '../api/http'
import { useUserStore } from '../stores/user'
import { fmtTime } from '../utils/format'

const store = useUserStore()
const tokens = ref<any[]>([])
const name = ref('')
const created = ref<string | null>(null)

async function load() {
  tokens.value = (await http.get('/tokens')).data
}
onMounted(load)

async function create() {
  if (!name.value.trim()) return
  const { data } = await http.post('/tokens', { name: name.value.trim() })
  created.value = data.token // 明文只出现这一次，数据库里只存哈希
  name.value = ''
  await load()
}

async function revoke(row: any) {
  await ElMessageBox.confirm(`吊销令牌「${row.name}」？用它的 MCP 客户端会立刻失效。`, '吊销', { type: 'warning' })
  await http.delete(`/tokens/${row.id}`)
  await load()
}

async function copy(s: string) {
  await navigator.clipboard.writeText(s)
  ElMessage.success('已复制')
}

const apiUrl = computed(() => `${location.origin}/api`)
const mcpCmd = computed(
  () =>
    `claude mcp add project-b \\\n  -e PB_API_URL=${apiUrl.value} \\\n  -e PB_API_TOKEN=${created.value || 'pbt_你的令牌'} \\\n  -- uv run --directory /path/to/project-b/backend python -m app.mcp_server`,
)
</script>

<template>
  <div class="page" style="max-width: 900px">
    <h1 class="page-title">设置</h1>
    <el-card header="账号" style="margin-bottom: 16px">
      <el-descriptions :column="2">
        <el-descriptions-item label="登录名">{{ store.me?.login }}</el-descriptions-item>
        <el-descriptions-item label="角色">{{ store.me?.role === 'admin' ? '管理员' : '普通用户' }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card header="个人 API 令牌" style="margin-bottom: 16px">
      <p class="muted">给 MCP 客户端（Claude Code、Cursor 等）用。令牌和你的账号权限相同，请不要分享。</p>
      <div class="flex">
        <el-input v-model="name" placeholder="令牌名称，例如 my-laptop" style="width: 300px" @keyup.enter="create" />
        <el-button type="primary" @click="create">生成</el-button>
      </div>
      <el-alert v-if="created" type="success" :closable="false" style="margin-top: 12px" title="令牌已生成，只会显示这一次，请立即复制保存">
        <div class="flex"><code class="mono">{{ created }}</code><el-button size="small" @click="copy(created!)">复制</el-button></div>
      </el-alert>
      <el-table :data="tokens" style="margin-top: 12px" empty-text="还没有令牌">
        <el-table-column label="名称" prop="name" />
        <el-table-column label="创建时间"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
        <el-table-column label="最近使用"><template #default="{ row }">{{ fmtTime(row.last_used_at) }}</template></el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }"><el-tag :type="row.revoked ? 'info' : 'success'" size="small">{{ row.revoked ? '已吊销' : '有效' }}</el-tag></template>
        </el-table-column>
        <el-table-column width="80">
          <template #default="{ row }"><el-button v-if="!row.revoked" link type="danger" @click="revoke(row)">吊销</el-button></template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card header="在 Claude Code 里使用（MCP）">
      <p>在本机克隆项目后，执行下面的命令把 project-b 注册为 MCP Server：</p>
      <pre class="mono cmd">{{ mcpCmd }}</pre>
      <el-button size="small" @click="copy(mcpCmd)">复制命令</el-button>
      <p class="muted">
        之后在 Claude Code 里可以直接说"用 project-b 分析 https://github.com/xxx/yyy 然后考我 3 道题"。
        可用工具：list_repos、analyze_repo、start_session、answer、hint、get_report。
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.cmd { background: var(--el-fill-color-light); padding: 12px; border-radius: 6px; white-space: pre-wrap; }
</style>
