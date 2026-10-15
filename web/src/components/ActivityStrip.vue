<script setup>
/**
 * 实时活动条：最近 N 分钟，每分钟采到多少条消息。
 *
 * 用纯 CSS 画而不是 ECharts —— 60 根小柱子开一个图表实例太重，
 * 而且这里的重点是「有没有在动」，不需要坐标轴和交互。
 * 鼠标悬停给一条 tooltip，够用了。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  series: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  height: { type: Number, default: 64 },
})

const hover = ref(-1)

const peak = computed(() =>
  Math.max(1, ...props.series.map((b) => Number(b.messages) || 0))
)

const total = computed(() =>
  props.series.reduce((sum, b) => sum + (Number(b.messages) || 0), 0)
)

/** 本地时间：后端给的是 UTC 时间戳，显示要按浏览器时区换算 */
function localLabel(item) {
  const d = new Date(item.at)
  if (Number.isNaN(d.getTime())) return item.label || ''
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}`
}

function barStyle(item, index) {
  const value = Number(item.messages) || 0
  // 有数据时至少给 8% 高度，否则「1 条」和「0 条」看起来一样
  const ratio = value ? Math.max(0.08, value / peak.value) : 0
  return {
    height: `${Math.max(ratio * 100, value ? 8 : 2)}%`,
    animationDelay: `${(index % 12) * 0.05}s`,
  }
}

const active = computed(() =>
  hover.value >= 0 ? props.series[hover.value] : null
)
</script>

<template>
  <div class="strip">
    <div class="strip__head">
      <span class="strip__title">实时活动</span>
      <span class="strip__meta">
        最近 {{ series.length }} 分钟 · <b>{{ total }}</b> 条消息
      </span>
      <span class="strip__hint">{{ active ? localLabel(active) : '每分钟一格' }}</span>
    </div>

    <div class="strip__plot" :style="{ height: `${height}px` }">
      <div class="strip__grid" aria-hidden="true">
        <span /><span /><span />
      </div>

      <div
        v-for="(item, index) in series"
        :key="item.at || index"
        class="strip__slot"
        @mouseenter="hover = index"
        @mouseleave="hover = -1"
      >
        <div
          class="strip__bar"
          :class="{
            on: Number(item.messages) > 0,
            live: index === series.length - 1,
            dim: hover >= 0 && hover !== index,
          }"
          :style="barStyle(item, index)"
        />
      </div>

      <div v-if="loading && !series.length" class="strip__empty">读取中…</div>
      <div v-else-if="!series.length" class="strip__empty">暂无数据</div>
    </div>

    <div class="strip__foot">
      <span>{{ series.length ? localLabel(series[0]) : '—' }}</span>
      <span v-if="active" class="strip__tip">
        {{ localLabel(active) }} · 消息 {{ active.messages }} · 新链接 {{ active.links }}
      </span>
      <span v-else class="strip__tip faint">悬停查看每分钟明细</span>
      <span>现在</span>
    </div>
  </div>
</template>

<style scoped>
.strip {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.strip__head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 12.5px;
}

.strip__title {
  font-weight: 600;
  color: var(--app-text);
}

.strip__meta {
  color: var(--app-muted);
}

.strip__meta b {
  color: var(--app-text);
}

.strip__hint {
  margin-left: auto;
  color: var(--app-faint);
  font-variant-numeric: tabular-nums;
}

.strip__plot {
  position: relative;
  display: flex;
  align-items: flex-end;
  gap: 2px;
  padding: 0 2px;
  border-bottom: 1px solid var(--app-line);
}

.strip__grid {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  pointer-events: none;
}

.strip__grid span {
  height: 1px;
  background: var(--app-line-soft);
  opacity: 0.7;
}

.strip__slot {
  position: relative;
  flex: 1;
  min-width: 0;
  height: 100%;
  display: flex;
  align-items: flex-end;
}

.strip__bar {
  width: 100%;
  min-height: 2px;
  border-radius: 3px 3px 1px 1px;
  background: var(--app-line);
  transform-origin: bottom;
  transition: opacity var(--dur-fast) ease, background var(--dur-fast) ease;
}

.strip__bar.on {
  background: color-mix(in srgb, var(--app-brand) 62%, var(--app-line));
  animation: strip-grow 0.5s var(--ease-out) backwards;
}

.strip__bar.dim {
  opacity: 0.45;
}

/* 最后一格是「这一分钟」，加个呼吸感表示还在采 */
.strip__bar.live {
  background: var(--app-brand);
  animation: strip-pulse 2.4s ease-in-out infinite;
}

.strip__empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: var(--app-faint);
  font-size: 12.5px;
}

.strip__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11.5px;
  color: var(--app-faint);
  font-variant-numeric: tabular-nums;
}

.strip__tip {
  color: var(--app-muted);
}

@keyframes strip-grow {
  from {
    transform: scaleY(0.2);
    opacity: 0;
  }
}

@keyframes strip-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.55;
  }
}

@media (prefers-reduced-motion: reduce) {
  .strip__bar.on,
  .strip__bar.live {
    animation: none;
  }
}
</style>
