/** 管理后台登录态。 */
import { reactive } from 'vue'

import * as api from './api'

export const webAuth = reactive({
  ready: false,
  authenticated: false,
  configured: true,
})

let pending = null

export async function ensureWebAuth(force = false) {
  if (pending && !force) return pending
  pending = (async () => {
    try {
      const result = await api.get('/api/web-auth/session')
      webAuth.authenticated = !!result.authenticated
      webAuth.configured = result.configured !== false
    } catch {
      webAuth.authenticated = false
      webAuth.configured = false
    } finally {
      webAuth.ready = true
    }
    return webAuth.authenticated
  })()
  try {
    return await pending
  } finally {
    pending = null
  }
}

export async function loginWeb(password) {
  const result = await api.post('/api/web-auth/login', { password })
  webAuth.authenticated = true
  webAuth.configured = true
  webAuth.ready = true
  return result
}

export async function logoutWeb() {
  try {
    await api.post('/api/web-auth/logout')
  } finally {
    webAuth.authenticated = false
    webAuth.ready = true
  }
}
