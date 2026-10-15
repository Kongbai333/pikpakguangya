<script setup>
/**
 * 左侧导航栏。
 *
 * 取代了原来的底部浮动导航：8 个页面横排成一条 dock，既占底部空间又看不出
 * 层级，页面一多就得靠图标猜。侧边栏能显示文字标签和分组，还能折叠成图标条。
 *
 * 窄屏（<=900px）下不占位，变成盖在内容上的抽屉，由顶栏的汉堡按钮控制。
 */
import { RouterLink, useRoute } from 'vue-router'
import { PanelLeftClose, PanelLeftOpen, X } from 'lucide-vue-next'

defineProps({
  groups: { type: Array, default: () => [] },
  collapsed: { type: Boolean, default: false },
  dots: { type: Array, default: () => [] },
  mobileOpen: { type: Boolean, default: false },
  dotColor: { type: Object, default: () => ({}) },
})

const emit = defineEmits(['toggle-collapse', 'close-mobile'])

const route = useRoute()

function isActive(item) {
  return item.names?.includes(route.name) || route.path === item.to
}
</script>

<template>
  <div
    class="side-scrim"
    :class="{ on: mobileOpen }"
    aria-hidden="true"
    @click="emit('close-mobile')"
  />

  <aside
    class="side"
    :class="{ mini: collapsed, 'mobile-open': mobileOpen }"
    aria-label="后台导航"
  >
    <div class="side-head">
      <RouterLink to="/admin" class="side-brand" @click="emit('close-mobile')">
        <span class="brand-mark">PK</span>
        <span class="side-brand__text">
          <span class="t">PikPak 链接库</span>
          <span class="s">采集 · 校验 · 秒传</span>
        </span>
      </RouterLink>

      <button
        class="side-close"
        type="button"
        title="收起导航"
        @click="emit('close-mobile')"
      >
        <X :size="18" />
      </button>
    </div>

    <nav class="side-nav">
      <template v-for="group in groups" :key="group.label">
        <div class="side-group">{{ group.label }}</div>
        <RouterLink
          v-for="item in group.items"
          :key="item.to"
          :to="item.to"
          class="side-item"
          :class="{ on: isActive(item) }"
          :title="collapsed ? item.label : ''"
          @click="emit('close-mobile')"
        >
          <span class="side-item__icon">
            <component :is="item.icon" :size="18" :stroke-width="1.8" />
          </span>
          <span class="side-item__label">{{ item.label }}</span>
        </RouterLink>
      </template>
    </nav>

    <div class="side-foot">
      <RouterLink
        v-for="d in dots"
        :key="d.key"
        to="/admin"
        class="side-stat"
        :title="`${d.name}：${d.label} · ${d.detail}`"
      >
        <span class="side-stat__dot" :style="{ color: dotColor[d.kind] }" />
        <span class="side-stat__body">
          <span class="side-stat__name">{{ d.name }}</span>
          <span class="side-stat__value" :style="{ color: dotColor[d.kind] }">
            {{ d.label }}
          </span>
        </span>
      </RouterLink>

      <button
        class="side-collapse"
        type="button"
        :title="collapsed ? '展开导航' : '收起导航'"
        @click="emit('toggle-collapse')"
      >
        <component :is="collapsed ? PanelLeftOpen : PanelLeftClose" :size="16" />
        <span class="side-collapse__label">收起</span>
      </button>
    </div>
  </aside>
</template>

<style scoped>
.side {
  position: sticky;
  top: 0;
  z-index: 30;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  width: var(--side-w);
  height: 100vh;
  background: var(--app-panel);
  border-right: 1px solid var(--app-line);
  transition: width var(--dur) var(--ease-out), transform var(--dur) var(--ease-out);
}

.side.mini {
  width: var(--side-w-mini);
}

/* ---------- 顶部品牌 ---------- */
.side-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px 14px;
}

.side-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  color: inherit;
  text-decoration: none;
}

.side-brand__text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  white-space: nowrap;
}

.side-brand__text .t {
  font-size: 14px;
  font-weight: 700;
  line-height: 1.2;
  color: var(--app-text);
}

.side-brand__text .s {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--app-faint);
}

.side-close {
  display: none;
  margin-left: auto;
  padding: 6px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--app-muted);
  cursor: pointer;
}

