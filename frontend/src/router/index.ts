import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '../stores/user'

// 页面按路由懒加载：首屏只下载登录页和布局，ECharts、Shiki 这些大依赖用到时才加载
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('../views/LoginView.vue'), meta: { public: true } },
    { path: '/', component: () => import('../views/DashboardView.vue') },
    { path: '/repos/new', component: () => import('../views/RepoNewView.vue') },
    { path: '/repos/:id', component: () => import('../views/RepoView.vue'), props: true },
    { path: '/sessions/new', component: () => import('../views/SessionNewView.vue') },
    { path: '/sessions/:id', component: () => import('../views/SessionView.vue'), props: true },
    { path: '/sessions/:id/report', component: () => import('../views/ReportView.vue'), props: true },
    { path: '/resume', component: () => import('../views/ResumeView.vue') },
    { path: '/mastery', component: () => import('../views/MasteryView.vue') },
    { path: '/kb', component: () => import('../views/KbView.vue') },
    { path: '/settings', component: () => import('../views/SettingsView.vue') },
    { path: '/ops', component: () => import('../views/OpsView.vue'), meta: { admin: true } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

router.beforeEach(async (to) => {
  const store = useUserStore()
  if (!store.loaded) await store.fetchMe()
  if (to.meta.public) return true
  if (!store.me) return { path: '/login', query: { next: to.fullPath } }
  // 前端隐藏只是体验；真正的权限检查在后端 require_admin
  if (to.meta.admin && store.me.role !== 'admin') return '/'
  return true
})

export default router
