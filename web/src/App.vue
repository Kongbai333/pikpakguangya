<script setup>
/**
 * 三套外壳：登录页、前台浏览页、管理后台。
 * 管理后台的登录态由路由守卫和后端 Cookie 共同保证。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { darkTheme, dateZhCN, zhCN } from 'naive-ui'
import {
  ArrowUp,
  CloudUpload,
  Image,
  LayoutDashboard,
  Link2,
  LogOut,
  Menu,
  Moon,
  RefreshCw,
  ScrollText,
  Search,
  Settings,
  Sun,
  UserRound,
  View,
} from 'lucide-vue-next'

import CommandPalette from './components/CommandPalette.vue'
import Sidebar from './components/Sidebar.vue'
import { logoutWeb } from './auth'
import { dark, toggleTheme } from './theme'
import {
  accountStatus,
  collectorStatus,
  loadOverview,
  startPolling,
  state,
  stopPolling,
} from './store'
import './workbuddy-theme.css'

const route = useRoute()
const router = useRouter()
const isAdmin = computed(() => route.path.startsWith('/admin'))
const isLogin = computed(() => route.name === 'login')

const NAV = [
  {
    label: '总览',
    items: [
      { to: '/admin', label: '仪表盘', icon: LayoutDashboard, names: ['dashboard'] },
      { to: '/admin/links', label: '链接', icon: Link2, names: ['links'] },
      { to: '/admin/media', label: '媒体', icon: Image, names: ['media'] },
      { to: '/admin/transfers', label: '秒传', icon: CloudUpload, names: ['transfers'] },
    ],
  },
  {
    label: '系统',
    items: [
      { to: '/admin/account', label: '账号监控', icon: UserRound, names: ['account'] },
      { to: '/admin/logs', label: '日志', icon: ScrollText, names: ['logs'] },
      { to: '/admin/settings', label: '设置', icon: Settings, names: ['settings'] },
    ],
  },
]

/** 命令面板的候选：所有页面 + 前台浏览 */
const palettePages = computed(() => [
  ...NAV.flatMap((group) => group.items.map((item) => ({ ...item, group: group.label }))),
  { to: '/', label: '前台浏览', icon: View, names: ['browse'], group: '其它' },
])

const TITLES = {
  browse: ['链接库', '按提取码、状态筛选，点一下复制'],
  login: ['管理员登录', ''],
  dashboard: ['仪表盘', '采集、账号、链接的整体状态'],
  links: ['链接管理', '校验、筛选、隐藏、删除'],
  media: ['媒体瀑布流', '只缓存缩略图，原媒体跳转 Telegram'],
  transfers: ['秒传到光鸭', 'PikPak 分享按 GCID 秒传到光鸭云盘'],
  logs: ['日志', '每个账号的采集日志和服务日志'],
  account: ['账号与监控', '采集开关、代理、群组勾选都按账号走'],
  settings: ['设置', '应用凭据、代理池、PikPak 详情和环境信息'],
}
const head = computed(() => TITLES[route.name] || ['PikPak 链接库', ''])

