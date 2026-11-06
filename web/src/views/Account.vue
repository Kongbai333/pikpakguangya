<script setup>
/**
 * 账号与监控。
 *
 * 左边选账号，右边是这个账号的采集开关、代理出口和群组勾选。
 * api_id / api_hash 是全局的，放在「设置」页。
 */
import { computed, h, onMounted, reactive, ref, watch } from 'vue'
import { NSwitch, useDialog, useMessage } from 'naive-ui'
import {
  Pencil,
  Plus,
  RefreshCw,
  Send,
  Trash2,
  UserRound,
  LogOut,
  Users,
} from 'lucide-vue-next'

import * as api from '../api'
import { loadAuth, state } from '../store'
import { fmtTime, meName } from '../format'
import { usePolling } from '../polling'

const message = useMessage()
const dialog = useDialog()

const busy = ref('')
const loading = ref(false)
const accounts = ref([])
const activeId = ref(null)
const selectedId = ref(0)
const refreshedAt = ref(0)
const chats = ref([])
const chatsLoading = ref(false)

/* ---------- 视频转发 ---------- */
const forwardBusy = ref('')
const forwardCandidates = ref([])
const forwardPick = ref(null)
const forwardMode = ref('direct')
const forwardDiag = ref(null)
const forward = reactive({
  account_id: 0,
  target: {},
  stats: {},
  relay: { chat: {}, stats: {}, token_set: false },
  chats: [],
})

const selected = computed(
  () => accounts.value.find((row) => row.id === selectedId.value) || null
)
const updated = computed(() =>
  refreshedAt.value
    ? new Date(refreshedAt.value).toLocaleTimeString('zh-CN', { hour12: false })
    : '—'
)

/* ---------- 加群 ---------- */
const joinOf = ref(null)
const joinOpen = ref(false)
const joinTarget = ref('')
const joining = ref(false)

const a = computed(() => state.auth || {})
const credOk = computed(() => !!a.value.api_id_set)

/* ---------- 代理选项 ---------- */
const proxies = ref([])
const proxyOf = ref(0)
/* 账号可以绑 http / https / socks5 / mtproto —— 这几种 Telethon 都能用。
   不绑就是直连，同样是合法配置：能直连 Telegram 的机器（海外 VPS、部分家宽）
   根本不需要代理，不该被当成没配好。 */
const proxyOptions = computed(() => [
  { label: '直连（不绑定代理）', value: 0 },
  ...proxies.value.map((p) => ({
    label: `${p.name || p.addr + ':' + p.port} · ${p.proxy_type.toUpperCase()}`,
    value: p.id,
  })),
])

async function loadProxies() {
  try {
    const j = await api.get('/api/proxies')
    proxies.value = j.proxies || []
  } catch {
    /* 还没配代理也不影响账号管理 */
  }
}

function stateTag(row) {
  if (row.collecting) return { type: 'success', label: '采集中' }
  if (row.collect_enabled) return { type: 'warning', label: '未运行' }
  return { type: 'default', label: '采集关闭' }
}

/* ---------- 编辑账号 ---------- */
const editOpen = ref(false)
const editOf = ref(null)
const editBusy = ref('')
const editForm = reactive({ alias: '', phone: '', proxyId: 0 })

function openEdit(row) {
  editOf.value = row
  editForm.alias = row.alias || ''
  editForm.phone = row.phone || ''
  editForm.proxyId = row.proxy_id || 0
  editOpen.value = true
}

async function saveEdit() {
  if (!editOf.value) return
  editBusy.value = 'save'
  try {
    await api.put(`/api/accounts/${editOf.value.id}/profile`, {
      alias: editForm.alias,
      phone: editForm.phone,
    })
    await api.put(`/api/accounts/${editOf.value.id}/proxy`, {
      proxy_id: editForm.proxyId,
    })
    editOpen.value = false
    message.success('账号已保存')
    await Promise.all([load(), loadAuth()])
  } catch (e) {
    message.error(e.message)
  } finally {
    editBusy.value = ''
  }
}

