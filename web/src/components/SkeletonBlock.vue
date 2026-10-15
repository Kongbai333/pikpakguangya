<script setup>
/**
 * 骨架块。首屏加载时先用它占位，比整页转圈稳 —— 布局不会在数据到达时跳一下。
 */
defineProps({
  width: { type: String, default: '100%' },
  height: { type: String, default: '14px' },
  radius: { type: String, default: '' },
})
</script>

<template>
  <div
    class="skeleton"
    :style="{ width, height, borderRadius: radius || undefined }"
    aria-hidden="true"
  />
</template>
