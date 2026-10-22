<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { http } from '../api/http'

const router = useRouter()
const url = ref('')
const ref_ = ref('')
const loading = ref(false)

async function submit() {
  if (!url.value.trim()) return
  loading.value = true
  try {
    const { data } = await http.post('/repos', { url: url.value.trim(), ref: ref_.value.trim() || null })
    if (data.reused) ElMessage.success('这个提交已经分析过，直接复用结果')
    router.push(`/repos/${data.repository_id}`)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page" style="max-width: 720px">
    <h1 class="page-title">提交仓库</h1>
    <el-card>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="GitHub 仓库地址">
          <el-input v-model="url" placeholder="https://github.com/spring-projects/spring-petclinic" />
        </el-form-item>
        <el-form-item label="分支 / 标签 / 提交 SHA（可选，默认用默认分支）">
          <el-input v-model="ref_" placeholder="main" />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading">开始分析</el-button>
      </el-form>
      <el-divider />
      <ul class="muted">
        <li>目前只支持公开仓库，主要面向 Java / Spring Boot 项目。</li>
        <li>系统只读取代码，不会运行仓库里的任何脚本或构建。</li>
        <li>同一个提交只分析一次，再次提交会直接复用。</li>
      </ul>
    </el-card>
  </div>
</template>
