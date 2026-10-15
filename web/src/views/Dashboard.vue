<script setup>
/**
 * 仪表盘：采集监控 → 指标卡 → 走势 / 状态构成 / 来源构成 → 账号、代理。
 * 采集开关按账号走，账号卡片上就能直接开关。
 */
import { computed, onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import {
  CircleCheck,
  CircleHelp,
  CircleX,
  KeyRound,
  Link2,
  PieChart,
  Radio,
  Server,
  TrendingUp,
  TriangleAlert,
  UserRound,
} from 'lucide-vue-next'

import * as api from '../api'
import {
  state,
  collectorStatus,
  loadListener,
  loadAuth,
  loadStats,
} from '../store'
import { DAILY_METRICS, fmtAgo, fmtTime } from '../format'
import { usePolling } from '../polling'
import ActivityStrip from '../components/ActivityStrip.vue'
import DailyChart from '../components/DailyChart.vue'
import AnimatedNumber from '../components/AnimatedNumber.vue'
import EmptyState from '../components/EmptyState.vue'
import RadarScan from '../components/RadarScan.vue'
import SkeletonBlock from '../components/SkeletonBlock.vue'
import StatusDonut from '../components/StatusDonut.vue'

const message = useMessage()
const l = computed(() => state.listener || {})
const s = computed(() => state.stats || {})

const kindType = { ok: 'success', warn: 'warning', off: 'default', unknown: 'default' }

/* ---------- 链接库：状态构成 + 来源构成 ---------- */
const lib = ref({ sources: [], status_counts: {}, status_labels: {} })
const libLoading = ref(false)
const liveReady = ref(false)
const accountRows = ref([])
const proxyRows = ref([])
const collectBusy = ref(0)

async function loadAccountRows() {
  try {
    const j = await api.get('/api/monitor')
    accountRows.value = j.accounts || []
  } catch {
    accountRows.value = []
  }
}

async function toggleCollect(row, enabled) {
  collectBusy.value = row.id
  const before = row.collect_enabled
  row.collect_enabled = enabled
  try {
    const j = await api.post(`/api/accounts/${row.id}/collect`, { enabled })
    if (j.accounts) accountRows.value = j.accounts
    message.success(enabled ? '采集已打开，程序下次启动也会自动开始' : '采集已关闭')
  } catch (e) {
    row.collect_enabled = before
    message.error(e.message)
  } finally {
    collectBusy.value = 0
    await loadAccountRows()
  }
}

function accountState(row) {
  if (!row.session_exists) return { type: 'default', label: '未登录' }
  if (!row.logged_in) return { type: 'warning', label: '未同步' }
  if (row.proxy_id && !row.proxy_ready) return { type: 'warning', label: '出口未就绪' }
  if (row.collecting) return { type: 'success', label: '采集中' }
  if (row.collect_enabled) return { type: 'warning', label: '已开启未运行' }
  if (row.is_active) return { type: 'primary', label: '在线 · 当前' }
  return { type: 'info', label: '在线 · 待命' }
}

const onlineCount = computed(
  () => accountRows.value.filter((row) => row.logged_in && row.session_exists).length
)
const runningCount = computed(
  () => accountRows.value.filter((row) => row.collecting).length
)
const enabledCount = computed(
  () => accountRows.value.filter((row) => row.collect_enabled).length
)
const watchedCount = computed(() =>
  accountRows.value.reduce((sum, row) => sum + (row.watched?.length || 0), 0)
)

async function loadProxyRows() {
  try {
    const j = await api.get('/api/proxies')
    proxyRows.value = j.proxies || []
  } catch {
    proxyRows.value = []
  }
}

/* ---------- 实时活动 ---------- */
const activity = ref({ series: [], messages: 0, links: 0 })
const activityLoading = ref(false)

async function loadActivity() {
  activityLoading.value = true
  try {
    activity.value = await api.get('/api/activity', { minutes: 60 })
  } catch {
    /* 活动条拿不到不影响别的 */
  } finally {
    activityLoading.value = false
  }
}

/* ---------- 监控指标块 ---------- */
const cq = computed(() => l.value.check_queue || null)
const monitorTiles = computed(() => [
  {
    key: 'running',
    label: '运行中账号',
    value: runningCount.value,
    hint: `共 ${accountRows.value.length} 个账号`,
    tone: runningCount.value ? 'ok' : 'off',
  },
  {
    key: 'enabled',
    label: '已开采集开关',
    value: enabledCount.value,
    hint: '程序启动时自动拉起',
    tone: enabledCount.value ? 'brand' : 'off',
  },
  {
    key: 'watched',
    label: '监控群组',
    value: watchedCount.value,
    hint: '按账号分别统计',
    tone: watchedCount.value ? 'brand' : 'off',
  },
  {
    key: 'queue',
    label: '待校验队列',
    value: cq.value?.pending ?? 0,
    hint: cq.value ? `已检 ${cq.value.done} 条` : '队列未启动',
    tone: cq.value?.pending ? 'warn' : 'off',
  },
])

const lastEvent = computed(() => {
  const ev = l.value.last_event || {}
  if (!ev.at && !ev.title) return ''
  const bits = [ev.title, ev.remark, ev.link].filter(Boolean)
  return `${fmtTime(ev.at)} · ${bits.join(' · ') || '有事件'}`
})

async function loadLib(showLoading = true) {
  if (showLoading) libLoading.value = true
  try {
    lib.value = await api.get('/api/admin/links', { size: 1 })
  } catch {
    lib.value = { sources: [], status_counts: {}, status_labels: {} }
  } finally {
    if (showLoading) libLoading.value = false
  }
}

/* ---------- 每日走势 ---------- */
const daily = ref({ series: [] })
const dailyLoading = ref(false)
const range = ref(30)
const metric = ref('new_links')

function setMetric(value) {
  metric.value = value
}

async function loadDaily(days) {
  if (days) range.value = days
  dailyLoading.value = true
  try {
    daily.value = await api.get('/api/stats/daily', { days: range.value })
  } catch {
    daily.value = { series: [] }
  } finally {
    dailyLoading.value = false
  }
}

const rangeText = computed(() => {
  const f = daily.value?.from
  const t = daily.value?.to
  if (!f || !t) return ''
  return `${f.slice(5)} ~ ${t.slice(5)} · 按天（UTC）`
})

/* ---------- 顶部指标卡 ---------- */
const cards = computed(() => {
  const v = s.value
  return [
    { k: '链接总数', n: v.shares, icon: Link2, sub: `原始消息 ${v.messages ?? '—'} 条` },
    { k: '有效', n: v.ok, icon: CircleCheck, sub: `有提取码 ${v.with_code ?? '—'} 条` },
    { k: '需提取码', n: v.need_code, icon: KeyRound, sub: `其中提取码错误 ${v.bad_code ?? '—'} 条` },
    { k: '已失效', n: v.gone, icon: CircleX, sub: '含已删除 / 已过期 / 违规' },
    { k: '未检测', n: v.unchecked, icon: CircleHelp, sub: `最后校验 ${fmtTime(v.last_checked_at)}` },
    { k: '校验出错', n: v.error, icon: TriangleAlert, sub: `最后入库 ${fmtTime(v.last_message_at)}` },
  ]
})

/* ---------- 来源构成 ---------- */
const SOURCE_PALETTE = [
  '#2563eb', '#12a594', '#e0a11f', '#e5484d',
  '#8b5cf6', '#0ea5e9', '#f97316', '#64748b',
]
const sourceCounts = computed(() =>
  Object.fromEntries(
    (lib.value.sources || []).map((x) => [x.source || '（未知来源）', x.total || 0])
  )
)
const sourceLabels = computed(() =>
  Object.fromEntries(Object.keys(sourceCounts.value).map((name) => [name, name]))
)

async function refreshAll() {
  await Promise.all([
    loadListener(),
    loadAuth(),
    loadStats(),
    loadLib(),
    loadAccountRows(),
    loadProxyRows(),
    loadActivity(),
  ])
  liveReady.value = true
  loadDaily()
}

async function refreshLive() {
  await Promise.all([
    loadLib(false),
    loadAccountRows(),
    loadProxyRows(),
    loadActivity(),
  ])
}

onMounted(refreshAll)
// 标签页切到后台时跳过整轮，切回来立刻补一次
usePolling(refreshLive, 5000)
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- ============ 采集监控 ============ -->
    <section class="monitor" :class="{ live: runningCount > 0 }">
      <div class="monitor__glow" aria-hidden="true" />

      <div class="monitor__head">
        <div class="flex items-center gap-2">
          <Radio :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">采集监控</span>
          <n-tag size="small" round :type="kindType[collectorStatus.kind]">
            {{ collectorStatus.label }}
          </n-tag>
        </div>
        <div class="monitor__meta">
          <span>心跳 {{ fmtAgo(l.heartbeat_age_seconds) }}</span>
          <span v-if="l.origin">· {{ l.origin }}</span>
        </div>
      </div>

      <div class="monitor__body">
        <RadarScan
          :running="runningCount > 0"
          :count="runningCount"
          :accounts="accountRows"
          :armed="liveReady"
        />

        <div class="monitor__tiles">
          <div v-for="t in monitorTiles" :key="t.key" class="tile" :class="`tile--${t.tone}`">
            <div class="tile__k">{{ t.label }}</div>
            <div class="tile__n">
              <SkeletonBlock v-if="!liveReady" width="42px" height="26px" />
              <AnimatedNumber v-else :value="t.value" :armed="liveReady" />
            </div>
            <div class="tile__h">{{ t.hint }}</div>
          </div>
        </div>

        <div class="monitor__feed">
          <ActivityStrip :series="activity.series || []" :loading="activityLoading" />
          <div class="monitor__feed-foot">
            <span v-if="lastEvent" class="truncate" :title="lastEvent">
              最近采集：{{ lastEvent }}
            </span>
            <span v-else class="faint">还没有采集事件</span>
            <span v-if="cq" class="faint whitespace-nowrap">
              即时校验 已检 {{ cq.done }}<template v-if="cq.failed"> · 失败 {{ cq.failed }}</template>
            </span>
          </div>
        </div>
      </div>

      <n-alert
        v-if="(l.problems || []).length"
        type="warning"
        :show-icon="true"
        class="mt-3"
      >
        {{ (l.problems || []).join('；') }}
      </n-alert>

      <p class="monitor__note">
        采集由「账号监控」页每个账号自己的开关控制，程序启动时自动开始；这里只显示状态。
      </p>
    </section>

    <!-- ============ 指标卡 ============ -->
    <div class="grid gap-3 grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
      <div v-for="c in cards" :key="c.k" class="kpi">
        <div class="k">
          <span class="ico"><component :is="c.icon" :size="14" /></span>
          <span class="truncate">{{ c.k }}</span>
        </div>
        <div class="n">
          <SkeletonBlock v-if="!liveReady" width="76px" height="30px" />
          <AnimatedNumber v-else :value="c.n ?? '—'" :armed="liveReady" />
        </div>
        <div class="d truncate" :title="c.sub">
          <SkeletonBlock v-if="!liveReady" width="120px" height="12px" />
          <template v-else>{{ c.sub }}</template>
        </div>
      </div>
    </div>

    <!-- ============ 走势 + 状态构成 + 来源构成 ============ -->
    <div class="grid gap-4 xl:grid-cols-[minmax(0,1.7fr)_minmax(280px,0.8fr)_minmax(280px,0.8fr)]">
      <n-card size="small" :bordered="true">
        <template #header>
          <div class="flex items-center gap-2">
            <TrendingUp :size="16" class="text-[var(--app-brand)]" />
            <span class="font-semibold">每日走势</span>
            <span class="text-[12.5px] muted hidden md:inline">{{ rangeText }}</span>
          </div>
        </template>
        <template #header-extra>
          <div class="flex items-center gap-2">
            <div class="trend-switch" role="group" aria-label="走势指标">
              <button
                v-for="m in DAILY_METRICS"
                :key="m.key"
                type="button"
                class="trend-switch__item"
                :class="{ active: metric === m.key }"
                @click="setMetric(m.key)"
              >
                {{ m.label }}
              </button>
            </div>
            <div class="trend-switch" role="group" aria-label="统计范围">
              <button
                v-for="days in [7, 30, 90]"
                :key="days"
                type="button"
                class="trend-switch__item"
                :class="{ active: range === days }"
                @click="loadDaily(days)"
              >
                {{ days }} 天
              </button>
            </div>
          </div>
        </template>
        <DailyChart :series="daily.series || []" :metric="metric" :loading="dailyLoading" />
      </n-card>

      <n-card size="small">
        <template #header>
          <div class="flex items-center gap-2">
            <PieChart :size="16" class="text-[var(--app-brand)]" />
            <span class="font-semibold">状态构成</span>
            <span class="ml-auto text-[12.5px] muted">
              共 <AnimatedNumber :value="lib.total ?? s.shares ?? '—'" :armed="liveReady" /> 条
            </span>
          </div>
        </template>
        <StatusDonut
          :counts="lib.status_counts || {}"
          :labels="lib.status_labels || {}"
          :loading="libLoading"
          :armed="liveReady"
        />
      </n-card>

      <n-card size="small">
        <template #header>
          <div class="flex items-center gap-2">
            <PieChart :size="16" class="text-[var(--app-brand)]" />
            <span class="font-semibold">来源构成</span>
            <span class="ml-auto text-[12.5px] muted">
              <AnimatedNumber
                :value="Object.keys(sourceCounts).length"
                :armed="liveReady"
              /> 个来源
            </span>
          </div>
        </template>
        <StatusDonut
          :counts="sourceCounts"
          :labels="sourceLabels"
          :palette="SOURCE_PALETTE"
          center-label="来源链接"
          :loading="libLoading"
          :armed="liveReady"
        />
      </n-card>
    </div>

    <!-- ============ 账号 + 代理 ============ -->
    <div class="grid gap-4 lg:grid-cols-2">
      <n-card size="small">
        <template #header>
          <div class="flex items-center gap-2">
            <UserRound :size="16" class="text-[var(--app-brand)]" />
            <span class="font-semibold">Telegram 账号</span>
            <n-tag class="ml-auto" size="small" round :type="onlineCount ? 'success' : 'default'">
              <AnimatedNumber :value="onlineCount" :armed="liveReady" /> /
              <AnimatedNumber :value="accountRows.length" :armed="liveReady" /> 在线
            </n-tag>
          </div>
        </template>
        <div v-if="accountRows.length" class="flex flex-col gap-2 max-h-[280px] overflow-auto pr-1">
          <div v-for="row in accountRows" :key="row.id" class="row-card">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span class="font-medium truncate">{{ row.name || row.phone || `#${row.id}` }}</span>
                <n-tag size="tiny" round :type="accountState(row).type">
                  {{ accountState(row).label }}
                </n-tag>
              </div>
              <div class="text-[12.5px] muted mt-1">
                {{ row.phone || '未填手机号' }}
                <span class="ml-2">
                  监控
                  <AnimatedNumber :value="row.watched?.length || 0" :armed="liveReady" />
                  群
                </span>
              </div>
            </div>
            <n-switch
              size="small"
              :value="row.collect_enabled || row.collecting"
              :loading="collectBusy === row.id"
              @update:value="(v) => toggleCollect(row, v)"
            />
          </div>
        </div>
        <div v-else-if="!liveReady" class="flex flex-col gap-2">
          <SkeletonBlock v-for="i in 2" :key="i" height="58px" radius="10px" />
        </div>
        <EmptyState
          v-else
          :icon="UserRound"
          title="还没有账号"
          hint="到「账号监控」添加一个 Telegram 账号，填好 api_id / api_hash 后登录，再绑定代理。"
        />
        <template #action>
          <n-button size="small" secondary @click="$router.push('/admin/account')">
            <template #icon><UserRound :size="15" /></template>
            账号监控
          </n-button>
        </template>
      </n-card>

      <n-card size="small">
        <template #header>
          <div class="flex items-center gap-2">
            <Server :size="16" class="text-[var(--app-brand)]" />
            <span class="font-semibold">代理池</span>
            <n-tag class="ml-auto" size="small" round :type="proxyRows.length ? 'success' : 'default'">
              <AnimatedNumber :value="proxyRows.length" :armed="liveReady" /> 条代理
            </n-tag>
          </div>
        </template>
        <div v-if="proxyRows.length" class="flex flex-col gap-2 max-h-[280px] overflow-auto pr-1">
          <div v-for="row in proxyRows" :key="row.id" class="row-card">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <span class="font-medium truncate">{{ row.name || row.addr }}</span>
                <n-tag class="ml-auto" size="tiny" round>{{ row.proxy_type.toUpperCase() }}</n-tag>
              </div>
              <div class="mono text-[12.5px] muted mt-1 truncate">{{ row.addr }}:{{ row.port }}</div>
            </div>
          </div>
        </div>
        <div v-else-if="!liveReady" class="flex flex-col gap-2">
          <SkeletonBlock v-for="i in 2" :key="i" height="52px" radius="10px" />
        </div>
        <EmptyState
          v-else
          :icon="Server"
          title="还没有代理"
          hint="Telegram 采集必须走代理。到「设置 → 代理池」添加 http / https / socks5 代理，再到账号上绑定。"
        />
        <template #action>
          <n-button size="small" secondary @click="$router.push('/admin/settings')">
            <template #icon><Server :size="15" /></template>
            管理代理
          </n-button>
        </template>
      </n-card>
    </div>
  </div>
</template>

<style scoped>
/* ==========================================================================
   采集监控面板：仪表盘的主角，做成一块「仪表」而不是普通卡片
   ========================================================================== */
.monitor {
  position: relative;
  overflow: hidden;
  padding: 18px 20px 14px;
  border: 1px solid var(--app-line);
  border-radius: 20px;
  background:
    radial-gradient(
      120% 140% at 8% 0%,
      color-mix(in srgb, var(--app-brand) 7%, transparent),
      transparent 58%
    ),
    var(--app-panel);
  transition: border-color var(--dur) ease;
}

/* 在采时描边带一点强调色，一眼区分「在跑 / 没跑」 */
.monitor.live {
  border-color: color-mix(in srgb, var(--app-brand) 34%, var(--app-line));
}

.monitor__glow {
  position: absolute;
  top: -140px;
  left: -80px;
  width: 380px;
  height: 380px;
  border-radius: 50%;
  background: radial-gradient(
    circle,
    color-mix(in srgb, var(--app-brand) 16%, transparent),
    transparent 68%
  );
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.6s ease;
}

.monitor.live .monitor__glow {
  opacity: 1;
  animation: monitor-breathe 5.5s ease-in-out infinite;
}

.monitor__head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.monitor__meta {
  margin-left: auto;
  display: flex;
  gap: 6px;
  color: var(--app-faint);
  font-size: 12.5px;
}

.monitor__body {
  position: relative;
  display: grid;
  gap: 22px;
  grid-template-columns: auto minmax(220px, 300px) minmax(0, 1fr);
  align-items: center;
}

.monitor__tiles {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.tile {
  padding: 12px 14px;
  border: 1px solid var(--app-line-soft);
  border-radius: 14px;
  background: color-mix(in srgb, var(--app-bg) 55%, transparent);
  transition: border-color var(--dur-fast) ease, transform var(--dur) var(--ease-spring);
}

.tile:hover {
  transform: translateY(-2px);
}

.tile__k {
  color: var(--app-muted);
  font-size: 12px;
}

.tile__n {
  margin-top: 5px;
  font-size: 25px;
  font-weight: 650;
  line-height: 1;
  letter-spacing: -0.4px;
  font-variant-numeric: tabular-nums;
}

.tile__h {
  margin-top: 6px;
  color: var(--app-faint);
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tile--ok .tile__n {
  color: var(--app-ok);
}
.tile--warn .tile__n {
  color: var(--app-warn);
}
.tile--brand .tile__n {
  color: var(--app-brand);
}

.monitor__feed {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.monitor__feed-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
  font-size: 12.5px;
  color: var(--app-muted);
}

.monitor__note {
  margin: 14px 0 0;
  color: var(--app-faint);
  font-size: 12px;
}

@keyframes monitor-breathe {
  0%,
  100% {
    opacity: 0.75;
    transform: scale(1);
  }
  50% {
    opacity: 1;
    transform: scale(1.06);
  }
}

@media (max-width: 1180px) {
  .monitor__body {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .monitor__feed {
    grid-column: 1 / -1;
  }
}

@media (max-width: 720px) {
  .monitor {
    padding: 16px 14px 12px;
  }
  .monitor__body {
    grid-template-columns: minmax(0, 1fr);
    justify-items: center;
    gap: 16px;
  }
  .monitor__tiles {
    width: 100%;
  }
  .monitor__feed {
    width: 100%;
  }
}

/* ---------- 账号 / 代理列表 ---------- */
.row-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--app-line-soft);
  border-radius: 10px;
  transition:
    background var(--dur-fast) ease,
    border-color var(--dur-fast) ease;
}

.row-card:hover {
  background: var(--app-panel-2);
  border-color: var(--app-line);
}

/* ---------- 走势图切换 ---------- */
.trend-switch {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--app-line);
  border-radius: 10px;
  background: var(--app-panel-2);
}

.trend-switch__item {
  height: 26px;
  padding: 0 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--app-muted);
  font-family: inherit;
  font-size: 12px;
  line-height: 26px;
  white-space: nowrap;
  cursor: pointer;
  transition:
    color 0.15s ease,
    background-color 0.15s ease,
    box-shadow 0.15s ease;
}

.trend-switch__item:hover {
  color: var(--app-text);
  background: color-mix(in srgb, var(--app-panel) 70%, transparent);
}

.trend-switch__item.active {
  color: var(--app-text);
  background: var(--app-panel);
  box-shadow: 0 1px 2px color-mix(in srgb, var(--app-text) 12%, transparent);
}

@media (prefers-reduced-motion: reduce) {
  .monitor__glow,
  .tile {
    transition: none;
    animation: none;
  }
}
</style>
