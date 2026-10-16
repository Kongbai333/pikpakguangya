/**
 * 全局状态：采集 / 账号 / 统计。
 *
 * 轮询只在管理后台跑，这三块也都是后台要看的，所以放在一个 reactive 里统一拉，
 * 免得各页各来一份、互相打架。前台浏览页是只读卡片流，它只用公开接口
 * （`/api/items`），统计也从那边的响应里拿，不碰这里的东西。
 *
 * 统计走 `/api/admin/stats`：前台的 `/api/stats` 在关闭前台浏览后会一起 403，
 * 管理后台要是吃同一口，仪表盘就跟着空了。
 */
import { reactive, computed } from 'vue'
import * as api from './api'
// 起个别名：本模块自己也要导出 startPolling，同名会构成重复声明
import { startPolling as runPolling } from './polling'

export const state = reactive({
  listener: null,
  auth: null,
  stats: null,
  watch: [],
  // 每个块最近一次的错误（后端挂了时页面上要看得见，不能一直显示「加载中」）
  errors: { listener: '', auth: '', stats: '' },
  // 最近一次成功拿到数据的时间（顶栏「更新于 …」）
  updatedAt: 0,
  ready: false,
})

function setError(key, e) {
  state.errors[key] = e?.message || String(e)
}

export async function loadListener() {
  try {
    state.listener = await api.get('/api/listener')
    state.errors.listener = ''
    state.updatedAt = Date.now()
  } catch (e) {
    setError('listener', e)
  }
}

export async function loadAuth() {
  try {
    state.auth = await api.get('/api/auth/status')
    state.errors.auth = ''
  } catch (e) {
    setError('auth', e)
  }
}

export async function loadStats() {
  try {
    state.stats = (await api.get('/api/admin/stats')).stats
    state.errors.stats = ''
  } catch (e) {
    setError('stats', e)
  }
}

export async function loadWatch() {
  try {
    state.watch = (await api.get('/api/watch')).watched || []
  } catch {
    /* 勾选列表拿不到不影响别的 */
  }
}

export async function loadOverview() {
  await Promise.all([loadListener(), loadAuth(), loadStats(), loadWatch()])
  state.ready = true
}

/* ---------- 派生状态：给顶部状态点和首页卡片用 ---------- */

export const collectorStatus = computed(() => {
  const l = state.listener
  if (!l) return { kind: 'unknown', label: '未知', detail: state.errors.listener || '正在读取…' }
  if (l.running) {
    const count = (l.accounts || []).filter((x) => x.running).length
    return {
      kind: 'ok',
      label: '采集中',
      detail: `${count} 个账号在采 · 心跳 ${Math.round(l.heartbeat_age_seconds ?? 0)} 秒前`,
      raw: l,
    }
  }
  const enabled = (l.accounts || []).filter((x) => x.enabled).length
  return {
    kind: 'off',
    label: '未运行',
    detail: enabled ? `${enabled} 个账号已开启，等待启动` : '所有账号的采集开关都关着',
    raw: l,
  }
})

export const accountStatus = computed(() => {
  const a = state.auth
  if (!a) return { kind: 'unknown', label: '未知', detail: state.errors.auth || '正在读取…' }
  if (a.error && a.proxy_masked === '账号代理不可用') {
    return { kind: 'warn', label: '出口未就绪', detail: a.error, raw: a }
  }
  if (a.authorized && a.connected) {
    return { kind: 'ok', label: '已登录', detail: a.me?.name || a.me?.username || '已连接', raw: a }
  }
  if (a.authorized) return { kind: 'warn', label: '已登录·未连接', detail: '需要时会自动连', raw: a }
  return { kind: 'off', label: '未登录', detail: '去「账号」页登录', raw: a }
})

/* ---------- 轮询 ---------- */
let stopFast = null
let stopSlow = null

export function startPolling() {
  if (stopFast) return
  // 采集和统计最常变，5 秒；账号状态 15 秒。
  // 两个定时器都走 runPolling：标签页在后台时整轮跳过。
  stopFast = runPolling(() => {
    loadListener()
    loadStats()
  }, 5000)
  stopSlow = runPolling(() => {
    loadAuth()
  }, 15000)
}

export function stopPolling() {
  stopFast?.()
  stopSlow?.()
  stopFast = stopSlow = null
}
