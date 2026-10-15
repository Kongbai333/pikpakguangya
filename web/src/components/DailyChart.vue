<script setup>
/**
 * 每日走势图。ECharts 按需引入，别把整个 echarts 打进来。
 *
 * 柱状 = 当前选中的指标（新增链接 / 消息量 / 校验数 / 判失效），
 * 折线 = 累计链接（右轴，看总量趋势）。柱子换成别的指标时累计线不动，
 * 这样「今天采得多不多」和「库里攒了多少」能同时看到。
 *
 * ⚠️ 图表容器**不能**加 v-show / v-if：组件挂载时数据还没回来，容器一旦是
 * display:none，echarts.init() 量到的是 0×0，之后数据到了也不会自己变回来
 * —— 表现就是「图表一片空白」。所以容器常驻，空数据时用一层浮层盖住。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { DAILY_METRICS } from '../format'
import { dark } from '../theme'

echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps({
  series: { type: Array, default: () => [] },
  metric: { type: String, default: 'new_links' },
  loading: { type: Boolean, default: false },
})

const el = ref(null)
let chart = null
let ro = null

const dates = computed(() => props.series.map((r) => r.date.slice(5))) // MM-DD
const bars = computed(() => props.series.map((r) => r[props.metric] ?? 0))
const cumulative = computed(() => props.series.map((r) => r.cumulative))

const meta = computed(
  () => DAILY_METRICS.find((m) => m.key === props.metric) || DAILY_METRICS[0]
)

function build() {
  const d = dark.value
  const axis = { color: d ? '#7d8899' : '#98a1b3', fontSize: 11 }
  const line = d ? '#26303e' : '#e6e9ee'
  return {
    animationDuration: 400,
    grid: { left: 4, right: 4, top: 34, bottom: 2, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: {
        type: 'shadow',
        shadowStyle: { color: 'rgba(37,99,235,0.05)' },
      },
      backgroundColor: d ? 'rgba(21,27,37,0.97)' : 'rgba(255,255,255,0.97)',
      borderColor: line,
      textStyle: { color: d ? '#e6ebf3' : '#1f2430', fontSize: 12 },
      extraCssText: 'box-shadow:0 4px 16px rgba(16,24,40,0.12);border-radius:8px;',
    },
    legend: {
      top: 0,
      icon: 'roundRect',
      itemWidth: 9,
      itemHeight: 9,
      itemGap: 16,
      textStyle: { color: d ? '#9aa7b8' : '#6b7285', fontSize: 12 },
    },
    xAxis: {
      type: 'category',
      data: dates.value,
      boundaryGap: true,
      axisLine: { lineStyle: { color: line } },
      axisTick: { show: false },
      axisLabel: axis,
    },
    yAxis: [
      {
        type: 'value',
        name: '当天',
        nameTextStyle: { color: d ? '#6d7a8c' : '#b3b9c6', fontSize: 11, padding: [0, 0, 0, -26] },
        splitLine: { lineStyle: { color: d ? '#202936' : '#f0f2f6', type: 'dashed' } },
        axisLabel: axis,
      },
      {
        type: 'value',
        name: '累计',
        nameTextStyle: { color: d ? '#6d7a8c' : '#b3b9c6', fontSize: 11 },
        splitLine: { show: false },
        axisLabel: axis,
      },
    ],
    series: [
      {
        name: meta.value.label,
        type: 'bar',
        barMaxWidth: 16,
        itemStyle: { color: meta.value.color, borderRadius: [3, 3, 0, 0] },
        data: bars.value,
      },
      {
        name: '累计链接',
        type: 'line',
        yAxisIndex: 1,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1.6, color: '#2563eb' },
        itemStyle: { color: '#2563eb' },
        data: cumulative.value,
      },
    ],
  }
}

function resize() {
  // 容器还藏着（display:none / 宽度为 0）时 resize 会把画布算成 0×0，跳过
  if (!chart || !el.value) return
  if (!el.value.clientWidth || !el.value.clientHeight) return
  chart.resize()
}

onMounted(() => {
  chart = echarts.init(el.value)
  chart.setOption(build())
  window.addEventListener('resize', resize)
  // 窗口拉伸、卡片宽度变化（比如侧边栏折叠）都要跟着重画
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(resize)
    ro.observe(el.value)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  ro?.disconnect()
  ro = null
  chart?.dispose()
  chart = null
})

watch(
  () => [props.series, props.metric, dark.value],
  async () => {
    if (!chart) return
    chart.setOption(build(), { notMerge: true })
    // 从「没数据」切到「有数据」时容器尺寸可能刚变，下一帧补一次
    await nextTick()
    resize()
  },
  { deep: true }
)
</script>

<template>
  <div class="relative w-full" style="height: 288px">
    <div ref="el" class="w-full h-full" />
    <div
      v-if="loading"
      class="absolute inset-0 grid place-items-center bg-[var(--app-panel)]/70"
    >
      <n-spin size="small" />
    </div>
    <div
      v-if="!series.length && !loading"
      class="absolute inset-0 grid place-items-center muted text-[14px] pointer-events-none"
    >
      还没有数据
    </div>
  </div>
</template>
