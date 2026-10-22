<script setup lang="ts">
import { computed, ref, watchEffect } from 'vue'
import { useRoute } from 'vue-router'
import { Moon, Sunny } from '@element-plus/icons-vue'
import { useUserStore } from './stores/user'

const store = useUserStore()
const route = useRoute()
const bare = computed(() => route.meta.public === true)

// 深色模式：Element Plus 的暗色变量挂在 html.dark 上；记在 localStorage，刷新后保持
const dark = ref(localStorage.getItem('pb-dark') === '1')
watchEffect(() => {
  document.documentElement.classList.toggle('dark', dark.value)
  localStorage.setItem('pb-dark', dark.value ? '1' : '0')
})

const active = computed(() => {
  const p = route.path
  if (p.startsWith('/repos') || p === '/') return '/'
  if (p.startsWith('/sessions')) return '/sessions/new'
  return '/' + p.split('/')[1]
})
</script>

<template>
  <router-view v-if="bare" />
  <el-container v-else class="layout">
    <el-header class="header">
      <div class="brand" @click="$router.push('/')">project-b</div>
      <el-menu mode="horizontal" :default-active="active" router :ellipsis="false" class="nav">
        <el-menu-item index="/">仓库</el-menu-item>
        <el-menu-item index="/sessions/new">开始考核</el-menu-item>
        <el-menu-item index="/mastery">掌握度</el-menu-item>
        <el-menu-item index="/resume">简历</el-menu-item>
        <el-menu-item index="/kb">知识库</el-menu-item>
        <el-menu-item index="/settings">设置</el-menu-item>
        <el-menu-item v-if="store.me?.role === 'admin'" index="/ops">运维</el-menu-item>
      </el-menu>
      <div class="flex">
        <el-switch v-model="dark" :active-action-icon="Moon" :inactive-action-icon="Sunny" />
        <el-dropdown v-if="store.me">
          <span class="flex user">
            <el-avatar :size="28" :src="store.me.avatar_url || undefined">{{ store.me.login[0] }}</el-avatar>
            {{ store.me.login }}
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="$router.push('/settings')">设置</el-dropdown-item>
              <el-dropdown-item divided @click="store.logout()">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </el-header>
    <el-main class="main"><router-view /></el-main>
  </el-container>
</template>

<style scoped>
.layout { min-height: 100%; }
.header {
  display: flex; align-items: center; gap: 16px; background: var(--pb-card);
  border-bottom: 1px solid var(--pb-border);
}
.brand { font-weight: 700; font-size: 18px; cursor: pointer; white-space: nowrap; }
.nav { flex: 1; border-bottom: none; background: transparent; }
.user { cursor: pointer; gap: 8px; color: var(--el-text-color-regular); }
.main { padding: 0; }
</style>
