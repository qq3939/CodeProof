<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ECharts, EChartsOption } from 'echarts'

const props = defineProps<{ option: EChartsOption; height?: string }>()
const el = ref<HTMLElement>()
let chart: ECharts | null = null
const ro = new ResizeObserver(() => chart?.resize())

onMounted(async () => {
  const echarts = await import('echarts') // 按需加载，只有带图表的页面才下载 ECharts
  chart = echarts.init(el.value!, document.documentElement.classList.contains('dark') ? 'dark' : undefined)
  chart.setOption(props.option)
  ro.observe(el.value!)
})
watch(() => props.option, (o) => chart?.setOption(o, true), { deep: true })
onBeforeUnmount(() => {
  ro.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div ref="el" :style="{ height: height || '300px', width: '100%' }" />
</template>
