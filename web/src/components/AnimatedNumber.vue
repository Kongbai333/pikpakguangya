<script setup>
/**
 * 动态数字：数字增加时在右上角短暂显示 +N。
 * 首次加载和数字减少时不提示，避免页面初始化时满屏增量。
 */
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({
  value: { type: [Number, String], default: 0 },
  armed: { type: Boolean, default: true },
})

const delta = ref(0)
const pulseKey = ref(0)
let clearTimer = null
let previousValue = props.value

watch(
  () => props.armed,
  (armed) => {
    if (armed) previousValue = props.value
  }
)

watch(
  () => props.value,
  (next) => {
    const previous = previousValue
    previousValue = next
    if (!props.armed) return

    const nextNumber = Number(next)
    const previousNumber = Number(previous)
    if (
      !Number.isFinite(nextNumber) ||
      !Number.isFinite(previousNumber) ||
      nextNumber <= previousNumber
    ) {
      return
    }

    delta.value = nextNumber - previousNumber
    pulseKey.value += 1
    clearTimeout(clearTimer)
    clearTimer = setTimeout(() => {
      delta.value = 0
    }, 1000)
  }
)

onBeforeUnmount(() => clearTimeout(clearTimer))
</script>

<template>
  <span class="animated-number">
    {{ value ?? '—' }}
    <span v-if="delta" :key="pulseKey" class="number-delta" aria-hidden="true">
      +{{ delta }}
    </span>
  </span>
</template>

<style scoped>
.animated-number {
  position: relative;
  display: inline-block;
  font: inherit;
  line-height: inherit;
  font-variant-numeric: tabular-nums;
}

.number-delta {
  position: absolute;
  top: -0.32em;
  right: -0.6em;
  z-index: 1;
  color: var(--app-ok);
  font-size: 0.48em;
  font-weight: 700;
  line-height: 1;
  white-space: nowrap;
  pointer-events: none;
  animation: number-rise 0.95s cubic-bezier(0.16, 1, 0.3, 1) both;
}

@keyframes number-rise {
  0% {
    opacity: 0;
    transform: translateY(5px) scale(0.9);
  }
  18% {
    opacity: 1;
  }
  72% {
    opacity: 1;
  }
  100% {
    opacity: 0;
    transform: translateY(-14px) scale(1);
  }
}

@media (prefers-reduced-motion: reduce) {
  .number-delta {
    display: none;
  }
}
</style>