const themeOverrides = computed(() => {
  // 交互色统一成强调蓝：链接、可点图标、主按钮、选中态用同一个颜色。
  // 外壳（面板 / 边框 / 正文）保持中性灰阶，所以整体调性没变，
  // 只是「哪里能点」一眼能看出来。
  const light = {
    primaryColor: '#2563eb',
    primaryColorHover: '#1d4ed8',
    primaryColorPressed: '#1e40af',
    primaryColorSuppl: '#3b82f6',
    bodyColor: '#ffffff',
    cardColor: '#f6f6f7',
    modalColor: '#ffffff',
    popoverColor: '#ffffff',
    tableColor: '#f6f6f7',
    inputColor: '#ffffff',
    borderColor: '#e4e4e7',
    dividerColor: '#ededf0',
    textColor1: '#18181b',
    textColor2: '#52525b',
    textColor3: '#71717a',
    placeholderColor: '#a1a1aa',
  }
  const darkValues = {
    primaryColor: '#60a5fa',
    primaryColorHover: '#93c5fd',
    primaryColorPressed: '#3b82f6',
    primaryColorSuppl: '#60a5fa',
    bodyColor: '#111113',
    cardColor: '#1d1d20',
    modalColor: '#202023',
    popoverColor: '#202023',
    tableColor: '#1d1d20',
    inputColor: '#26262a',
    borderColor: '#303034',
    dividerColor: '#28282c',
    textColor1: '#f4f4f5',
    textColor2: '#d4d4d8',
    textColor3: '#a1a1aa',
    placeholderColor: '#71717a',
  }
  return {
    common: {
      ...(dark.value ? darkValues : light),
      borderRadius: '10px',
      borderRadiusSmall: '8px',
      fontFamily:
        "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif",
      fontSize: '14px',
    },
    Card: { borderRadius: '16px' },
    Button: {
      borderRadiusMedium: '10px',
      borderRadiusSmall: '9px',
      // 深色主题下主色是浅蓝，按钮文字要用深色才够对比
      textColorPrimary: dark.value ? '#111113' : '#ffffff',
      textColorHoverPrimary: dark.value ? '#111113' : '#ffffff',
      textColorPressedPrimary: dark.value ? '#111113' : '#ffffff',
    },
    // 表格走 Naive 自己的主题变量，比在 CSS 里 !important 覆盖干净得多。
    // 目标是「表格消失在页面里」：表头无底色、行分隔线极淡、悬停才浮出层次。
    DataTable: {
      borderRadius: '14px',
      // ⚠️ 这几个颜色必须**不透明**。冻结列（fixed: left/right）是靠
      // --n-td-color 盖住下面滚动的内容的，设成 transparent 就会透出重叠。
      // 取值和卡片底色一致，看起来仍像「没有底色」。
      thColor: dark.value ? '#1d1d20' : '#f6f6f7',
      thColorHover: dark.value ? '#27272b' : '#ebebef',
      thTextColor: dark.value ? '#8b8b93' : '#8f8f98',
      thFontWeight: '600',
      thPaddingMedium: '11px 12px',
      tdColor: dark.value ? '#1d1d20' : '#f6f6f7',
      tdColorHover: dark.value ? '#27272b' : '#ebebef',
      tdColorStriped: dark.value ? '#212125' : '#f1f1f4',
      tdColorModal: dark.value ? '#1d1d20' : '#f6f6f7',
      tdColorHoverModal: dark.value ? '#27272b' : '#ebebef',
      tdColorStripedModal: dark.value ? '#212125' : '#f1f1f4',
      tdColorPopover: dark.value ? '#1d1d20' : '#f6f6f7',
      tdColorHoverPopover: dark.value ? '#27272b' : '#ebebef',
      tdColorStripedPopover: dark.value ? '#212125' : '#f1f1f4',
      tdPaddingMedium: '12px 12px',
      borderColor: dark.value ? '#28282c' : '#ededf0',
      lineHeight: '1.55',
      fontSizeMedium: '13.5px',
    },
    Pagination: { itemFontSizeMedium: '13px' },
    Tag: { borderRadius: '999px' },
    Input: { borderRadius: '10px' },
  }
})

const dots = computed(() => [
  { key: 'collector', name: '采集', ...collectorStatus.value },
  { key: 'account', name: '账号', ...accountStatus.value },
])

const dotColor = {
  ok: 'var(--app-ok)',
  warn: 'var(--app-warn)',
  off: 'var(--app-faint)',
  unknown: 'var(--app-faint)',
}

const offline = computed(() => !!state.errors.listener && !!state.errors.auth)