/* ---------- 群列表 ---------- */
async function loadChats() {
  if (!selected.value) {
    chats.value = []
    return
  }
  chatsLoading.value = true
  try {
    const j = await api.get('/api/chats', { account_id: selected.value.id })
    chats.value = j.chats || []
  } catch (e) {
    message.error(e.message)
    chats.value = []
  } finally {
    chatsLoading.value = false
  }
}

async function toggleWatch(row, enabled) {
  if (!selected.value) return
  try {
    await api.post('/api/watch/toggle', {
      chat_id: row.chat_id,
      account_id: selected.value.id,
      enabled,
      title: row.title || '',
      username: row.username || '',
      kind: row.kind || '',
    })
    row.watched = enabled
    message.success(enabled ? '已开启监控' : '已关闭监控')
    await load()
  } catch (e) {
    message.error(e.message)
  }
}

async function toggleForward(row, enabled) {
  if (!selected.value) return
  if (enabled && !row.watched) {
    // 转发靠采集的实时链路，群没在监控就收不到新消息
    row.forward_enabled = false
    message.warning('先打开这个群的「监控」，转发才有消息来源')
    return
  }
  try {
    await api.post('/api/forward/toggle', {
      chat_id: row.chat_id,
      account_id: selected.value.id,
      enabled,
    })
    row.forward_enabled = enabled
    message.success(enabled ? '已开启视频转发' : '已关闭视频转发')
  } catch (e) {
    message.error(e.message)
    row.forward_enabled = !enabled
  }
}

async function loadForward() {
  if (!selected.value) {
    forward.target = {}
    forward.stats = {}
    return
  }
  try {
    const data = await api.get('/api/forward', { account_id: selected.value.id })
    forward.account_id = data.account_id || 0
    forward.target = data.target || {}
    forward.stats = data.stats || {}
    forward.relay = data.relay || { chat: {}, stats: {}, token_set: false }
    forwardMode.value = forward.target.mode || 'direct'
  } catch (e) {
    message.error(e.message)
  }
}

async function createForwardGroup() {
  if (!selected.value) return
  forwardBusy.value = 'create'
  try {
    const data = await api.post('/api/forward/group', {
      account_id: selected.value.id,
      title: 'PikPak 视频转发',
    })
    forward.target = data.target || {}
    forward.stats = data.stats || {}
    message.success(`已创建转发群组：${forward.target.title || ''}`)
    await loadChats()
  } catch (e) {
    message.error(e.message)
  } finally {
    forwardBusy.value = ''
  }
}

async function clearForwardGroup() {
  if (!selected.value) return
  dialog.warning({
    title: '解除转发绑定',
    content: '只取消绑定，群本身还在 Telegram 里，要删请到客户端删。',
    positiveText: '解除',
    negativeText: '取消',
    onPositiveClick: async () => {
      forwardBusy.value = 'clear'
      try {
        const data = await api.del('/api/forward/group', {
          account_id: selected.value.id,
        })
        forward.target = data?.target || {}
        forward.stats = data?.stats || {}
        message.success('已解除转发绑定')
      } catch (e) {
        message.error(e.message)
      } finally {
        forwardBusy.value = ''
      }
    },
  })
}

/** 账号被限制建群（spamreported）时的出路：绑一个已有的超级群/频道。 */
async function loadForwardCandidates() {
  if (!selected.value) return
  forwardBusy.value = 'candidates'
  try {
    const data = await api.get('/api/forward/candidates', {
      account_id: selected.value.id,
    })
    forwardCandidates.value = data.chats || []
    if (!forwardCandidates.value.length) {
      message.warning('这个账号没有可用作转发目标的群组/频道')
    }
  } catch (e) {
    message.error(e.message)
  } finally {
    forwardBusy.value = ''
  }
}

