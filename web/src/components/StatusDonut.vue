<script setup>
/**
 * 状态构成：环形图 + 图例。
 *
 * 和 DailyChart 一样，容器**不能**加 v-show / v-if —— 挂载时若容器是
 * display:none，echarts.init() 量到 0×0，之后数据到了也回不来（画布一片空白）。
 * 所以图表容器常驻，空数据时用一层浮层盖住。
 *
 * 图例用 HTML 画而不是 ECharts 自带 legend：状态最多 9 种，自带 legend 会挤成一团，
 * 而且没法做「名称在左、数量和占比右对齐」这种排法。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { PieChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { dark } from '../theme'
import AnimatedNumber from './AnimatedNumber.vue'

echarts.use([PieChart, TooltipComponent, CanvasRenderer])

const props = defineProps({
  counts: { type: Object, default: () => ({}) },
  labels: { type: Object, default: () => ({}) },
  loading: { type: Boolean, default: false },
  centerLabel: { type: String, default: '总链接' },
  palette: { type: Array, default: () => [] },
  armed: { type: Boolean, default: true },
})

/* 语义化配色：绿=还能用，蓝=要提取码，橙=码不对，红=打不开，灰=没检测过 */
const COLOR = {
  OK: '#12a594',
  PASS_CODE_EMPTY: '#3b82f6',
  PASS_CODE_ERROR: '#e08700',
  NOT_FOUND: '#e5484d',
  DELETED: '#e07b39',
  EXPIRED: '#b7791f',
  SENSITIVE_RESOURCE: '#a14f70',
  ERROR: '#8b8f9a',
  unchecked: '#cbd2dc',
}

const items = computed(() => {
  const entries = Object.entries(props.counts || {}).map(([k, v], i) => ({
      key: k,
      name: props.labels?.[k] || (k === 'unchecked' ? '未检测' : k),
      value: Number(v) || 0,
      itemStyle: {
        color: COLOR[k] || props.palette[i % Math.max(1, props.palette.length)] || '#98a1b3',
      },
    }))
  return entries.filter((x) => x.value > 0).sort((a, b) => b.value - a.value)
})

const total = computed(() => items.value.reduce((s, x) => s + x.value, 0))
const pct = (v) => (total.value ? ((v / total.value) * 100).toFixed(1) + '%' : '0%')

const el = ref(null)
let chart = null
let ro = null

function build() {
  const d = dark.value
  return {
    animationDuration: 400,
    tooltip: {
      trigger: 'item',
      formatter: '{b}：{c}（{d}%）',
      backgroundColor: d ? 'rgba(21,27,37,0.97)' : 'rgba(255,255,255,0.97)',
      borderColor: d ? '#26303e' : '#e6e9ee',
      textStyle: { color: d ? '#e6ebf3' : '#1f2430', fontSize: 12 },
      extraCssText: 'box-shadow:0 4px 16px rgba(16,24,40,0.12);border-radius:8px;',
    },
    series: [
      {
        type: 'pie',
        radius: ['64%', '88%'],
        center: ['50%', '50%'],
        avoidLabelOverlap: false,
        label: { show: false },
        labelLine: { show: false },
        itemStyle: { borderColor: d ? '#151b25' : '#fff', borderWidth: 2 },
        data: items.value,
      },
    ],
  }
}

function resize() {
  // 侧边栏折叠会改卡片宽度；容器还是 0 宽时 resize 会把画布算成 0×0，跳过
  if (!chart || !el.value) return
  if (!el.value.clientWidth || !el.value.clientHeight) return
  chart.resize()
}

onMounted(() => {
  chart = echarts.init(el.value)
  chart.setOption(build())
  window.addEventListener('resize', resize)
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
  () => [props.counts, dark.value],
  async () => {
    if (!chart) return
    chart.setOption(build(), { notMerge: true })
    await nextTick()
    resize()
  },
  { deep: true }
)
</script>

<template>
  <div>
    <div class="relative w-full" style="height: 176px">
      <div ref="el" class="w-full h-full" />
      <div class="absolute inset-0 grid place-items-center pointer-events-none">
        <div class="text-center">
          <div class="text-[22px] font-bold leading-none">
            <AnimatedNumber :value="total" :armed="armed" />
          </div>
          <div class="text-[12.5px] muted mt-1">{{ centerLabel }}</div>
        </div>
      </div>
      <div
        v-if="!items.length && !loading"
        class="absolute inset-0 grid place-items-center muted text-[14px] pointer-events-none"
        style="background: var(--app-panel)"
      >
        还没有数据
      </div>
      <div
        v-if="loading"
        class="absolute inset-0 grid place-items-center bg-[var(--app-panel)]/70"
      >
        <n-spin size="small" />
      </div>
    </div>

    <div class="mt-2">
      <div v-for="it in items" :key="it.key" class="legend-row">
        <span class="legend-dot" :style="{ background: it.itemStyle.color }" />
        <span class="legend-name">{{ it.name }}</span>
        <span class="legend-val"><AnimatedNumber :value="it.value" :armed="armed" /></span>
        <span class="legend-pct">{{ pct(it.value) }}</span>
      </div>
    </div>
  </div>
</template>
