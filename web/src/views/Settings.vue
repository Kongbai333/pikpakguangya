<script setup>
/**
 * 设置页：应用凭据、Telegram Bot、网盘账号、代理池、PikPak 详情接口和环境信息。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import {
  Archive,
  ArrowLeft,
  Bot,
  Cloud,
  Download,
  FileText,
  Folder,
  FolderOpen,
  Globe,
  Info,
  KeyRound,
  Link2,
  LogIn,
  LogOut,
  Server,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Upload,
} from 'lucide-vue-next'

import * as api from '../api'
import ProxyPool from '../components/ProxyPool.vue'
import { fmtBytes } from '../format'

const message = useMessage()

const busy = ref('')
const info = ref({ credentials: {}, paths: {} })
const cred = reactive({ api_id: '', api_hash: '', phone: '', hashSet: false, source: '' })
const bot = reactive({
  token: '',
  user_id: '',
  tokenSet: false,
  running: false,
  error: '',
  lastSeenAt: '',
})
const pk = reactive({ enabled: true, proxy_ready: false, proxy_masked: '' })
const pub = reactive({ enabled: true, saving: false })
const integrationBusy = ref('')
const transferSettings = reactive({
  scan_workers: 20,
  transfer_workers: 15,
  cid_chunk_limit: 40,
  check_used_links: false,
})
const savingTransfer = ref(false)
const logs = reactive({
  dir: '',
  retention_days: 14,
  files: [],
  total_bytes: 0,
  loading: false,
  cleaning: false,
})
const backup = reactive({
  info: null,
  file: null,
  loading: false,
  downloading: false,
  restoring: false,
  error: '',
  result: null,
})
const backupInput = ref(null)
const pikpakForm = reactive({ username: '', password: '' })
const guangyaForm = reactive({ phone: '', code: '' })
const guangyaToken = ref('')
const integrations = reactive({
  pikpak: { connected: false, account: '', updated_at: '', user_id: '', storage: null },
  guangya: {
    connected: false,
    code_pending: false,
    phone: '',
    token_hint: '',
    updated_at: '',
    storage: null,
    root_folder_id: '',
    root_folder_path: '/',
  },
})
const folderModal = reactive({
  open: false,
  saving: false,
  loading: false,
  folders: [],
  stack: [{ id: '', name: '根目录' }],
})

const currentFolderPath = computed(() =>
  '/' + folderModal.stack.slice(1).map((item) => item.name).join('/')
)

async function loadPk() {
  try {
    Object.assign(pk, await api.get('/api/pikpak-api'))
  } catch (e) {
    message.error(e.message)
  }
}

async function savePk(v) {
  try {
    await api.put('/api/pikpak-api', { enabled: !!v })
    message.success(v ? '已启用详情接口' : '已关闭详情接口（校验退回抓分享页）')
  } catch (e) {
    message.error(e.message)
  }
}

async function loadPub() {
  try {
    Object.assign(pub, await api.get('/api/web-public'))
  } catch (e) {
    message.error(e.message)
  }
}

async function savePub(v) {
  pub.saving = true
  try {
    await api.put('/api/web-public', { enabled: !!v })
    pub.enabled = !!v
    message.success(v ? '前台浏览页已打开' : '前台浏览页已关闭，外部访问会看到关闭提示')
  } catch (e) {
    message.error(e.message)
  } finally {
    pub.saving = false
  }
}

async function load() {
  try {
    const j = await api.get('/api/settings')
    info.value = j
    cred.api_id = j.credentials?.api_id || ''
    cred.phone = j.credentials?.phone || ''
    cred.hashSet = !!j.credentials?.api_hash_set
    cred.source = j.credentials?.source || ''
    bot.tokenSet = !!j.bot?.token_set
    bot.user_id = j.bot?.user_id || ''
    bot.running = !!j.bot?.running
    bot.error = j.bot?.error || ''
    bot.lastSeenAt = j.bot?.last_seen_at || ''
  } catch (e) {
    message.error(e.message)
  }
}

async function loadIntegrations() {
  try {
    // 先秒回本地登录态；容量走后端缓存，缓存在后台刷新，接口不等网盘
    const quick = await api.get('/api/integrations/status', { storage: false })
    mergeIntegrations(quick)
    const data = await api.get('/api/integrations/status')
    mergeIntegrations(data)
    scheduleStorageRetry(data)
  } catch (e) {
    message.error(e.message)
  }
}

function mergeIntegrations(data) {
  Object.assign(integrations.pikpak, data.pikpak || {})
  Object.assign(integrations.guangya, data.guangya || {})
}

async function loadTransferSettings() {
  try {
    const data = await api.get('/api/transfers/settings')
    Object.assign(transferSettings, data.settings || {})
  } catch {
    // 拿不到就用默认值，不挡设置页
  }
}

async function saveTransferSettings() {
  savingTransfer.value = true
  try {
    const data = await api.put('/api/transfers/settings', {
      scan_workers: transferSettings.scan_workers || 15,
      transfer_workers: transferSettings.transfer_workers || 10,
      cid_chunk_limit: transferSettings.cid_chunk_limit || 30,
      check_used_links: !!transferSettings.check_used_links,
    })
    Object.assign(transferSettings, data.settings || {})
    message.success('并发设置已保存，对之后新建的任务生效')
  } catch (e) {
    message.error(e.message)
  } finally {
    savingTransfer.value = false
  }
}

async function loadLogs() {
  logs.loading = true
  try {
    // 注意是 /files：/api/logs 是日志**内容**（日志页用的），这里要的是文件清单
    Object.assign(logs, await api.get('/api/logs/files'))
  } catch (e) {
    message.error(e.message)
  } finally {
    logs.loading = false
  }
}

async function cleanupLogs(clearCurrent = false) {
  logs.cleaning = true
  try {
    const data = await api.post('/api/logs/cleanup', {
      keep_days: clearCurrent ? 0 : logs.retention_days,
      clear_current: clearCurrent,
    })
    Object.assign(logs, data)
    if (clearCurrent) {
      const removed = Number(data.deleted?.length || 0)
      message.success(
        data.cleared
          ? `日志已清空${removed ? `（删除 ${removed} 个文件）` : ''}`
          : '当前日志被占用未清空，历史日志已删除',
      )
    } else {
      message.success(`已清理 ${data.deleted?.length || 0} 个过期日志文件`)
    }
    await loadLogs()
  } catch (e) {
    message.error(e.message)
  } finally {
    logs.cleaning = false
  }
}

let storageTimer = null
let storageTries = 0

async function loadBackup() {
  backup.loading = true
  try {
    backup.info = await api.get('/api/backup')
  } catch (e) {
    backup.error = e.message
  } finally {
    backup.loading = false
  }
}

/**
 * 走 fetch 而不是 <a href>：需要从 Content-Disposition 里拿服务端生成的文件名，
 * 而且接口失败时要能弹错误 —— 直接跳转会变成下载一个 JSON 报错页。
 */