const clock = (d) =>
  `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
const updatedAt = computed(() => (state.updatedAt ? clock(new Date(state.updatedAt)) : ''))

const refreshing = ref(false)
async function refresh() {
  refreshing.value = true
  await loadOverview()
  refreshing.value = false
}

async function logoutAdmin() {
  await logoutWeb()
  stopPolling()
  await router.replace('/login')
}

/* ---------- 侧边栏折叠（记住选择） ---------- */
const SIDE_KEY = 'pk.sidebar.collapsed'
const collapsed = ref(localStorage.getItem(SIDE_KEY) === '1')
const mobileOpen = ref(false)
watch(collapsed, (value) => {
  try {
    localStorage.setItem(SIDE_KEY, value ? '1' : '0')
  } catch {
    /* 隐私模式下 localStorage 可能不可写 */
  }
})
// 换页时自动收起抽屉，否则点完菜单抽屉还盖在内容上
watch(() => route.fullPath, () => {
  mobileOpen.value = false
})

/* ---------- 命令面板 ---------- */
const paletteOpen = ref(false)

function onGlobalKey(event) {
  // ⌘K / Ctrl+K 开面板。用 key.toLowerCase() 兼容大写锁定和 Shift 组合
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    paletteOpen.value = !paletteOpen.value
  }
}

/* ---------- 回到顶部 ---------- */
const scrolled = ref(false)
function onScroll() {
  scrolled.value = window.scrollY > 400
}
function toTop() {
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

onMounted(() => {
  window.addEventListener('keydown', onGlobalKey)
  window.addEventListener('scroll', onScroll, { passive: true })
})
onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKey)
  window.removeEventListener('scroll', onScroll)
  stopPolling()
})

watch(
  isAdmin,
  async (enabled) => {
    if (!enabled) {
      stopPolling()
      return
    }
    await loadOverview()
    startPolling()
  },
  { immediate: true }
)
</script>

<template>
  <n-config-provider
    :theme="dark ? darkTheme : null"
    :theme-overrides="themeOverrides"
    :locale="zhCN"
    :date-locale="dateZhCN"
  >
    <n-global-style />
    <n-message-provider>
      <n-dialog-provider>
        <n-notification-provider>
          <!-- 登录页 -->
          <RouterView v-if="isLogin" v-slot="{ Component }">
            <component :is="Component" :key="route.fullPath" />
          </RouterView>

          <!-- 前台：给访客看的浏览页。
               头部、搜索栏、页脚都由 Browse.vue 自己带 —— 之前这里再套一层
               顶栏会和页面里的标题重复一遍。 -->
          <RouterView v-else-if="!isAdmin" v-slot="{ Component }">
            <component :is="Component" :key="route.fullPath" class="page-enter" />
          </RouterView>

          <!-- 管理后台：左侧导航 + 右侧内容 -->
          <div v-else class="app-shell">
            <div class="app-ambient" aria-hidden="true" />

            <Sidebar
              :groups="NAV"
              :collapsed="collapsed"
              :dots="dots"
              :dot-color="dotColor"
              :mobile-open="mobileOpen"
              @toggle-collapse="collapsed = !collapsed"
              @close-mobile="mobileOpen = false"
            />

            <div class="app-main">
              <header class="app-topbar">
                <button
                  class="icon-btn app-burger"
                  type="button"
                  title="打开导航"
                  @click="mobileOpen = true"
                >
                  <Menu :size="18" />
                </button>

                <div class="app-heading">
                  <h1>{{ head[0] }}</h1>
                  <p v-if="head[1]">{{ head[1] }}</p>
                </div>

                <div class="app-actions">
                  <span v-if="updatedAt" class="app-updated">更新于 {{ updatedAt }}</span>
                  <button
                    class="cmd-trigger"
                    title="命令面板（Ctrl / ⌘ + K）"
                    @click="paletteOpen = true"
                  >
                    <Search :size="15" />
                    <span>搜索</span>
                    <kbd>Ctrl K</kbd>
                  </button>
                  <button class="icon-btn" title="立即刷新" @click="refresh">
                    <RefreshCw :size="16" :class="{ 'is-spin': refreshing }" />
                  </button>
                  <button
                    class="icon-btn"
                    :title="dark ? '切到浅色' : '切到深色'"
                    @click="toggleTheme"
                  >
                    <component :is="dark ? Sun : Moon" :size="16" />
                  </button>
                  <button class="icon-btn" title="退出管理后台" @click="logoutAdmin">
                    <LogOut :size="16" />
                  </button>
                </div>
              </header>

              <div v-if="offline" class="offline-bar">
                连不上后台接口：{{ state.errors.listener }}。确认服务在跑（
                <code class="mono">start.ps1</code>），并且改完代码后重启过。
              </div>

              <main class="app-content">
                <RouterView v-slot="{ Component }">
                  <component :is="Component" :key="route.fullPath" class="page-enter" />
                </RouterView>
              </main>
            </div>

            <Transition name="fade-up">
              <button v-if="scrolled" class="to-top" title="回到顶部" @click="toTop">
                <ArrowUp :size="17" />
              </button>
            </Transition>

            <CommandPalette
              v-model:open="paletteOpen"
              :pages="palettePages"
              :dark="dark"
              @refresh="refresh"
              @toggle-theme="toggleTheme"
              @logout="logoutAdmin"
            />
          </div>
        </n-notification-provider>
      </n-dialog-provider>
    </n-message-provider>
  </n-config-provider>
</template>

<style scoped>
.app-shell {
  position: relative;
  display: flex;
  min-height: 100vh;
  background: var(--app-bg);
}

/* 环境光：两团极淡的光晕固定在视口上，滚动时内容从上面滑过。
   纯色背景会让整页看起来是「一块板」，这一层负责把它变成「有深度的空间」。 */
.app-ambient {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(
      60% 38% at 22% -6%,
      color-mix(in srgb, var(--app-brand) 8%, transparent),
      transparent 70%
    ),
    radial-gradient(
      44% 30% at 92% 8%,
      color-mix(in srgb, var(--app-ok) 6%, transparent),
      transparent 70%
    );
  animation: ambient-drift 26s ease-in-out infinite;
}

@keyframes ambient-drift {
  0%, 100% { transform: translate3d(0, 0, 0) scale(1); }
  50% { transform: translate3d(0, -1.6%, 0) scale(1.05); }
}

.app-main {
  position: relative;
  z-index: 1;
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

/* 高度必须精确等于 --top-h：下面所有吸顶元素（设置页分区条、链接页批量条）
   都按它算偏移，一旦实际高度超出，它们就会被盖住一半。 */
.app-topbar {
  position: sticky;
  top: 0;
  z-index: 25;
  display: flex;
  align-items: center;
  gap: 16px;
  height: var(--top-h);
  padding: 0 26px;
  background: color-mix(in srgb, var(--app-bg) 82%, transparent);
  backdrop-filter: blur(16px) saturate(170%);
  border-bottom: 1px solid var(--app-line);
}

.app-burger {
  display: none;
}

.app-heading {
  min-width: 0;
}

.app-heading h1 {
  margin: 0;
  color: var(--app-text);
  font-size: 19px;
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: -0.2px;
}

.app-heading p {
  margin: 3px 0 0;
  color: var(--app-muted);
  font-size: 12.5px;
}

.app-actions {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-left: auto;
}

.app-updated {
  color: var(--app-muted);
  font-size: 12.5px;
  white-space: nowrap;
}

.app-content {
  flex: 1;
  width: 100%;
  max-width: var(--page-max);
  margin: 0 auto;
  /* 底部留白收窄：表格是按视口算高度的，底部留太多会直接把页面顶出一条滚动条 */
  padding: 24px 28px 48px;
}

.is-spin {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* 右下角，避开内容区 */
.to-top {
  position: fixed;
  right: 22px;
  bottom: 24px;
  z-index: 40;
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border: 1px solid var(--app-line);
  border-radius: 999px;
  background: color-mix(in srgb, var(--app-bg) 86%, transparent);
  backdrop-filter: blur(12px);
  color: var(--app-muted);
  cursor: pointer;
  box-shadow: var(--shadow-2);
  transition: color var(--dur-fast) ease, transform var(--dur-fast) ease;
}

.to-top:hover {
  color: var(--app-text);
  transform: translateY(-2px);
}

.fade-up-enter-active,
.fade-up-leave-active {
  transition: opacity var(--dur-fast) ease, transform var(--dur) var(--ease-out);
}

.fade-up-enter-from,
.fade-up-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

@media (max-width: 900px) {
  /* 窄屏顶栏只剩标题，不需要那么高。改的是同一个变量，
     吸顶元素的偏移会跟着一起变（:root 在 scoped 里匹配不到，写 .app-shell 就够）。 */
  .app-shell {
    --top-h: 56px;
  }

  .app-burger {
    display: grid;
  }

  .app-topbar {
    padding: 0 14px;
    gap: 10px;
  }

  .app-heading p {
    display: none;
  }

  .app-content {
    padding: 16px 14px 64px;
  }

  .app-updated {
    display: none;
  }
}

@media (max-width: 620px) {
  .app-heading h1 {
    font-size: 16px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .is-spin {
    animation: none;
  }
}
</style>
