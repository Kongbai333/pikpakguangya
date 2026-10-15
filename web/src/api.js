/** 接口封装：统一错误处理，错误信息优先用后端返回的 detail。 */

async function request(path, { method = 'GET', body, query, timeout = 40000 } = {}) {
  let url = path
  if (query) {
    const usp = new URLSearchParams()
    Object.entries(query).forEach(([k, v]) => {
      if (v === undefined || v === null || v === '') return
      usp.append(k, v)
    })
    const qs = usp.toString()
    if (qs) url += (url.includes('?') ? '&' : '?') + qs
  }
  const init = { method, headers: {} }
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  // 超时兜底：某些接口（比如 /api/auth/status 要连 Telegram）可能一直不返回，
  // 页面就会卡在「正在读取…」。宁可报错也不要让用户盯着转圈。
  const ctrl = timeout ? new AbortController() : null
  if (ctrl) init.signal = ctrl.signal
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeout) : null
  let res
  try {
    res = await fetch(url, init)
  } catch (e) {
    if (ctrl && ctrl.signal.aborted) {
      throw new Error(`接口 ${path} 超过 ${Math.round(timeout / 1000)} 秒没响应（多半在等 Telegram / 网络）`)
    }
    throw new Error('连不上后台服务，确认服务在跑（start.ps1 / python -m app.api）')
  } finally {
    if (timer) clearTimeout(timer)
  }
  const text = await res.text()
  let data = {}
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = { detail: text.slice(0, 300) }
    }
  }
  if (!res.ok) {
    if (
      res.status === 401 &&
      path !== '/api/web-auth/session' &&
      path !== '/api/web-auth/login'
    ) {
      const current = `${window.location.hash.slice(1) || '/admin'}`
      window.location.hash = `#/login?redirect=${encodeURIComponent(current)}`
    }
    throw new Error(data.detail || `HTTP ${res.status}`)
  }
  return data
}

export const get = (path, query) => request(path, { query })
export const post = (path, body) => request(path, { method: 'POST', body: body ?? {} })
export const del = (path, body) => request(path, { method: 'DELETE', body: body ?? {} })
export const put = (path, body) => request(path, { method: 'PUT', body: body ?? {} })

/** 不带超时的 POST：批量检测这类本来就要等很久的用这个。 */
export const postLong = (path, body) => request(path, { method: 'POST', body: body ?? {}, timeout: 0 })

/**
 * 链接列表。接口拿不到时回退到同目录的 data.js（导出的静态快照），
 * 这样把 dist 拷到别处、没有后台时也能当只读列表看。
 */
export async function fetchItems(query) {
  try {
    const j = await get('/api/items', query)
    return { ...j, offline: false }
  } catch (e) {
    const fallback = await loadLegacyData()
    if (!fallback) throw e
    const items = fallback.items || []
    return {
      status: 200,
      total: items.length,
      page: 1,
      size: items.length,
      items,
      stats: null,
      offline: true,
    }
  }
}

// data.js 是 tools/export_json.py 导出的，挂的全局名是 __DATA__。
// 以前这里读的是 window.SHUJU，对不上 —— 兜底路径等于从来没生效过。
function readLegacy() {
  return window.__DATA__ || window.SHUJU || null
}

function loadLegacyData() {
  return new Promise((resolve) => {
    const existing = readLegacy()
    if (existing) return resolve(existing)
    const s = document.createElement('script')
    s.src = 'data.js'
    s.onload = () => resolve(readLegacy())
    s.onerror = () => resolve(null)
    document.head.appendChild(s)
  })
}