async function bindForwardTarget() {
  if (!selected.value || !forwardPick.value) return
  forwardBusy.value = 'bind'
  try {
    const data = await api.post('/api/forward/target', {
      account_id: selected.value.id,
      chat_id: forwardPick.value,
      mode: forwardMode.value,
    })
    forward.target = data.target || {}
    forward.stats = data.stats || {}
    forward.relay = data.relay || forward.relay
    message.success(`已绑定转发目标：${forward.target.title || ''}`)
  } catch (e) {
    message.error(e.message)
  } finally {
    forwardBusy.value = ''
  }
}

/** 让 bot 往目标群发一条测试消息，确认权限真的够。 */async function testForwardChannel() {
  if (!selected.value) return
  forwardBusy.value = 'test'
  try {
    await api.post(`/api/forward/test?account_id=${selected.value.id}`, {})
    message.success('bot 已经往目标群发了一条消息，去群里看看')
  } catch (e) {
    message.error(e.message)
  } finally {
    forwardBusy.value = ''
  }
}

/** 逐项查「日志说转了、群里却没有」到底卡在哪一步。 */
async function diagnoseForward() {
  if (!selected.value) return
  forwardBusy.value = 'diagnose'
  try {
    forwardDiag.value = await api.get('/api/forward/diagnose', {
      account_id: selected.value.id,
    })
  } catch (e) {
    message.error(e.message)
  } finally {
    forwardBusy.value = ''
  }
}

function chatStat(row) {
  const key = String(row.chat_id || '').replace(/^-/, '').replace(/^100/, '')
  const hit = (selected.value?.watched || []).find((x) => {
    const other = String(x.chat_id || '').replace(/^-/, '').replace(/^100/, '')
    return other === key
  })
  return hit || { links: 0, messages: 0, last_at: '' }
}

const chatColumns = computed(() => [
  {
    title: '群组',
    key: 'title',
    minWidth: 260,
    render: (row) =>
      h('div', [
        h('div', row.title || row.chat_id),
        row.username
          ? h('div', { class: 'mono muted text-[12.5px]' }, '@' + row.username)
          : null,
      ]),
  },
  { title: '链接', key: 'links', width: 80, align: 'right', render: (row) => chatStat(row).links },
  { title: '消息', key: 'messages', width: 80, align: 'right', render: (row) => chatStat(row).messages },
  {
    title: '最后一条',
    key: 'last_at',
    width: 160,
    render: (row) => h('span', { class: 'muted text-[13px]' }, fmtTime(chatStat(row).last_at)),
  },
  {
    title: '监控',
    key: 'watched',
    width: 80,
    align: 'center',
    render: (row) =>
      h(NSwitch, {
        value: !!row.watched,
        size: 'small',
        onUpdateValue: (v) => toggleWatch(row, v),
      }),
  },
  {
    title: '转发',
    key: 'forward',
    width: 80,
    align: 'center',
    render: (row) =>
      h(NSwitch, {
        value: !!row.forward_enabled,
        size: 'small',
        onUpdateValue: (v) => toggleForward(row, v),
      }),
  },
])

watch(selectedId, () => {
  chats.value = []
  loadChats()
  loadForward()
})

async function openJoin(row) {
  joinOf.value = row
  joinTarget.value = ''
  joinOpen.value = true
}

async function doJoin() {
  const target = joinTarget.value.trim()
  if (!target) {
    message.warning('填 @username、t.me 链接或 t.me/+邀请链接')
    return
  }
  joining.value = true
  try {
    const j = await api.post('/api/chats/join', { target, account_id: joinOf.value?.id })
    if (j.joined) {
      message.success(`已加入${j.title ? '：' + j.title : ''}，并勾上监控`)
      joinOpen.value = false
      await loadChats()
    } else {
      message.warning(j.message || '已提交入群申请，等管理员批准')
    }
  } catch (e) {
    message.error(e.message)
  } finally {
    joining.value = false
  }
}

