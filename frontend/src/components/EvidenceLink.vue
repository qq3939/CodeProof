<script setup lang="ts">
import { ref } from 'vue'
import { http } from '../api/http'
import type { Evidence } from '../api/types'
import CodeViewer from './CodeViewer.vue'

const props = defineProps<{ snapshotId: string; ev: Evidence }>()

const open = ref(false)
const loading = ref(false)
const file = ref<{ content: string; language: string } | null>(null)

async function show() {
  open.value = true
  if (file.value) return
  loading.value = true
  try {
    const { data } = await http.get(`/snapshots/${props.snapshotId}/file`, { params: { path: props.ev.path } })
    file.value = data
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-link type="primary" class="mono" @click="show">
    {{ ev.path.split('/').pop() }}:{{ ev.start_line }}<template v-if="ev.end_line !== ev.start_line">-{{ ev.end_line }}</template>
  </el-link>
  <el-dialog v-model="open" :title="`${ev.path}  第 ${ev.start_line}-${ev.end_line} 行`" width="80%" append-to-body>
    <div v-loading="loading" style="min-height: 120px">
      <CodeViewer
        v-if="file"
        :content="file.content"
        :language="file.language"
        :start="ev.start_line"
        :end="ev.end_line"
        max-height="65vh"
      />
    </div>
  </el-dialog>
</template>
