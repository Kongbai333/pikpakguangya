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

async function toggleMedia(row, enabled) {
  if (!selected.value) return
  try {
    await api.post('/api/watch/media', {
      chat_id: row.chat_id,
      account_id: selected.value.id,
      enabled,
      title: row.title || '',
      username: row.username || '',
      kind: row.kind || '',
    })
    row.media_enabled = enabled
    message.success(enabled ? '已开启媒体采集' : '已关闭媒体采集')
    await load()
  } catch (e) {
    message.error(e.message)
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
    title: '媒体',
    key: 'media',
    width: 80,
    align: 'center',
    render: (row) =>
      h(NSwitch, {
        value: !!row.media_enabled,
        size: 'small',
        onUpdateValue: (v) => toggleMedia(row, v),
      }),
  },
])

watch(selectedId, () => {
  chats.value = []
  loadChats()
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
