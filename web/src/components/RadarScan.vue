<script setup>
/**
 * 采集雷达 —— 仪表盘的实时监控核心。
 *
 * 光点是**随机位置生成、自己淡出**的，而不是绑在账号上固定不动：
 * 固定的点看起来像一张静态图，扫描线转过去也没反应。现在的做法更像真雷达 ——
 * 扫描到哪儿就在哪儿冒一个回波，亮一下再消失。在采账号越多，冒得越密。
 *
 * 只做状态可视化，不承载交互。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AnimatedNumber from './AnimatedNumber.vue'

const props = defineProps({
  running: { type: Boolean, default: false },
  count: { type: Number, default: 0 },
  accounts: { type: Array, default: () => [] },
  armed: { type: Boolean, default: true },
})

const R = 88
const CX = 100
const CY = 100
// 光点只落在这个半径带里：太靠中心会盖住读数，太靠边会压到刻度环
const MIN_R = 26
const MAX_R = R - 12
const TICK_MS = 320

/** 外圈刻度：60 根，每 5 根加长一档 */
const ticks = computed(() =>
  Array.from({ length: 60 }, (_, i) => {
    const angle = (i * 6 * Math.PI) / 180
    const major = i % 5 === 0
    const inner = major ? R - 9 : R - 5
    return {
      key: i,
      x1: CX + Math.sin(angle) * inner,
      y1: CY - Math.cos(angle) * inner,
      x2: CX + Math.sin(angle) * R,
      y2: CY - Math.cos(angle) * R,
      major,
    }
  })
)

/* ---------- 光点：随机出现、慢慢消失 ---------- */
const blips = ref([])
let seq = 0
let timer = null

/** 在采账号越多，同时存在的回波越多、冒得越勤 */
const maxBlips = computed(() => (props.running ? Math.min(7, 2 + props.count) : 0))
const spawnChance = computed(() => (props.running ? Math.min(0.75, 0.35 + props.count * 0.12) : 0))

function spawn() {
  // sqrt 让点在圆面上均匀分布，否则会明显往圆心堆
  const angle = Math.random() * Math.PI * 2
  const radius = Math.sqrt(
    Math.random() * (MAX_R * MAX_R - MIN_R * MIN_R) + MIN_R * MIN_R
  )
  const life = 2200 + Math.random() * 1600
  seq += 1
  blips.value.push({
    id: seq,
    x: +(CX + Math.cos(angle) * radius).toFixed(1),
    y: +(CY + Math.sin(angle) * radius).toFixed(1),
    life,
    expiresAt: Date.now() + life,
  })
}

function tick() {
  if (!props.running) {
    if (blips.value.length) blips.value = []
    return
  }
  const now = Date.now()
  const alive = blips.value.filter((b) => b.expiresAt > now)
  if (alive.length !== blips.value.length) blips.value = alive
  if (blips.value.length < maxBlips.value && Math.random() < spawnChance.value) {
    spawn()
  }
}

onMounted(() => {
  timer = setInterval(tick, TICK_MS)
  if (props.running) spawn()
})

onBeforeUnmount(() => {
  clearInterval(timer)
  timer = null
})

// 从「没在跑」切到「在跑」时立刻给一个回波，不用等下一个 tick
watch(
  () => props.running,
  (running) => {
    if (running) spawn()
    else blips.value = []
  }
)

const legend = computed(() =>
  props.accounts
    .filter((a) => a.running || a.collecting)
    .slice(0, 3)
    .map((a, i) => ({
      id: a.account_id ?? a.id ?? i,
      name: String(a.account || a.name || a.phone || `#${a.account_id ?? a.id ?? i}`),
    }))
)
const legendMore = computed(
  () => props.accounts.filter((a) => a.running || a.collecting).length - legend.value.length
)
</script>

