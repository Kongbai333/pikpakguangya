/** 展示层的格式化与状态映射，几个页面共用。 */

export function fmtTime(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function fmtAgo(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} 秒前`
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  return `${Math.floor(s / 86400)} 天前`
}

export function fmtBytes(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 1024) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n
  let i = -1
  do {
    v /= 1024
    i += 1
  } while (v >= 1024 && i < units.length - 1)
  return `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${units[i]}`
}

/**
 * 校验状态 → Naive UI tag 的 type + 中文。
 *
 * 除了我们自己定义的 4 种，PikPak 还会直接返回 DELETED / EXPIRED /
 * SENSITIVE_RESOURCE，所以这里都得有；认不出来的原样显示，别装作「未检测」。
 *
 * 注意 PASS_CODE_EMPTY 是 PikPak 的原话「这个分享需要提取码才能访问」，
 * **不是**「我们没采到提取码」。所以行内标签要用 rowStatus()：有码显示「有提取码」，
 * 没码才显示「缺提取码」。光看 checkStatus() 会把带码的分享误判成缺码。
 */
export const CHECK_STATUS = {
  OK: { type: 'success', label: '有效' },
  PASS_CODE_EMPTY: { type: 'warning', label: '需提取码' },
  PASS_CODE_ERROR: { type: 'error', label: '提取码错误' },
  NOT_FOUND: { type: 'error', label: '已失效' },
  DELETED: { type: 'error', label: '已删除' },
  EXPIRED: { type: 'error', label: '已过期' },
  SENSITIVE_RESOURCE: { type: 'error', label: '违规资源' },
  ERROR: { type: 'warning', label: '检测出错' },
}

export function checkStatus(st) {
  if (!st) return { type: 'default', label: '未检测' }
  return CHECK_STATUS[st] || { type: 'default', label: st }
}

/** 库里有没有这条链接的提取码（CODE 是 '无' 表示明确没码） */
export function hasCode(row) {
  const c = row && row.CODE
  return !!c && c !== '无'
}

/** 列表里显示的标签：结合「PikPak 要不要码」和「我们有没有码」 */
export function rowStatus(row) {
  if (!row || !row.STATUS) return { type: 'default', label: '未检测' }
  if (row.STATUS === 'PASS_CODE_EMPTY') {
    return hasCode(row)
      ? { type: 'success', label: '有提取码' }
      : { type: 'warning', label: '缺提取码' }
  }
  return checkStatus(row.STATUS)
}

export function meName(me) {
  if (!me) return ''
  const n = [me.first_name, me.last_name].filter(Boolean).join(' ')
  return n || me.username || (me.id ? String(me.id) : '')
}

/** 复制：优先用剪贴板 API，http 下回退到 textarea + execCommand */
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.cssText = 'position:fixed; top:-1000px; opacity:0'
  document.body.appendChild(ta)
  ta.select()
  try {
    if (!document.execCommand('copy')) throw new Error('execCommand 返回 false')
  } finally {
    document.body.removeChild(ta)
  }
}

export function copyPayload(it) {
  const code = it.CODE && it.CODE !== '无' ? `  提取码 ${it.CODE}` : ''
  return it.LINK + code
}

/**
 * 走势图的指标（图表和仪表盘的分段按钮共用一份，免得两边标签写岔）。
 * color 是柱子的颜色，和状态构成那边的语义色保持一致。
 */
export const DAILY_METRICS = [
  { key: 'new_links', label: '新增链接', color: '#12a594' },
  { key: 'messages', label: '消息量', color: '#5b8def' },
  { key: 'checked', label: '校验数', color: '#e0a11f' },
  { key: 'gone', label: '判失效', color: '#e5484d' },
]
