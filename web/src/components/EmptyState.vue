<script setup>
/**
 * 空状态：图标 + 一句话 + 一句「接下来该干什么」。
 * 各页面原来都是光秃秃一行灰字，用户看不出是「没数据」还是「坏了」。
 */
defineProps({
  icon: { type: [Object, Function], default: null },
  title: { type: String, default: '还没有数据' },
  hint: { type: String, default: '' },
})
</script>

<template>
  <div class="empty">
    <div v-if="icon" class="empty__icon">
      <component :is="icon" :size="22" :stroke-width="1.6" />
    </div>
    <div class="empty__title">{{ title }}</div>
    <div v-if="hint" class="empty__hint">{{ hint }}</div>
    <slot />
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 44px 20px;
  text-align: center;
}

.empty__icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  margin-bottom: 4px;
  border-radius: 14px;
  background: var(--app-panel);
  color: var(--app-faint);
}

.empty__title {
  color: var(--app-text);
  font-size: 14px;
  font-weight: 600;
}

.empty__hint {
  max-width: 420px;
  color: var(--app-muted);
  font-size: 12.5px;
  line-height: 1.6;
}
</style>