async function downloadBackup() {
  if (backup.downloading) return
  backup.downloading = true
  backup.error = ''
  try {
    const res = await fetch('/api/backup/download')
    if (!res.ok) {
      let detail = `HTTP ${res.status}`
      try {
        detail = (await res.json()).detail || detail
      } catch {
        /* 不是 JSON 就用状态码 */
      }
      throw new Error(detail)
    }
    const blob = await res.blob()
    const matched = /filename="?([^";]+)"?/i.exec(
      res.headers.get('content-disposition') || ''
    )
    const name = matched ? matched[1] : 'pikpak-backup.tar.gz'
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = name
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    message.success(`已生成备份 ${name}`)
  } catch (e) {
    backup.error = e.message
    message.error(`生成备份失败：${e.message}`)
  } finally {
    backup.downloading = false
  }
}

function pickBackupFile() {
  backupInput.value?.click()
}

function onBackupPicked(event) {
  backup.file = event.target.files?.[0] || null
  backup.result = null
  backup.error = ''
  // 清掉 value，同一个文件再选一次也能触发 change
  event.target.value = ''
}

async function restoreBackup() {
  if (!backup.file || backup.restoring) return
  backup.restoring = true
  backup.error = ''
  try {
    // 直接把 File 当请求体发，后端收原始流；不用 multipart 就不用多装依赖
    const res = await fetch(
      `/api/backup/restore?name=${encodeURIComponent(backup.file.name)}`,
      { method: 'POST', body: backup.file }
    )
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`)
    backup.result = data
    backup.file = null
    message.success('还原完成')
    await loadBackup()
  } catch (e) {
    backup.error = e.message
    message.error(`还原失败：${e.message}`)
  } finally {
    backup.restoring = false
  }
}

/** 缓存第一次是空的：等后台刷新完再补一两次，避免页面转圈等网盘。 */
function scheduleStorageRetry(data) {
  const waiting = !!data.pikpak?.storage_refreshing || !!data.guangya?.storage_refreshing
  const missing = (data.pikpak?.connected && !data.pikpak?.storage)
    || (data.guangya?.connected && !data.guangya?.storage)
  if ((!waiting && !missing) || storageTries >= 4) return
  storageTries += 1
  clearTimeout(storageTimer)
  storageTimer = setTimeout(async () => {
    storageTimer = null
    try {
      const next = await api.get('/api/integrations/status')
      mergeIntegrations(next)
      scheduleStorageRetry(next)
    } catch {
      // 容量查不出来不打扰用户，下次进页面自然会再试
    }
  }, 2500)
}

async function saveCred() {
  busy.value = 'cred'
  try {
    const j = await api.post('/api/auth/credentials', {
      api_id: Number(cred.api_id) || 0,
      api_hash: cred.api_hash,
      phone: cred.phone,
    })
    cred.hashSet = !!j.api_hash_set
    cred.api_hash = ''
    message.success('凭据已保存')
    await load()
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = ''
  }
}

async function saveBot() {
  busy.value = 'bot'
  try {
    const j = await api.put('/api/settings/telegram-bot', {
      bot_token: bot.token,
      user_id: Number(bot.user_id) || 0,
    })
    bot.token = ''
    bot.tokenSet = !!j.bot?.token_set
    bot.user_id = j.bot?.user_id || ''
    bot.running = !!j.bot?.running
    bot.error = j.bot?.error || ''
    bot.lastSeenAt = j.bot?.last_seen_at || ''
    message.success('Telegram Bot 配置已保存')
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = ''
  }
}

async function loginPikPak() {
  integrationBusy.value = 'pikpak-login'
  try {
    const data = await api.post('/api/integrations/pikpak/login', {
      username: pikpakForm.username,
      password: pikpakForm.password,
    })
    Object.assign(integrations.pikpak, data.pikpak || {})
    pikpakForm.password = ''
    message.success('PikPak 登录成功')
  } catch (e) {
    message.error(e.message)
  } finally {
    integrationBusy.value = ''
  }
}

async function logoutPikPak() {
  integrationBusy.value = 'pikpak-logout'
  try {
    const data = await api.post('/api/integrations/pikpak/logout')
    Object.assign(integrations.pikpak, data.pikpak || {})
    message.success('PikPak 登录态已清除')
  } catch (e) {
    message.error(e.message)
  } finally {
    integrationBusy.value = ''
  }
}

async function sendGuangyaCode() {
  integrationBusy.value = 'guangya-code'
  try {
    const data = await api.post('/api/integrations/guangya/send-code', {
      phone: guangyaForm.phone,
    })
    Object.assign(integrations.guangya, data.guangya || {})
    message.success('短信验证码已发送')
  } catch (e) {
    message.error(e.message)
  } finally {
    integrationBusy.value = ''
  }
}

async function loginGuangya() {
  integrationBusy.value = 'guangya-login'
  try {
    const data = await api.post('/api/integrations/guangya/login', {
      code: guangyaForm.code,
    })
    Object.assign(integrations.guangya, data.guangya || {})
    guangyaForm.code = ''
    message.success('光鸭登录成功')
  } catch (e) {
    message.error(e.message)
  } finally {
    integrationBusy.value = ''
  }
}

async function connectGuangyaToken() {
  integrationBusy.value = 'guangya-token'
  try {
    const data = await api.post('/api/integrations/guangya/token', {
      token: guangyaToken.value,
    })
    Object.assign(integrations.guangya, data.guangya || {})
    guangyaToken.value = ''
    message.success('光鸭 Token 已接入')
  } catch (e) {
    message.error(e.message)
  } finally {
    integrationBusy.value = ''
  }
}

async function logoutGuangya() {
  integrationBusy.value = 'guangya-logout'
  try {
    const data = await api.post('/api/integrations/guangya/logout')
    Object.assign(integrations.guangya, data.guangya || {})
    message.success('光鸭连接已清除')
  } catch (e) {
    message.error(e.message)
  } finally {
    integrationBusy.value = ''
  }
}

async function loadFolderRows() {
  const current = folderModal.stack[folderModal.stack.length - 1]
  folderModal.loading = true
  try {
    const data = await api.get('/api/integrations/guangya/folders', {
      parent_id: current?.id || '',
    })
    folderModal.folders = data.folders || []
  } catch (e) {
    message.error(e.message)
    folderModal.folders = []
  } finally {
    folderModal.loading = false
  }
}

async function openFolderPicker() {
  folderModal.stack = [{ id: '', name: '根目录' }]
  folderModal.open = true
  await loadFolderRows()
}

async function enterFolder(folder) {
  folderModal.stack.push({ id: folder.id, name: folder.name })
  await loadFolderRows()
}

async function backFolder() {
  if (folderModal.stack.length <= 1) return
  folderModal.stack.pop()
  await loadFolderRows()
}

async function saveFolder() {
  const current = folderModal.stack[folderModal.stack.length - 1]
  folderModal.saving = true
  try {
    const data = await api.post('/api/integrations/guangya/folder', {
      folder_id: current?.id || '',
      folder_path: currentFolderPath.value,
    })
    Object.assign(integrations.guangya, data.guangya || {})
    folderModal.open = false
    message.success(`秒传目录已设为 ${currentFolderPath.value}`)
  } catch (e) {
    message.error(e.message)
  } finally {
    folderModal.saving = false
  }
}

onMounted(async () => {
  await Promise.all([
    load(), loadPk(), loadPub(), loadIntegrations(), loadTransferSettings(), loadLogs(),
    loadBackup(),
  ])
})
/* ---------- 分区导航 ----------
   这一页有九个板块、近千行。之前是顶部一排横向胶囊吸顶 —— 横向空间不够时
   要左右滑，而且和顶栏贴在一起很容易被盖住。改成左侧竖向导航：
   和整个应用的外壳语言一致，也不需要吸顶偏移那套计算。 */
const SECTIONS = [
  { id: 'set-cred', label: '应用凭据', icon: KeyRound },
  { id: 'set-bot', label: 'Telegram Bot', icon: Bot },
  { id: 'set-pikpak', label: 'PikPak 账号', icon: Cloud },
  { id: 'set-guangya', label: '光鸭账号', icon: Link2 },
  { id: 'set-transfer', label: '秒传参数', icon: SlidersHorizontal },
  { id: 'set-proxy', label: '代理池', icon: Server },
  { id: 'set-share-api', label: '分享详情接口', icon: Sparkles },
  { id: 'set-public', label: '前台浏览页', icon: Globe },
  { id: 'set-backup', label: '备份与还原', icon: Archive },
  { id: 'set-env', label: '环境信息', icon: Info },
  { id: 'set-logs', label: '日志', icon: FileText },
]
const activeSection = ref(SECTIONS[0].id)
let sectionObserver = null

function gotoSection(id) {
  const el = document.getElementById(id)
  if (!el) return
  // 顶栏高度 + 一点呼吸空间
  const top = el.getBoundingClientRect().top + window.scrollY - 84
  window.scrollTo({ top, behavior: 'smooth' })
  activeSection.value = id
}

onMounted(() => {
  if (typeof IntersectionObserver === 'undefined') return
  sectionObserver = new IntersectionObserver(
    (entries) => {
      // 取当前可见的最靠上那个，避免快速滚动时来回跳
      const visible = entries
        .filter((e) => e.isIntersecting)
        .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
      if (visible[0]?.target?.id) activeSection.value = visible[0].target.id
    },
    { rootMargin: '-88px 0px -62% 0px', threshold: 0 }
  )
  for (const section of SECTIONS) {
    const el = document.getElementById(section.id)
    if (el) sectionObserver.observe(el)
  }
})

onUnmounted(() => {
  clearTimeout(storageTimer)
  sectionObserver?.disconnect()
  sectionObserver = null
})
</script>

<template>
  <div class="settings">
    <!-- 左：分区导航。跟着页面滚，滚到哪个区自动高亮。 -->
    <nav class="sidenav" aria-label="设置分区">
      <div class="sidenav__head">设置</div>
      <button
        v-for="section in SECTIONS"
        :key="section.id"
        type="button"
        class="sidenav__item"
        :class="{ on: activeSection === section.id }"
        @click="gotoSection(section.id)"
      >
        <component :is="section.icon" :size="15" :stroke-width="1.9" />
        <span class="sidenav__label">{{ section.label }}</span>
      </button>
    </nav>

    <!-- 右：各分区。
         下面这些卡片保持在原缩进（没有为多出来的这层容器再缩一级）——
         内容有六百行，为纯缩进整体重排不划算。 -->
    <div class="settings__body">

    <!-- 应用凭据 -->
    <n-card id="set-cred" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <KeyRound :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">应用凭据</span>
          <n-tag class="ml-2" size="small" round>
            {{ cred.hashSet ? '已设置' : '未设置' }}
            <template v-if="cred.source">
              · 来自{{ cred.source === 'db' ? '这里' : 'config.toml' }}
            </template>
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        登录 Telegram 用的 api_id / api_hash，<b>只存在本机数据库里</b>
        （写进 <code class="mono">config.toml</code> 也行，这里填的优先）。
        没有就去
        <a
          class="text-[var(--app-brand)]"
          href="https://my.telegram.org"
          target="_blank"
          rel="noreferrer"
        >my.telegram.org</a>
        的 API development tools 申请，免费。
      </p>

      <div class="flex flex-wrap items-end gap-3">
        <div>
          <div class="text-[13px] muted mb-1">api_id</div>
          <n-input v-model:value="cred.api_id" class="w-[150px]" placeholder="1234567" />
        </div>
        <div>
          <div class="text-[13px] muted mb-1">
            api_hash {{ cred.hashSet ? '（已设置，留空则不修改）' : '' }}
          </div>
          <n-input v-model:value="cred.api_hash" class="w-[320px]" placeholder="32 位十六进制" />
        </div>
        <div>
          <div class="text-[13px] muted mb-1">默认手机号</div>
          <n-input v-model:value="cred.phone" class="w-[190px]" placeholder="+8613800000000" />
        </div>
        <n-button type="primary" :loading="busy === 'cred'" @click="saveCred">保存</n-button>
      </div>

      <n-alert v-if="!cred.hashSet" class="mt-3" type="warning" :show-icon="true">
        还没配 api_id / api_hash，账号页的「添加账号」用不了
      </n-alert>
    </n-card>

    <!-- Telegram Bot -->
    <n-card id="set-bot" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <Bot :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">Telegram Bot</span>
          <n-tag
            class="ml-2"
            size="small"
            round
            :type="bot.running ? 'success' : (bot.tokenSet ? 'warning' : 'default')"
          >
            {{ bot.running ? '运行中' : (bot.tokenSet ? '未运行' : '未配置') }}
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        在
        <a
          class="text-[var(--app-brand)]"
          href="https://t.me/BotFather"
          target="_blank"
          rel="noreferrer"
        >@BotFather</a>
        创建机器人并复制 Bot Token。这里只接收和处理下方 TG 用户 ID 发来的私聊消息，
        其他用户的消息会直接忽略。
      </p>

      <div class="flex flex-wrap items-end gap-3">
        <div>
          <div class="text-[13px] muted mb-1">
            Bot Token {{ bot.tokenSet ? '（已设置，留空则不修改）' : '' }}
          </div>
          <n-input
            v-model:value="bot.token"
            class="w-[380px] max-w-full"
            type="password"
            show-password-on="click"
            placeholder="123456789:AA..."
          />
        </div>
        <div>
          <div class="text-[13px] muted mb-1">TG 用户 ID</div>
          <n-input
            v-model:value="bot.user_id"
            class="w-[190px]"
            inputmode="numeric"
            placeholder="例如 123456789"
          />
        </div>
        <n-button type="primary" :loading="busy === 'bot'" @click="saveBot">保存</n-button>
      </div>

      <div class="text-[12.5px] muted mt-3">
        命令：
        <code class="mono">/tasks 秒传任务</code>
        <code class="mono">/cleanup 链接清理</code>
        <code class="mono">/system 系统情况</code>
        <code class="mono">/links 链接情况</code>
        <code class="mono">/accounts 账号管理</code>
        <code class="mono">/search 关键词 链接搜索</code>
        · 发送 PikPak 链接可直接创建秒传任务
      </div>
      <div class="text-[12.5px] muted mt-1">
        解析完成消息会永久保留；进度按每 10 个文件或最多 5 秒合并更新一次
      </div>
      <div v-if="bot.lastSeenAt" class="text-[12.5px] muted mt-1">
        最近收到私聊：{{ bot.lastSeenAt }}
      </div>
      <n-alert v-if="bot.error" class="mt-3" type="warning" :show-icon="true">
        {{ bot.error }}
      </n-alert>
    </n-card>

    <!-- PikPak 账号 -->
    <n-card id="set-pikpak" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <Cloud :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">PikPak 账号</span>
          <n-tag
            class="ml-2"
            size="small"
            round
            :type="integrations.pikpak.connected ? 'success' : 'default'"
          >
            {{ integrations.pikpak.connected ? '已登录' : '未登录' }}
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        用于读取分享目录和文件 GCID。账号密码只用于本次登录，不保存；登录态和 refresh token 存在本机数据库。
      </p>

      <div class="flex flex-wrap items-end gap-3">
        <div>
          <div class="text-[13px] muted mb-1">邮箱、手机号或用户名</div>
          <n-input
            v-model:value="pikpakForm.username"
            class="w-[280px]"
            placeholder="邮箱 / 手机号 / 用户名"
          />
        </div>
        <div>
          <div class="text-[13px] muted mb-1">密码</div>
          <n-input
            v-model:value="pikpakForm.password"
            class="w-[260px]"
            type="password"
            show-password-on="click"
            @keyup.enter="loginPikPak"
          />
        </div>
        <n-button
          type="primary"
          :loading="integrationBusy === 'pikpak-login'"
          @click="loginPikPak"
        >
          <template #icon><LogIn :size="15" /></template>
          登录
        </n-button>
        <n-button
          v-if="integrations.pikpak.connected"
          secondary
          :loading="integrationBusy === 'pikpak-logout'"
          @click="logoutPikPak"
        >
          <template #icon><LogOut :size="15" /></template>
          清除登录
        </n-button>
      </div>

      <div v-if="integrations.pikpak.connected" class="text-[12.5px] muted mt-3">
        账号 {{ integrations.pikpak.account || '已连接' }}
        <span v-if="integrations.pikpak.updated_at">
          · 登录于 {{ integrations.pikpak.updated_at }}
        </span>
      </div>
      <div
        v-if="integrations.pikpak.storage"
        class="mt-3 rounded-xl bg-[var(--app-panel-2)] p-3 text-[12.5px] muted"
      >
        <div class="flex items-center justify-between">
          <span>已用 {{ fmtBytes(integrations.pikpak.storage.used) }}</span>
          <span>总容量 {{ fmtBytes(integrations.pikpak.storage.total) }}</span>
        </div>
        <n-progress
          class="mt-2"
          type="line"
          :percentage="integrations.pikpak.storage.percent || 0"
          :show-indicator="false"
          :height="6"
        />
        <div v-if="integrations.pikpak.storage_error" class="text-[11.5px] mt-1 text-rose-500">
          {{ integrations.pikpak.storage_error }}
        </div>
      </div>
      <div
        v-if="!integrations.pikpak.storage && integrations.pikpak.storage_error"
        class="mt-2 text-[11.5px] text-rose-500"
      >
        容量读取失败：{{ integrations.pikpak.storage_error }}
      </div>
    </n-card>

    <!-- 光鸭账号 -->
    <n-card id="set-guangya" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <Link2 :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">光鸭账号</span>
          <n-tag
            class="ml-2"
            size="small"
            round
            :type="integrations.guangya.connected ? 'success' : (integrations.guangya.code_pending ? 'warning' : 'default')"
          >
            {{ integrations.guangya.connected ? '已连接' : (integrations.guangya.code_pending ? '待验证码' : '未连接') }}
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        使用光鸭网页端同一套手机号和短信验证码登录，登录后保存 access token / refresh token。
      </p>

      <div class="flex flex-wrap items-end gap-3">
        <div>
          <div class="text-[13px] muted mb-1">手机号</div>
          <n-input
            v-model:value="guangyaForm.phone"
            class="w-[220px]"
            placeholder="+8613800000000"
          />
        </div>
        <n-button
          secondary
          :loading="integrationBusy === 'guangya-code'"
          :disabled="!guangyaForm.phone"
          @click="sendGuangyaCode"
        >
          发送验证码
        </n-button>
        <div>
          <div class="text-[13px] muted mb-1">短信验证码</div>
          <n-input
            v-model:value="guangyaForm.code"
            class="w-[170px]"
            placeholder="6 位验证码"
            @keyup.enter="loginGuangya"
          />
        </div>
        <n-button
          type="primary"
          :loading="integrationBusy === 'guangya-login'"
          :disabled="!guangyaForm.code"
          @click="loginGuangya"
        >
          <template #icon><LogIn :size="15" /></template>
          登录
        </n-button>
        <n-button
          v-if="integrations.guangya.connected"
          secondary
          :loading="integrationBusy === 'guangya-logout'"
          @click="logoutGuangya"
        >
          <template #icon><LogOut :size="15" /></template>
          清除登录
        </n-button>
      </div>

      <div v-if="integrations.guangya.code_pending" class="text-[12.5px] muted mt-3">
        验证码已发送，手机号 {{ integrations.guangya.phone }}
      </div>
      <div v-else-if="integrations.guangya.connected" class="text-[12.5px] muted mt-3">
        已登录 {{ integrations.guangya.phone || integrations.guangya.token_hint }}
        <span v-if="integrations.guangya.updated_at">
          · {{ integrations.guangya.updated_at }}
        </span>
      </div>
      <div v-if="integrations.guangya.storage" class="mt-3 rounded-xl bg-[var(--app-panel-2)] p-3 text-[12.5px] muted">
        <div class="flex items-center justify-between">
          <span>已用 {{ fmtBytes(integrations.guangya.storage.used) }}</span>
          <span>总容量 {{ fmtBytes(integrations.guangya.storage.total) }}</span>
        </div>
        <n-progress
          class="mt-2"
          type="line"
          :percentage="integrations.guangya.storage.percent || 0"
          :show-indicator="false"
          :height="6"
        />
        <div v-if="integrations.guangya.storage_error" class="text-[11.5px] mt-1 text-rose-500">
          {{ integrations.guangya.storage_error }}
        </div>
      </div>
      <div
        v-if="!integrations.guangya.storage && integrations.guangya.storage_error"
        class="mt-2 text-[11.5px] text-rose-500"
      >
        容量读取失败：{{ integrations.guangya.storage_error }}
      </div>
      <div v-if="integrations.guangya.connected" class="flex items-center gap-3 mt-3">
        <n-button size="small" secondary @click="openFolderPicker">
          <template #icon><FolderOpen :size="15" /></template>
          选择秒传目录
        </n-button>
        <span class="text-[12.5px] muted">
          当前：{{ integrations.guangya.root_folder_path || '/' }}
        </span>
      </div>

      <n-collapse class="mt-3">
        <n-collapse-item title="Token 接入（备用）" name="token">
          <n-input
            v-model:value="guangyaToken"
            type="textarea"
            :rows="3"
            placeholder='粘贴 access_token，或 {"access_token":"..."}'
          />
          <n-button
            class="mt-3"
            secondary
            :loading="integrationBusy === 'guangya-token'"
            :disabled="!guangyaToken"
            @click="connectGuangyaToken"
          >
            验证并接入 Token
          </n-button>
        </n-collapse-item>
      </n-collapse>
    </n-card>

    <!-- 秒传设置 -->
    <n-card id="set-transfer" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <SlidersHorizontal :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">秒传设置</span>
          <span class="ml-2 text-[12.5px] muted">实测：采样可到 60，光鸭 10~15 最优</span>
        </div>
      </template>

      <div class="flex flex-wrap items-end gap-4">
        <div>
          <div class="text-[13px] muted mb-1">目录扫描并发</div>
          <n-input-number
            v-model:value="transferSettings.scan_workers"
            class="w-[140px]"
            :min="1"
            :max="64"
          />
        </div>
        <div>
          <div class="text-[13px] muted mb-1">转存并发</div>
          <n-input-number
            v-model:value="transferSettings.transfer_workers"
            class="w-[140px]"
            :min="1"
            :max="64"
          />
        </div>
        <div>
          <div class="text-[13px] muted mb-1">CID 分片全局上限</div>
          <n-input-number
            v-model:value="transferSettings.cid_chunk_limit"
            class="w-[170px]"
            :min="1"
            :max="64"
          />
        </div>
        <label class="flex items-center gap-2 pb-2 text-[13px]">
          <n-switch v-model:value="transferSettings.check_used_links" />
          已使用链接校验
        </label>
        <n-button type="primary" :loading="savingTransfer" @click="saveTransferSettings">
          保存
        </n-button>
      </div>

      <p class="text-[12.5px] muted mt-2 mb-0">
        目录扫描并发用于解析 PikPak 分享目录；转存并发是同时向光鸭发起秒传的文件数；
        CID 是采样下载并发（每个文件只下 3×20KB）。实测本机：光鸭接口 10~15 并发吞吐
        最好，再高只会排队变慢；PikPak 采样能扛 60 并发。保存后对新建任务生效。
        开启「已使用链接校验」后，已经创建过秒传任务的分享会被直接拦截。
        单个分享超过 20000 个文件时会自动按目录拆成多个排队任务。
      </p>
    </n-card>

    <div id="set-proxy" class="scroll-anchor">
      <ProxyPool />
    </div>

    <!-- PikPak 详情接口 -->
    <n-card id="set-share-api" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <Sparkles :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">PikPak 分享详情接口</span>
          <n-tag class="ml-2" size="small" round :type="pk.enabled ? 'success' : 'default'">
            {{ pk.enabled ? (pk.proxy_ready ? '已启用' : '已启用 · 没有系统代理') : '已关闭' }}
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        抓分享页只能看出「要不要提取码」，拿不到文件名。开着这个接口就能直接问 PikPak 要详情：
        <b>拿到真正的文件名</b>，还能判出<b>提取码写错了</b>。
        它按 IP 地区限制，<b>必须走代理</b> —— 用的是上面「系统代理」里选的那条
        （不是某个 TG 账号绑的代理）。
        默认开着；关掉就退回抓分享页，只是少了文件名。
      </p>

      <div class="flex flex-wrap items-center gap-4">
        <label class="flex items-center gap-2 text-[14px]">
          <n-switch :value="pk.enabled" @update:value="savePk" />
          启用详情接口
        </label>
        <span class="text-[13.5px] muted">
          当前出口：{{ pk.proxy_masked || '直连' }}
          <template v-if="!pk.proxy_ready"> · 详情接口不可用</template>
        </span>
        <a
          href="#set-proxy"
          class="text-[13.5px] text-[var(--app-brand)]"
          @click.prevent="gotoSection('set-proxy')"
        >
          去设置系统代理
        </a>
      </div>
    </n-card>

    <!-- 前台浏览页总开关 -->
    <n-card id="set-public" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <Globe :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">前台浏览页</span>
          <n-tag class="ml-2" size="small" round :type="pub.enabled ? 'success' : 'default'">
            {{ pub.enabled ? '对外可见' : '已关闭' }}
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        根地址 <code class="mono">/</code> 那个给访客看的只读浏览页。关掉之后，
        <b>页面和公开接口一起拒绝</b>（只藏页面不拦接口，数据照样能直接拉走），
        <code class="mono">data.js</code> 这类导出的静态快照也一并停掉。
        管理后台 <code class="mono">/admin#/admin</code> 不受影响，只有你自己登录能看
        —— 关掉之后根地址只剩一个提示页，上面就有进后台的按钮，别找不到路。
      </p>

      <div class="flex flex-wrap items-center gap-4">
        <label class="flex items-center gap-2 text-[14px]">
          <n-switch
            :value="pub.enabled"
            :loading="pub.saving"
            @update:value="savePub"
          />
          开放前台浏览
        </label>
        <a
          v-if="pub.enabled"
          href="/"
          target="_blank"
          rel="noopener noreferrer"
          class="text-[13.5px] text-[var(--app-brand)]"
        >
          打开看看 ↗
        </a>
        <span v-else class="text-[13.5px] muted">外部访问会看到「浏览页已关闭」</span>
      </div>
    </n-card>

    <!-- 备份与还原 -->
    <n-card id="set-backup" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <Archive :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">备份与还原</span>
          <n-tag v-if="backup.info" class="ml-2" size="small" round>
            {{ backup.info.items.length }} 项 · {{ fmtBytes(backup.info.total_bytes) }}
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        换机器部署时不用重新配一遍：归档里装着配置、数据库、Telegram 登录态、缩略图和导出的静态快照，
        <b>只有日志不装</b>。在新服务器上还原这个归档，等于把整套配置和数据搬过去。
      </p>

      <div v-if="backup.info" class="text-[12.5px] muted mb-3">
        数据目录：<span class="mono">{{ backup.info.data_dir }}</span>
      </div>

      <div
        v-if="backup.info?.items?.length"
        class="mb-3 max-h-[190px] overflow-auto rounded-lg border border-[var(--app-line)]"
      >
        <div
          v-for="item in backup.info.items"
          :key="item.name"
          class="flex items-center gap-3 px-3 py-2 text-[12.5px] border-b border-[var(--app-line)] last:border-b-0"
        >
          <span class="min-w-0 flex-1 truncate mono">{{ item.name }}</span>
          <span class="shrink-0 muted">
            {{ item.kind === 'dir' ? `${item.files} 个文件` : '' }}
          </span>
          <span class="shrink-0">{{ fmtBytes(item.bytes) }}</span>
        </div>
      </div>
      <n-alert v-else-if="backup.info" class="mb-3" type="warning" :show-icon="true">
        没有可备份的数据（配置、数据库、登录态都不存在）
      </n-alert>

      <div class="flex flex-wrap items-center gap-3">
        <n-button
          type="primary"
          size="small"
          :loading="backup.downloading"
          @click="downloadBackup"
        >
          <template #icon><Download :size="14" /></template>
          下载备份
        </n-button>

        <input
          ref="backupInput"
          type="file"
          accept=".gz,.tgz,.tar.gz,application/gzip"
          class="hidden"
          @change="onBackupPicked"
        />
        <n-button size="small" secondary @click="pickBackupFile">
          <template #icon><Upload :size="14" /></template>
          选择归档
        </n-button>

        <n-popconfirm v-if="backup.file" @positive-click="restoreBackup">
          <template #trigger>
            <n-button size="small" type="warning" :loading="backup.restoring">
              还原
            </n-button>
          </template>
          用「{{ backup.file.name }}」覆盖当前配置和数据？日志不动。还原完要重启服务。
        </n-popconfirm>
        <span v-if="backup.file" class="max-w-[280px] truncate text-[12.5px] muted">
          {{ backup.file.name }}
        </span>
      </div>

      <n-alert v-if="backup.error" class="mt-3" type="error" :show-icon="true">
        {{ backup.error }}
      </n-alert>

      <n-alert v-if="backup.result" class="mt-3" type="success" :show-icon="true">
        已还原 {{ backup.result.files }} 个文件（{{ backup.result.applied.join('、') }}）。
        配置和登录态要<b>重启服务 / 容器</b>之后才生效。
      </n-alert>

      <p class="text-[12.5px] muted mt-3 mb-0">
        命令行也行：<code class="mono">docker exec pikpak-collector python -m tools.backup</code>
        会在数据目录的 <code class="mono">backups/</code> 里生成归档；
        还原用 <code class="mono">python -m tools.restore &lt;归档路径&gt;</code>。
      </p>
    </n-card>

    <!-- 环境信息 -->
    <n-card id="set-env" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <Info :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">环境信息</span>
        </div>
      </template>
      <n-descriptions :column="1" bordered size="small">
        <n-descriptions-item label="版本">{{ info.version || '—' }}</n-descriptions-item>
        <n-descriptions-item label="数据库">
          <span class="mono">{{ info.paths?.db || '—' }}</span>
        </n-descriptions-item>
        <n-descriptions-item label="登录态文件">
          <span class="mono">{{ info.paths?.session || '—' }}</span>
        </n-descriptions-item>
        <n-descriptions-item label="项目目录">
          <span class="mono">{{ info.paths?.root || '—' }}</span>
        </n-descriptions-item>
        <n-descriptions-item label="当前代理">
          {{ info.proxy_masked || '—' }}
        </n-descriptions-item>
      </n-descriptions>
      <p class="text-[13.5px] muted mt-3 mb-0">
        改这些路径要去 <code class="mono">config.toml</code>，改完重启服务。
      </p>
    </n-card>

    <!-- 日志 -->
    <n-card id="set-logs" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <FileText :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">日志</span>
          <n-tag class="ml-2" size="small" round>
            {{ logs.files?.length || 0 }} 个文件 · {{ fmtBytes(logs.total_bytes || 0) }}
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        每天按日期切割，自动保留最近 {{ logs.retention_days }} 天。日志只保留任务级信息、
        异常和关键操作，逐请求和逐条链接日志不会写入；后台 stdout/stderr 文件被占用时会跳过，
        重启后可再清一次。
      </p>
      <div class="text-[12.5px] muted mb-3">
        目录：<span class="mono">{{ logs.dir || '—' }}</span>
      </div>

      <div class="flex flex-wrap items-center gap-3">
        <n-button size="small" secondary :loading="logs.loading" @click="loadLogs">
          <template #icon><FileText :size="14" /></template>
          刷新
        </n-button>
        <n-button size="small" secondary :loading="logs.cleaning" @click="cleanupLogs(false)">
          <template #icon><Trash2 :size="14" /></template>
          清理过期日志
        </n-button>
        <n-popconfirm @positive-click="cleanupLogs(true)">
          <template #trigger>
            <n-button size="small" secondary type="error" :loading="logs.cleaning">
              <template #icon><Trash2 :size="14" /></template>
              清空全部日志
            </n-button>
          </template>
          清空当前和全部历史日志？不会影响任务记录和数据库。
        </n-popconfirm>
      </div>

      <div
        v-if="logs.files?.length"
        class="mt-3 max-h-[180px] overflow-auto rounded-lg border border-[var(--app-line)]"
      >
        <div
          v-for="file in logs.files"
          :key="file.name"
          class="flex items-center gap-3 px-3 py-2 text-[12.5px] border-b border-[var(--app-line)] last:border-b-0"
        >
          <span class="min-w-0 flex-1 truncate mono">{{ file.name }}</span>
          <span class="shrink-0 muted">{{ file.modified_at }}</span>
          <span class="shrink-0">{{ fmtBytes(file.size_bytes) }}</span>
        </div>
      </div>
    </n-card>

    <n-modal
      v-model:show="folderModal.open"
      preset="card"
      title="选择光鸭秒传目录"
      style="width: min(620px, 94vw)"
      :mask-closable="!folderModal.saving"
    >
      <div class="flex items-center gap-3">
        <n-button
          size="small"
          quaternary
          :disabled="folderModal.stack.length <= 1"
          @click="backFolder"
        >
          <template #icon><ArrowLeft :size="15" /></template>
          上一级
        </n-button>
        <span class="mono text-[13px] muted truncate">{{ currentFolderPath }}</span>
      </div>

      <n-spin class="block mt-3" :show="folderModal.loading">
        <div class="min-h-[220px] max-h-[52vh] overflow-auto rounded-xl bg-[var(--app-panel-2)] p-2">
          <button
            v-for="folder in folderModal.folders"
            :key="folder.id"
            type="button"
            class="flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-[13.5px] hover:bg-[var(--app-panel)]"
            @click="enterFolder(folder)"
          >
            <Folder :size="17" class="text-[var(--app-brand)]" />
            <span class="min-w-0 flex-1 truncate">{{ folder.name }}</span>
            <FolderOpen :size="15" class="text-[var(--app-faint)]" />
          </button>
          <div
            v-if="!folderModal.loading && !folderModal.folders.length"
            class="grid min-h-[190px] place-items-center text-[13px] muted"
          >
            当前目录没有子文件夹
          </div>
        </div>
      </n-spin>

      <template #footer>
        <div class="flex items-center justify-between gap-3">
          <span class="text-[12.5px] muted">当前选择：{{ currentFolderPath }}</span>
          <div class="flex gap-2">
            <n-button @click="folderModal.open = false">取消</n-button>
            <n-button type="primary" :loading="folderModal.saving" @click="saveFolder">
              选择当前文件夹
            </n-button>
          </div>
        </div>
      </template>
    </n-modal>
    </div>
  </div>
</template>

<style scoped>
/* ==========================================================================
   设置页布局：左侧竖向分区导航 + 右侧内容。

   之前是顶部一排横向胶囊吸顶 —— 横向空间不够要左右滑，还和顶栏贴在一起
   容易被盖。竖排导航和整个应用的外壳（左侧栏）是同一套语言，也省掉了
   吸顶偏移那套容易算错的东西。
   ========================================================================== */
.settings {
  display: flex;
  align-items: flex-start;
  gap: 28px;
}

.sidenav {
  position: sticky;
  top: calc(var(--top-h) + 20px);
  flex: 0 0 208px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px;
  border: 1px solid var(--app-line);
  border-radius: 16px;
  background: var(--app-panel);
  box-shadow: var(--shadow-card);
}

.sidenav__head {
  padding: 6px 10px 10px;
  color: var(--app-faint);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.8px;
  text-transform: uppercase;
}

.sidenav__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--app-muted);
  font-family: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition:
    background var(--dur-fast) ease,
    color var(--dur-fast) ease;
}

.sidenav__item svg {
  flex: 0 0 auto;
  opacity: 0.8;
}

.sidenav__label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sidenav__item:hover {
  background: var(--app-panel-2);
  color: var(--app-text);
}

.sidenav__item.on {
  background: var(--app-brand-soft);
  color: var(--app-brand);
  font-weight: 600;
}

.sidenav__item.on svg {
  opacity: 1;
}

/* 左侧强调条，和外层侧边栏的当前页标记是同一个语言 */
.sidenav__item.on::before {
  content: '';
  position: absolute;
  left: -10px;
  top: 50%;
  width: 3px;
  height: 16px;
  border-radius: 0 3px 3px 0;
  background: var(--app-brand);
  transform: translateY(-50%);
}

.settings__body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ---------- 分区卡片：去掉「浮卡」观感，做成一块块设置面板 ---------- */
.settings__body :deep(.n-card) {
  border: 1px solid var(--app-line);
  border-radius: 16px;
  background: var(--app-panel);
  box-shadow: var(--shadow-card);
  transition: border-color var(--dur) ease, box-shadow var(--dur) var(--ease-out);
}

.settings__body :deep(.n-card:hover) {
  border-color: color-mix(in srgb, var(--app-brand) 24%, var(--app-line));
}

/* 头部：图标装进一个圆角色块，标题加重，和正文拉开层次 */
.settings__body :deep(.n-card-header) {
  padding: 16px 20px 14px;
  border-bottom: 1px solid var(--app-line-soft);
}

.settings__body :deep(.n-card-header__main > .flex > svg:first-child) {
  box-sizing: content-box;
  width: 17px;
  height: 17px;
  padding: 7px;
  border-radius: 10px;
  background: var(--app-brand-soft);
  color: var(--app-brand);
}

.settings__body :deep(.n-card-header__main .font-semibold) {
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.1px;
}

.settings__body :deep(.n-card__content) {
  padding: 18px 20px 20px;
}

/* 滚动锚点：跳过去时留出顶栏的高度，别让标题被盖住 */
.scroll-anchor {
  scroll-margin-top: calc(var(--top-h) + 24px);
}

.settings__body :deep(.n-card[id]) {
  scroll-margin-top: calc(var(--top-h) + 24px);
}

/* ---------- 窄屏：导航收起来，设置项本来就能一路滚下去 ---------- */
@media (max-width: 1080px) {
  .settings {
    display: block;
  }

  .sidenav {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .sidenav__item,
  .settings__body :deep(.n-card) {
    transition: none;
  }
}
</style>
