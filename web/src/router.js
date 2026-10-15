import { createRouter, createWebHashHistory } from 'vue-router'
import { ensureWebAuth } from './auth'

/**
 * hash 路由：不管后台挂在哪个路径下都能用，也不用给 FastAPI 加 catch-all。
 *
 * `/` 是**前台**（浏览页，给访客看），`/admin/*` 是管理后台 —— 两套外壳
 * 在 App.vue 里分岔：前台是简单的顶栏，后台才带左侧导航。
 */
const routes = [
  { path: '/', name: 'browse', component: () => import('./views/Browse.vue') },
  { path: '/login', name: 'login', component: () => import('./views/Login.vue') },
  {
    path: '/admin',
    component: () => import('./views/AdminLayout.vue'),
    children: [
      { path: '', name: 'dashboard', component: () => import('./views/Dashboard.vue') },
      // 监控室和群组都已合并到账号页，保留旧地址直接跳过去
      { path: 'monitor', redirect: '/admin/account' },
      { path: 'chats', redirect: '/admin/account' },
      { path: 'links', name: 'links', component: () => import('./views/Links.vue') },
      { path: 'media', name: 'media', component: () => import('./views/Media.vue') },
      { path: 'transfers', name: 'transfers', component: () => import('./views/Transfers.vue') },
      { path: 'logs', name: 'logs', component: () => import('./views/Logs.vue') },
      { path: 'proxy', redirect: '/admin/settings' },
      { path: 'account', name: 'account', component: () => import('./views/Account.vue') },
      { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue') },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

// 直接打开 `/admin`（容器启动日志和 README 里提示的就是这个地址）时 hash 是空的，
// 路由会落到前台的浏览页 —— 而前台关掉之后那个页面根本打不开，人就被堵在门外。
// 补成 hash 路由认识的地址，`/admin` 就能直接进后台。
if (!window.location.hash && /\/admin\/?$/.test(window.location.pathname)) {
  window.history.replaceState(null, '', `${window.location.pathname}#/admin`)
}

const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const authenticated = await ensureWebAuth()
  if (to.name === 'login') {
    return authenticated ? { name: 'dashboard' } : true
  }
  if (to.path.startsWith('/admin') && !authenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
