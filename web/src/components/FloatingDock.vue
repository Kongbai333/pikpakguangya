<script setup>
/**
 * 管理端底部浮动导航。
 * 交互保持轻量：悬停放大、组间分隔和图标提示，不依赖额外动画库。
 */
import { RouterLink, useRoute } from 'vue-router'

defineProps({
  items: { type: Array, default: () => [] },
})

const route = useRoute()

function isActive(item) {
  return item.names?.includes(route.name) || route.path === item.to
}
</script>

<template>
  <div class="dock-wrap">
    <nav class="dock" aria-label="后台导航">
      <template v-for="(item, index) in items" :key="item.to">
        <span
          v-if="index && items[index - 1]?.group !== item.group"
          class="dock-divider"
          aria-hidden="true"
        />
        <RouterLink
          :to="item.to"
          class="dock-item"
          :class="{ on: isActive(item) }"
          :aria-label="item.label"
        >
          <span class="dock-tip">{{ item.label }}</span>
          <span class="dock-icon">
            <component :is="item.icon" :size="19" :stroke-width="1.8" />
          </span>
        </RouterLink>
      </template>
    </nav>
  </div>
</template>

<style scoped>
.dock-wrap {
  position: fixed;
  left: 50%;
  bottom: 18px;
  z-index: 50;
  transform: translateX(-50%);
  pointer-events: none;
}

.dock {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 64px;
  padding: 8px 12px;
  border: 1px solid color-mix(in srgb, var(--app-line) 74%, transparent);
  border-radius: 22px;
  background: color-mix(in srgb, var(--app-bg) 82%, transparent);
  box-shadow:
    0 18px 44px rgba(24, 24, 27, 0.12),
    inset 0 1px 0 rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(18px) saturate(150%);
  pointer-events: auto;
}

html[data-theme='dark'] .dock {
  box-shadow:
    0 20px 48px rgba(0, 0, 0, 0.42),
    inset 0 1px 0 rgba(255, 255, 255, 0.06);
}

.dock-item {
  position: relative;
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 999px;
  color: var(--app-muted);
  text-decoration: none;
  isolation: isolate;
  transition: color 0.18s ease;
}

.dock-item::before {
  content: '';
  position: absolute;
  inset: 2px;
  z-index: -1;
  border-radius: 999px;
  background: transparent;
  transform: scale(0.82);
  transition:
    transform 0.22s cubic-bezier(0.2, 0.8, 0.2, 1),
    background 0.18s ease;
}

.dock-item:hover,
.dock-item:focus-visible {
  color: var(--app-text);
}

.dock-item:hover::before,
.dock-item:focus-visible::before,
.dock-item.on::before {
  background: var(--app-panel-2);
  transform: scale(1);
}

.dock-icon {
  position: relative;
  z-index: 1;
  display: grid;
  place-items: center;
  transition: transform 0.22s cubic-bezier(0.2, 0.8, 0.2, 1);
  will-change: transform;
}

.dock-item:hover .dock-icon,
.dock-item:focus-visible .dock-icon {
  transform: translateY(-3px) scale(1.18);
}

.dock-item.on {
  color: var(--app-text);
}

.dock-item.on .dock-icon {
  transform: scale(1.05);
}

.dock-item.on::after {
  content: '';
  position: absolute;
  bottom: 5px;
    width: 3px;
    height: 3px;
  border-radius: 50%;
  background: var(--app-text);
}

.dock-tip {
  position: absolute;
  bottom: calc(100% + 14px);
  left: 50%;
  z-index: 2;
  width: max-content;
  max-width: 140px;
  padding: 5px 8px;
  border: 1px solid var(--app-line);
  border-radius: 9px;
  background: var(--app-panel);
  color: var(--app-text);
  font-size: 12px;
  line-height: 1.2;
  white-space: nowrap;
  opacity: 0;
  pointer-events: none;
  transform: translate(-50%, 5px);
  transition:
    opacity 0.16s ease,
    transform 0.16s ease;
}

.dock-item:hover .dock-tip,
.dock-item:focus-visible .dock-tip {
  opacity: 1;
  transform: translate(-50%, 0);
}

.dock-divider {
  width: 1px;
  height: 30px;
  margin: 0 2px 6px;
  background: var(--app-line);
}

@media (max-width: 760px) {
  .dock-wrap {
    bottom: 10px;
  }

  .dock {
    gap: 3px;
    min-height: 56px;
    padding: 6px 8px;
    border-radius: 18px;
  }

  .dock-item {
    width: 38px;
    height: 38px;
  }

  .dock-item:hover .dock-icon,
  .dock-item:focus-visible .dock-icon,
  .dock-item.on .dock-icon {
    transform: none;
  }

  .dock-tip,
  .dock-divider {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .dock-item,
  .dock-item::before,
  .dock-icon,
  .dock-tip {
    transition: none;
  }
}
</style>