/* ---------- 导航 ---------- */
.side-nav {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 4px 10px 12px;
}

.side-group {
  padding: 14px 10px 6px;
  color: var(--app-faint);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.7px;
  text-transform: uppercase;
}

.side-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 11px;
  margin-bottom: 2px;
  padding: 9px 10px;
  border-radius: 10px;
  color: var(--app-muted);
  font-size: 13.5px;
  text-decoration: none;
  white-space: nowrap;
  transition:
    background var(--dur-fast) ease,
    color var(--dur-fast) ease;
}

.side-item__icon {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  transition: transform var(--dur) var(--ease-spring);
}

.side-item__label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.side-item:hover {
  background: var(--app-panel-2);
  color: var(--app-text);
}

.side-item:hover .side-item__icon {
  transform: scale(1.08);
}

.side-item.on {
  background: var(--app-brand-soft);
  color: var(--app-brand);
  font-weight: 600;
}

/* 左侧的强调条：一眼看出当前在哪一页 */
.side-item.on::before {
  content: '';
  position: absolute;
  left: -10px;
  top: 50%;
  width: 3px;
  height: 18px;
  border-radius: 0 3px 3px 0;
  background: var(--app-brand);
  transform: translateY(-50%);
}

/* ---------- 底部：状态 + 折叠 ---------- */
.side-foot {
  padding: 10px;
  border-top: 1px solid var(--app-line);
}

.side-stat {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 10px;
  border-radius: 9px;
  color: var(--app-muted);
  font-size: 12.5px;
  text-decoration: none;
  transition: background var(--dur-fast) ease;
}

.side-stat:hover {
  background: var(--app-panel-2);
}

.side-stat__dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 18%, transparent);
}

.side-stat__body {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
}

.side-stat__name {
  color: var(--app-faint);
}

.side-stat__value {
  font-weight: 600;
  white-space: nowrap;
}

.side-collapse {
  display: flex;
  align-items: center;
  gap: 9px;
  width: 100%;
  margin-top: 4px;
  padding: 8px 10px;
  border: 0;
  border-radius: 9px;
  background: transparent;
  color: var(--app-faint);
  font-family: inherit;
  font-size: 12.5px;
  cursor: pointer;
  transition: background var(--dur-fast) ease, color var(--dur-fast) ease;
}

.side-collapse:hover {
  background: var(--app-panel-2);
  color: var(--app-text);
}

/* ---------- 折叠态 ---------- */
.side.mini .side-brand__text,
.side.mini .side-group,
.side.mini .side-item__label,
.side.mini .side-stat__body,
.side.mini .side-collapse__label {
  display: none;
}

.side.mini .side-head {
  justify-content: center;
  padding: 18px 8px 14px;
}

.side.mini .side-item,
.side.mini .side-stat,
.side.mini .side-collapse {
  justify-content: center;
  padding: 9px 0;
}

/* ---------- 窄屏：抽屉 ---------- */
.side-scrim {
  display: none;
}

@media (max-width: 900px) {
  .side {
    position: fixed;
    left: 0;
    top: 0;
    z-index: 60;
    transform: translateX(-100%);
    box-shadow: var(--shadow-3);
  }

  .side.mobile-open {
    transform: none;
  }

  .side.mini {
    width: var(--side-w);
  }

  .side.mini .side-brand__text,
  .side.mini .side-group,
  .side.mini .side-item__label,
  .side.mini .side-stat__body,
  .side.mini .side-collapse__label {
    display: initial;
  }

  .side.mini .side-head {
    justify-content: flex-start;
    padding: 18px 16px 14px;
  }

  .side.mini .side-item,
  .side.mini .side-stat,
  .side.mini .side-collapse {
    justify-content: flex-start;
    padding: 9px 10px;
  }

  .side-close {
    display: grid;
    place-items: center;
  }

  .side-collapse {
    display: none;
  }

  .side-scrim {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 55;
    background: color-mix(in srgb, #0b0b0d 44%, transparent);
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--dur) ease;
  }

  .side-scrim.on {
    opacity: 1;
    pointer-events: auto;
  }
}

@media (prefers-reduced-motion: reduce) {
  .side,
  .side-item__icon,
  .side-scrim {
    transition: none;
  }
}
</style>