/* ---------- 列表 ---------- */
async function load(showLoading = false) {
  if (showLoading || !accounts.value.length) loading.value = true
  try {
    const j = await api.get('/api/monitor')
    accounts.value = j.accounts || []
    activeId.value = j.active_id
    refreshedAt.value = Date.now()
    if (!accounts.value.some((row) => row.id === selectedId.value)) {
      selectedId.value = accounts.value[0]?.id || 0
    }
  } catch (e) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

function title(row) {
  return row.name || row.username || row.phone || `#${row.id}`
}

/* ---------- 行操作 ---------- */
async function activate(row) {
  busy.value = `act-${row.id}`
  try {
    await api.post(`/api/accounts/${row.id}/activate`)
    message.success(`群列表和校验出口改用 ${title(row)}`)
    await Promise.all([load(), loadAuth()])
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = ''
  }
}

async function toggleCollect(row, enabled) {
  busy.value = `collect-${row.id}`
  const before = row.collect_enabled
  row.collect_enabled = enabled
  try {
    const j = await api.post(`/api/accounts/${row.id}/collect`, { enabled })
    if (j.accounts) accounts.value = j.accounts
    message.success(
      enabled ? '采集已打开，程序下次启动也会自动开始' : '采集已关闭'
    )
  } catch (e) {
    row.collect_enabled = before
    message.error(e.message)
  } finally {
    busy.value = ''
    await load()
  }
}

async function sync(row) {
  busy.value = `sync-${row.id}`
  try {
    await api.post(`/api/accounts/${row.id}/sync`)
    message.success('已刷新账号信息')
    await Promise.all([load(), loadAuth()])
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = ''
  }
}

function remove(row) {
  dialog.warning({
    title: '删除账号',
    content: `删除「${title(row)}」？登录态文件会一起删掉，以后要重新走一遍验证码。`,
    positiveText: '删除',
    negativeText: '算了',
    onPositiveClick: async () => {
      busy.value = `del-${row.id}`
      try {
        await api.del(`/api/accounts/${row.id}`)
        message.success('已删除')
        await Promise.all([load(), loadAuth()])
      } catch (e) {
        message.error(e.message)
      } finally {
        busy.value = ''
      }
    },
  })
}

function logout() {
  dialog.warning({
    title: '退出登录',
    content: '退出当前账号的登录态？账号还在列表里，重新添加同一个手机号就能登回来。',
    positiveText: '退出',
    negativeText: '算了',
    onPositiveClick: async () => {
      busy.value = 'logout'
      try {
        await api.post('/api/auth/logout')
        message.success('已退出登录')
        await Promise.all([load(), loadAuth()])
      } catch (e) {
        message.error(e.message)
      } finally {
        busy.value = ''
      }
    },
  })
}

/* ---------- 添加账号 ---------- */
const addOpen = ref(false)
const step = ref('phone') // phone | code | password
const addBusy = ref('')
const pendingId = ref(null)
const form = reactive({ phone: '', code: '', password: '' })

const stepIndex = computed(() => ['phone', 'code', 'password'].indexOf(step.value) + 1)

function openAdd() {
  form.phone = a.value.phone || ''
  form.code = ''
  form.password = ''
  pendingId.value = null
  step.value = 'phone'
  proxyOf.value = proxies.value[0]?.id || 0
  addOpen.value = true
}

async function sendCode() {
  if (!form.phone.trim()) {
    message.warning('请填手机号')
    return
  }
  addBusy.value = 'code'
  try {
    const j = await api.post('/api/accounts', {
      phone: form.phone,
      proxy_id: proxyOf.value || 0,
    })
    pendingId.value = j.account_id
    step.value = 'code'
    message.success('验证码已发出，去 Telegram 里看（是 App 内的消息，不是短信）')
  } catch (e) {
    message.error(e.message)
  } finally {
    addBusy.value = ''
  }
}

async function submitCode() {
  addBusy.value = 'signin'
  try {
    const j = await api.post(`/api/accounts/${pendingId.value}/code`, { code: form.code })
    if (j.need_password) {
      step.value = 'password'
      message.warning('这个账号开了两步验证，请填二级密码')
      return
    }
    await finish(j)
  } catch (e) {
    message.error(e.message)
  } finally {
    addBusy.value = ''
  }
}

async function submitPassword() {
  addBusy.value = 'password'
  try {
    await finish(
      await api.post(`/api/accounts/${pendingId.value}/password`, { password: form.password })
    )
  } catch (e) {
    message.error(e.message)
  } finally {
    addBusy.value = ''
  }
}

async function finish(j) {
  message.success(`已添加 ${meName(j.me) || form.phone}`)
  addOpen.value = false
  await Promise.all([load(), loadAuth()])
}

onMounted(() => Promise.all([load(true), loadProxies(), loadAuth()]))
// 标签页在后台时跳过整轮，切回来立刻补一次
usePolling(load, 10000)
</script>

<template>
  <div class="grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)] items-start">
    <!-- 左：账号列表 -->
    <n-card size="small" title="账号">
      <template #header-extra>
        <n-button size="small" type="primary" @click="openAdd">
          <template #icon><Plus :size="15" /></template>
          添加账号
        </n-button>
      </template>

      <n-spin :show="loading">
        <div class="flex flex-col gap-2">
          <button
            v-for="row in accounts"
            :key="row.id"
            type="button"
            class="w-full rounded-lg border px-3 py-2 text-left transition-colors"
            :class="
              row.id === selectedId
                ? 'border-[var(--app-brand)] bg-[var(--app-brand-soft)]'
                : 'border-[var(--app-line)] hover:border-[var(--app-brand)]'
            "
            @click="selectedId = row.id"
          >
            <div class="flex items-center gap-2">
              <span class="font-medium truncate">{{ title(row) }}</span>
              <n-tag v-if="row.id === activeId" class="ml-auto" size="tiny" round>当前</n-tag>
              <n-tag
                size="tiny"
                round
                :class="{ 'ml-auto': row.id !== activeId }"
                :type="stateTag(row).type"
              >
                {{ stateTag(row).label }}
              </n-tag>
            </div>
            <div class="text-[12.5px] muted mt-1">{{ row.phone || '未填手机号' }}</div>
            <div class="text-[12.5px] mt-1">
              监控 {{ row.watched?.length || 0 }} 个群 · 链接 {{ row.links || 0 }}
            </div>
            <div class="text-[12.5px] muted truncate mt-1" :title="row.proxy_from">
              {{ row.proxy_from }}
            </div>
          </button>

          <div v-if="!accounts.length" class="muted text-[13.5px] py-8 text-center">
            还没有账号，点右上角「添加账号」。
          </div>
        </div>
      </n-spin>
    </n-card>

    <!-- 右：账号详情 -->
    <n-card size="small">
      <template v-if="selected" #header>
        <div class="flex items-center gap-2 min-w-0">
          <UserRound :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold truncate">{{ title(selected) }}</span>
          <n-tag v-if="selected.id === activeId" size="tiny" round>当前使用</n-tag>
          <n-tag size="tiny" round :type="stateTag(selected).type">
            {{ stateTag(selected).label }}
          </n-tag>
          <n-tag
            v-if="selected.collect_error"
            size="tiny"
            round
            type="warning"
            :title="selected.collect_error"
          >
            采集未运行
          </n-tag>
          <span class="ml-auto text-[12.5px] muted truncate">
            监控 {{ selected.watched?.length || 0 }} 个群 · 刷新 {{ updated }}
          </span>
        </div>
      </template>

      <template v-if="selected">
        <div class="flex flex-wrap items-center gap-2">
          <div class="flex items-center gap-2 text-[13.5px] mr-1">
            <n-switch
              :value="selected.collect_enabled || selected.collecting"
              :loading="busy === `collect-${selected.id}`"
              @update:value="(v) => toggleCollect(selected, v)"
            />
            采集
          </div>
          <n-button size="small" secondary @click="openJoin(selected)">
            <template #icon><Users :size="15" /></template>
            加入群组
          </n-button>
          <n-button size="small" secondary @click="openEdit(selected)">
            <template #icon><Pencil :size="15" /></template>
            编辑账号
          </n-button>
          <n-button
            v-if="selected.id !== activeId"
            size="small"
            type="primary"
            :loading="busy === `act-${selected.id}`"
            @click="activate(selected)"
          >
            设为当前
          </n-button>
          <n-button
            size="small"
            secondary
            :loading="busy === `sync-${selected.id}`"
            @click="sync(selected)"
          >
            <template #icon><RefreshCw :size="15" /></template>
            刷新账号
          </n-button>
          <n-button
            v-if="selected.id === activeId && a.authorized"
            size="small"
            tertiary
            type="warning"
            @click="logout"
          >
            <template #icon><LogOut :size="15" /></template>
            退出登录
          </n-button>
          <n-button
            size="small"
            tertiary
            type="error"
            :loading="busy === `del-${selected.id}`"
            @click="remove(selected)"
          >
            <template #icon><Trash2 :size="15" /></template>
            删除
          </n-button>
          <n-tag v-if="!credOk" size="small" type="warning">
            还没配 api_id / api_hash，去「设置」页填写
          </n-tag>
        </div>

        <div class="mt-3 grid gap-1 text-[13px] rounded-md bg-[var(--app-panel-2)] px-3 py-2">
          <div class="truncate">
            <span class="muted">代理出口：</span>
            {{ selected.proxy_from }}
            <span v-if="selected.proxy_warning" class="text-[var(--app-danger)] ml-2">
              {{ selected.proxy_warning }}
            </span>
          </div>
          <div class="truncate">
            <span class="muted">登录态：</span>
            <span class="mono">{{ selected.session_file || selected.session || '—' }}</span>
            <span class="muted ml-3">最后登录 {{ fmtTime(selected.last_login_at) }}</span>
          </div>
          <div v-if="selected.collect_error" class="text-[var(--app-warn)] truncate">
            <span class="muted">采集状态：</span>{{ selected.collect_error }}
          </div>
        </div>

        <!-- 视频转发：目标群 + 统计 -->
        <div class="mt-3 rounded-md border border-[var(--app-border)] px-3 py-2.5">
          <div class="flex flex-wrap items-center gap-2">
            <Send :size="15" class="text-[var(--app-brand)]" />
            <span class="text-[13.5px] font-medium">视频转发</span>
            <n-tag v-if="forward.target.title" size="small" round type="success">
              {{ forward.target.title }}
            </n-tag>
            <n-tag v-else size="small" round>还没有转发群组</n-tag>
            <span class="flex-1"></span>
            <n-button
              size="tiny"
              :loading="forwardBusy === 'create'"
              @click="createForwardGroup"
            >
              {{ forward.target.title ? '重新建一个' : '创建转发群组' }}
            </n-button>
            <n-button
              v-if="forward.target.title"
              size="tiny"
              tertiary
              type="error"
              :loading="forwardBusy === 'clear'"
              @click="clearForwardGroup"
            >
              解除绑定
            </n-button>
          </div>
          <div class="text-[12.5px] muted mt-1.5">
            打开下表的「转发」开关后，新视频会实时转进这个群；只转发新消息，不从历史开始刷。
            <span v-if="forward.stats.forwarded || forward.stats.failed">
              采集号转发 {{ forward.stats.forwarded || 0 }} 组 ·
              失败 {{ forward.stats.failed || 0 }} 次
            </span>
            <span v-if="forward.stats.last_at">· 最近 {{ fmtTime(forward.stats.last_at) }}</span>
          </div>
          <div
            v-if="forward.relay.stats.copied || forward.relay.stats.failed"
            class="text-[12.5px] muted mt-0.5"
          >
            Bot 已投递 {{ forward.relay.stats.copied || 0 }} 条 ·
            失败 {{ forward.relay.stats.failed || 0 }} 次
            <span v-if="forward.relay.stats.last_at">· 最近 {{ fmtTime(forward.relay.stats.last_at) }}</span>
          </div>
          <div v-if="forward.relay.stats.last_error" class="text-[12px] text-[var(--app-danger)] mt-1">
            {{ forward.relay.stats.last_error }}
          </div>

          <n-radio-group v-model:value="forwardMode" class="mt-2" size="small">
            <n-radio-button value="direct">直连（采集号发到群）</n-radio-button>
            <n-radio-button value="bot">经 Bot（bot 发到群）</n-radio-button>
          </n-radio-group>
          <div v-if="forwardDiag" class="mt-2 rounded-md bg-[var(--app-panel-2)] px-2.5 py-2">
            <div class="text-[12px] mb-1" :class="forwardDiag.ok ? 'text-[var(--app-ok)]' : 'text-[var(--app-danger)]'">
              {{ forwardDiag.ok ? '各项检查都通过' : '有项目没通过，看下面带 ✗ 的那几行' }}
            </div>
            <div
              v-for="c in forwardDiag.checks"
              :key="c.name"
              class="text-[12px] leading-5"
            >
              <span :class="c.ok ? 'text-[var(--app-ok)]' : 'text-[var(--app-danger)]'">
                {{ c.ok ? '✓' : '✗' }}
              </span>
              <span class="font-medium ml-1">{{ c.name }}</span>
              <span class="muted ml-1">{{ c.detail }}</span>
            </div>
          </div>
          <div v-if="forwardMode === 'bot'" class="text-[12px] muted mt-1.5">
            采集号只把视频转发给 bot（不往群里刷大文件，降低采集号被风控的风险），
            再由 bot 发到群里。要求：Bot Token 已填、采集号给 bot 发过 <code>/start</code>、
            bot 已在目标群里且有发言权限。
            <span v-if="forward.target.mode === 'bot' && forward.target.bot_username">
              当前 bot：@{{ forward.target.bot_username }}
            </span>
          </div>
          <div class="flex flex-wrap items-center gap-2 mt-2">
            <n-select
              v-model:value="forwardPick"
              class="w-[300px]"
              size="small"
              filterable
              clearable
              :options="forwardCandidates.map((c) => ({ label: `${c.title} · ${c.kind}`, value: c.chat_id }))"
              placeholder="已有群组 / 频道"
              :loading="forwardBusy === 'candidates'"
              @focus="loadForwardCandidates"
            />
            <n-button
              size="tiny"
              :disabled="!forwardPick"
              :loading="forwardBusy === 'bind'"
              @click="bindForwardTarget"
            >
              绑定为目标
            </n-button>
            <n-button
              size="tiny"
              tertiary
              :loading="forwardBusy === 'diagnose'"
              @click="diagnoseForward"
            >
              诊断
            </n-button>
            <n-button
              v-if="forward.target.mode === 'bot'"
              size="tiny"
              tertiary
              :loading="forwardBusy === 'test'"
              @click="testForwardChannel"
            >
              发测试消息
            </n-button>
            <span class="text-[12px] muted">
              账号被限制建群（spamreported）时，绑一个已有群组也能转发；
              普通群也行，但超过 200 人会被升级成超级群、需要重新绑
            </span>
          </div>
          <div v-if="forward.stats.last_error" class="text-[12px] text-[var(--app-danger)] mt-1">
            {{ forward.stats.last_error }}
          </div>
        </div>

        <n-data-table
          class="mt-3"
          size="small"
          :columns="chatColumns"
          :data="chats"
          :loading="chatsLoading"
          :row-key="(row) => row.chat_id"
          :max-height="560"
          :bordered="false"
        />
      </template>

      <div v-else class="grid place-items-center h-64 muted">左边还没有账号。</div>
    </n-card>

    <!-- 加入群组 -->
    <n-modal
      v-model:show="joinOpen"
      preset="card"
      :title="`加入群组 · ${joinOf?.name || joinOf?.phone || ''}`"
      style="width: min(560px, 94vw)"
      :mask-closable="false"
    >
      <div class="text-[13.5px] muted mb-3">
        支持 <b>@username</b>、<b>t.me/xxx</b>、<b>t.me/+邀请码</b>。
        加入成功后会自动勾上监控；私有群需要用邀请链接。
      </div>
      <n-input
        v-model:value="joinTarget"
        size="large"
        clearable
        placeholder="@channelname 或 https://t.me/+xxxxxxxx"
        @keyup.enter="doJoin"
      />
      <template #footer>
        <div class="flex justify-end gap-2">
          <n-button @click="joinOpen = false">取消</n-button>
          <n-button type="primary" :loading="joining" @click="doJoin">加入</n-button>
        </div>
      </template>
    </n-modal>

    <!-- 编辑账号 -->
    <n-modal
      v-model:show="editOpen"
      preset="card"
      :title="`编辑账号 · ${editOf?.name || editOf?.phone || ''}`"
      style="width: min(620px, 94vw)"
      :mask-closable="false"
    >
      <div class="grid md:grid-cols-2 gap-3">
        <div>
          <div class="text-[13px] muted mb-1">备注名</div>
          <n-input v-model:value="editForm.alias" placeholder="只影响这个列表里的显示名" />
        </div>
        <div>
          <div class="text-[13px] muted mb-1">手机号</div>
          <n-input v-model:value="editForm.phone" placeholder="+8613800000000" />
        </div>
        <div class="md:col-span-2">
          <div class="text-[13px] muted mb-1">绑定代理</div>
          <n-select v-model:value="editForm.proxyId" :options="proxyOptions" />
        </div>
      </div>
      <template #footer>
        <div class="flex justify-end gap-2">
          <n-button @click="editOpen = false">取消</n-button>
          <n-button type="primary" :loading="editBusy === 'save'" @click="saveEdit">保存</n-button>
        </div>
      </template>
    </n-modal>

    <!-- 添加账号 -->
    <n-modal
      v-model:show="addOpen"
      preset="card"
      title="添加账号"
      style="width: min(520px, 94vw)"
      :mask-closable="false"
    >
      <n-steps size="small" :current="stepIndex" class="mb-4">
        <n-step title="手机号" />
        <n-step title="验证码" />
        <n-step title="二级密码" />
      </n-steps>

      <div v-if="step === 'phone'" class="flex flex-col gap-2">
        <div class="text-[13.5px] muted">
          用用户账号登录（机器人读不到群消息）。手机号要带国家码，验证码发到 Telegram 里。
        </div>
        <n-input
          v-model:value="form.phone"
          size="large"
          placeholder="+8613800000000"
          @keyup.enter="sendCode"
        />
        <div class="text-[13.5px] muted mt-2">
          绑定代理出口（不同账号可以走不同的一条；不绑就是直连）
        </div>
        <n-select v-model:value="proxyOf" size="large" :options="proxyOptions" />
        <div class="text-[13px] muted">
          还没添加代理就先跳过，去「设置 → 代理池」维护，可以直接粘 tg://proxy 链接。
          能直连 Telegram 的机器不绑也照样跑。
        </div>
      </div>

      <div v-else-if="step === 'code'" class="flex flex-col gap-2">
        <div class="text-[13.5px] muted">
          {{ form.phone }} 的验证码。可能是 Telegram App 内的消息，不是短信。
        </div>
        <n-input
          v-model:value="form.code"
          size="large"
          placeholder="5 位数字"
          @keyup.enter="submitCode"
        />
      </div>

      <div v-else class="flex flex-col gap-2">
        <div class="text-[13.5px] muted">这个账号开了两步验证，填一下二级密码。</div>
        <n-input
          v-model:value="form.password"
          type="password"
          show-password-on="click"
          size="large"
          placeholder="二级密码"
          @keyup.enter="submitPassword"
        />
      </div>

      <template #footer>
        <div class="flex justify-end gap-2">
          <n-button @click="addOpen = false">取消</n-button>
          <n-button
            v-if="step === 'phone'"
            type="primary"
            :loading="addBusy === 'code'"
            @click="sendCode"
          >
            发送验证码
          </n-button>
          <n-button
            v-else-if="step === 'code'"
            type="primary"
            :loading="addBusy === 'signin'"
            @click="submitCode"
          >
            登录
          </n-button>
          <n-button
            v-else
            type="primary"
            :loading="addBusy === 'password'"
            @click="submitPassword"
          >
            提交
          </n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>
