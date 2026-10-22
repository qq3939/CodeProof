<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { http } from '../api/http'
import { useUserStore } from '../stores/user'

const route = useRoute()
const router = useRouter()
const store = useUserStore()
const cfg = ref<{ github: boolean; dev_login: boolean }>({ github: false, dev_login: false })
const login = ref('')
const loading = ref(false)
const next = (route.query.next as string) || '/'
const error = route.query.error as string | undefined

onMounted(async () => {
  const { data } = await http.get('/auth/config')
  cfg.value = data
})

function github() {
  // 整页跳转：OAuth 需要浏览器去 GitHub 授权页，再被重定向回来
  location.href = '/api/auth/github/login'
}

async function devLogin() {
  if (!login.value.trim()) return
  loading.value = true
  try {
    await http.post('/auth/dev-login', { login: login.value.trim() })
    await store.fetchMe()
    router.replace(next)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login">
    <el-card class="box">
      <h2>project-b</h2>
      <p class="muted">AI 当考官，以代码为证据，考你对一个 Java / Spring Boot 仓库的理解。</p>
      <el-alert v-if="error" :title="`登录失败：${error}`" type="error" show-icon :closable="false" />
      <el-button v-if="cfg.github" type="primary" size="large" class="full" @click="github">使用 GitHub 登录</el-button>
      <el-alert
        v-if="!cfg.github"
        type="info"
        :closable="false"
        title="还没有配置 GitHub OAuth App"
        description="在 .env 里填写 GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET 后即可用 GitHub 登录。"
      />
      <template v-if="cfg.dev_login">
        <el-divider>开发模式登录</el-divider>
        <el-form @submit.prevent="devLogin">
          <el-input v-model="login" placeholder="随便起个用户名（仅本地开发可用）" />
          <el-button class="full" :loading="loading" native-type="submit" style="margin-top: 10px">登录</el-button>
        </el-form>
      </template>
    </el-card>
  </div>
</template>

<style scoped>
.login { min-height: 100%; display: flex; align-items: center; justify-content: center; }
.box { width: 400px; }
.box :deep(.el-alert) { margin: 12px 0; }
.full { width: 100%; }
</style>