<template>
  <div class="radar" :class="{ on: running }">
    <svg viewBox="0 0 200 200" role="img" :aria-label="`采集雷达：${count} 个账号在采`">
      <defs>
        <radialGradient id="radar-disc" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="var(--app-brand)" stop-opacity="0.11" />
          <stop offset="70%" stop-color="var(--app-brand)" stop-opacity="0.03" />
          <stop offset="100%" stop-color="var(--app-brand)" stop-opacity="0" />
        </radialGradient>
        <linearGradient id="radar-trail" x1="100" y1="12" x2="185" y2="77" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="var(--app-brand)" stop-opacity="0.36" />
          <stop offset="100%" stop-color="var(--app-brand)" stop-opacity="0" />
        </linearGradient>
        <linearGradient id="radar-beam" x1="100" y1="12" x2="126" y2="36" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="var(--app-brand)" stop-opacity="0.6" />
          <stop offset="100%" stop-color="var(--app-brand)" stop-opacity="0" />
        </linearGradient>
        <filter id="radar-glow" x="-70%" y="-70%" width="240%" height="240%">
          <feGaussianBlur stdDeviation="2.6" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      <!-- 底盘 + 距离环 -->
      <circle class="disc" :cx="CX" :cy="CY" :r="R" />
      <circle class="ring" :cx="CX" :cy="CY" :r="R" />
      <circle class="ring" :cx="CX" :cy="CY" :r="66" />
      <circle class="ring" :cx="CX" :cy="CY" :r="44" />
      <circle class="ring ring--soft" :cx="CX" :cy="CY" :r="22" />

      <!-- 十字准线 -->
      <line class="axis" :x1="CX - R" :y1="CY" :x2="CX + R" :y2="CY" />
      <line class="axis" :x1="CX" :y1="CY - R" :x2="CX" :y2="CY + R" />

      <!-- 刻度环：反向慢转 -->
      <g class="ticks">
        <line
          v-for="t in ticks"
          :key="t.key"
          :class="{ major: t.major }"
          :x1="t.x1"
          :y1="t.y1"
          :x2="t.x2"
          :y2="t.y2"
        />
      </g>

      <!-- 扫描：宽拖尾 + 窄亮边 -->
      <g class="sweep">
        <path d="M100 100 L100 12 A88 88 0 0 1 185.0 77.2 Z" fill="url(#radar-trail)" />
        <path d="M100 100 L100 12 A88 88 0 0 1 122.8 24.4 Z" fill="url(#radar-beam)" />
        <line class="sweep-edge" :x1="CX" :y1="CY" :x2="CX" :y2="CY - R" />
      </g>

      <!-- 回波：随机位置冒出来，自己淡掉 -->
      <g v-for="b in blips" :key="b.id">
        <circle
          class="echo-ring"
          :cx="b.x"
          :cy="b.y"
          r="3"
          :style="{ animationDuration: `${b.life}ms` }"
        />
        <circle
          class="echo"
          :cx="b.x"
          :cy="b.y"
          r="3.2"
          :style="{ animationDuration: `${b.life}ms` }"
        />
      </g>
    </svg>

    <div class="readout">
      <div class="readout__n">
        <AnimatedNumber :value="count" :armed="armed" />
      </div>
      <div class="readout__k">{{ running ? '在采账号' : '未运行' }}</div>
    </div>

    <div v-if="legend.length" class="legend">
      <span v-for="b in legend" :key="b.id" class="legend__item">{{ b.name }}</span>
      <span v-if="legendMore > 0" class="legend__more">+{{ legendMore }}</span>
    </div>
  </div>
</template>

<style scoped>
.radar {
  position: relative;
  width: 208px;
  height: 208px;
  flex: 0 0 auto;
}

.radar svg {
  display: block;
  width: 100%;
  height: 100%;
}

.disc {
  fill: url(#radar-disc);
}

.ring {
  fill: none;
  stroke: var(--app-line);
  stroke-width: 1;
}

.ring--soft {
  stroke-dasharray: 3 5;
}

.axis {
  stroke: var(--app-line-soft);
  stroke-width: 1;
}

.ticks {
  transform-origin: 100px 100px;
  animation: radar-drift 90s linear infinite;
}

.ticks line {
  stroke: var(--app-line);
  stroke-width: 1;
}

.ticks line.major {
  stroke: var(--app-faint);
}

.sweep {
  transform-origin: 100px 100px;
  animation: radar-spin 3.6s linear infinite;
}

.sweep-edge {
  stroke: var(--app-brand);
  stroke-width: 1.6;
  filter: url(#radar-glow);
}

/* ---------- 回波 ---------- */
.echo {
  fill: var(--app-ok);
  filter: url(#radar-glow);
  animation-name: echo-life;
  animation-timing-function: ease-out;
  animation-fill-mode: both;
}

.echo-ring {
  fill: none;
  stroke: var(--app-ok);
  stroke-width: 1.2;
  transform-box: fill-box;
  transform-origin: center;
  animation-name: echo-spread;
  animation-timing-function: ease-out;
  animation-fill-mode: both;
}

@keyframes echo-life {
  0% {
    opacity: 0;
    transform: scale(0.3);
  }
  10% {
    opacity: 1;
    transform: scale(1.15);
  }
  22% {
    transform: scale(1);
  }
  60% {
    opacity: 0.85;
  }
  100% {
    opacity: 0;
    transform: scale(1.5);
  }
}

@keyframes echo-spread {
  0% {
    opacity: 0.85;
    transform: scale(1);
  }
  70% {
    opacity: 0;
    transform: scale(5.5);
  }
  100% {
    opacity: 0;
    transform: scale(5.5);
  }
}

/* ---------- 中心读数 ---------- */
.readout {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  text-align: center;
  pointer-events: none;
}

.readout__n {
  font-size: 40px;
  font-weight: 700;
  line-height: 1;
  letter-spacing: -1px;
  font-variant-numeric: tabular-nums;
  color: var(--app-text);
  text-shadow: 0 0 22px color-mix(in srgb, var(--app-brand) 34%, transparent);
}

.readout__k {
  margin-top: 5px;
  font-size: 11.5px;
  letter-spacing: 0.4px;
  color: var(--app-muted);
}

/* ---------- 底部账号名 ---------- */
.legend {
  position: absolute;
  left: 50%;
  bottom: -6px;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  font-size: 11.5px;
  color: var(--app-faint);
  white-space: nowrap;
}

.legend__item {
  max-width: 74px;
  overflow: hidden;
  text-overflow: ellipsis;
}

.legend__more {
  color: var(--app-muted);
}

/* ---------- 未运行：整体压暗，扫描停住 ---------- */
.radar:not(.on) {
  filter: saturate(0.25);
  opacity: 0.62;
}

.radar:not(.on) .sweep {
  animation: none;
  opacity: 0.2;
}

.radar:not(.on) .ticks {
  animation: none;
}

@keyframes radar-spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes radar-drift {
  to {
    transform: rotate(-360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .sweep,
  .ticks {
    animation: none;
  }
  /* 回波改成静态亮点：不闪，但「有东西被扫到」还是看得出来 */
  .echo {
    animation: none;
    opacity: 0.9;
  }
  .echo-ring {
    animation: none;
    opacity: 0.35;
  }
}
</style>
