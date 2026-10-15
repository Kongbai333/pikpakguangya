<script setup>
/**
 * 代理：系统代理 + 代理池。
 *
 * 两件事分开管，以前是混在一起的（全局出口就是「当前使用」账号绑的那条）：
 *
 *   系统代理 —— 一条，全局出口。PikPak 校验、分享详情接口、秒传、Bot API 走它，
 *              这些请求不属于任何 TG 账号，需要一个统一的出口。
 *   代理池   —— 一堆，绑给具体的 TG 账号。每个号可以走不同出口，采集和登录用自己那条。
 *
 * 账号不绑代理就是直连 —— 那是合法配置，不是错误。部署在能直连 Telegram 的机器上
 * （海外 VPS、部分家宽）根本不需要代理。
 */
import { computed, h, onMounted, reactive, ref } from 'vue'
import { NButton, NTag, useDialog, useMessage } from 'naive-ui'
import { Globe, Link2, Plus, RefreshCw, Server } from 'lucide-vue-next'

import * as api from '../api'

const message = useMessage()
const dialog = useDialog()

const loading = ref(false)
const busy = ref('')
const rows = ref([])
const accounts = ref([])
const system = reactive({ proxy_id: 0, usable: true, masked: '直连' })
const testResults = reactive({})

const modalOpen = ref(false)
const editingId = ref(0)
const parseBusy = ref(false)
const parseError = ref('')
const secretWarning = ref('')
const form = reactive({
  name: '',
  proxy_type: 'http',
  addr: '',
  port: null,
  username: '',
  password: '',
  secret: '',
  raw: '',
})

const typeOptions = [
  { label: 'HTTP', value: 'http' },
  { label: 'HTTPS', value: 'https' },
  { label: 'SOCKS5', value: 'socks5' },
  { label: 'MTProto（tg://proxy）', value: 'mtproto' },
]

/** MTProto 代理只代理 Telegram，代理不了 PikPak 的 HTTP 请求，所以不能当系统代理。 */
const isMtproto = computed(() => form.proxy_type === 'mtproto')

const systemOptions = computed(() => [
  { label: '直连（不走代理）', value: 0 },
  ...rows.value.map((p) => ({
    label: `${p.name || `${p.addr}:${p.port}`} · ${p.proxy_type.toUpperCase()}`,
    value: p.id,
    disabled: p.proxy_type === 'mtproto',
  })),
])

function labelOf(row) {
  return row.name || `${row.addr}:${row.port}`
}

function boundNames(row) {
  return accounts.value
    .filter((a) => Number(a.proxy_id || 0) === row.id)
    .map((a) => a.alias || a.name || a.phone || `#${a.id}`)
}

async function load() {
  loading.value = true
  try {
    const [proxies, sys, accts] = await Promise.all([
      api.get('/api/proxies'),
      api.get('/api/proxies/system'),
      api.get('/api/accounts').catch(() => ({ accounts: [] })),
    ])
    rows.value = proxies.proxies || []
    Object.assign(system, {
      proxy_id: sys.proxy_id || 0,
      usable: sys.usable !== false,
      masked: sys.masked || '直连',
    })
    accounts.value = accts.accounts || []
  } catch (e) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

function openAdd() {
  editingId.value = 0
  parseError.value = ''
  secretWarning.value = ''
  Object.assign(form, {
    name: '',
    proxy_type: 'http',
    addr: '',
    port: null,
    username: '',
    password: '',
    secret: '',
    raw: '',
  })
  modalOpen.value = true
}

function openEdit(row) {
  editingId.value = row.id
  parseError.value = ''
  secretWarning.value = ''
  Object.assign(form, {
    name: row.name || '',
    proxy_type: row.proxy_type || 'http',
    addr: row.addr || '',
    port: row.port || null,
    username: row.username || '',
    password: row.password || '',
    secret: row.secret || '',
    raw: '',
  })
  modalOpen.value = true
}

/** 粘贴一条链接 → 后端解析 → 回填表单，让用户还能改。 */
async function parseRaw() {
  const raw = form.raw.trim()
  if (!raw) return
  parseBusy.value = true
  parseError.value = ''
  try {
    const j = await api.post('/api/proxies/parse', { raw })
    Object.assign(form, {
      proxy_type: j.proxy_type,
      addr: j.addr,
      port: j.port,
      username: j.username || '',
      password: j.password || '',
      secret: j.secret || '',
    })
    secretWarning.value = j.secret_warning || ''
    message.success(`识别为 ${j.proxy_type.toUpperCase()} 代理`)
  } catch (e) {
    parseError.value = e.message
    secretWarning.value = ''
  } finally {
    parseBusy.value = false
  }
}

async function save() {
  // 粘贴框里还有内容就直接按它解析，用户不用先点「解析」
  if (form.raw.trim() && !form.addr.trim()) await parseRaw()
  if (parseError.value) return
  if (!form.addr.trim()) {
    message.warning('请填代理地址，或粘贴一条代理链接')
    return
  }
  if (!form.port) {
    message.warning('请填代理端口')
    return
  }
  if (isMtproto.value && !form.secret.trim()) {
    message.warning('MTProto 代理要填 secret（tg://proxy 链接里的那串）')
    return
  }
  busy.value = 'save'
  const payload = {
    name: form.name.trim(),
    proxy_type: form.proxy_type,
    addr: form.addr.trim(),
    port: Number(form.port),
    username: form.username.trim(),
    password: form.password,
    secret: form.secret.trim(),
  }
  try {
    if (editingId.value) {
      await api.put(`/api/proxies/${editingId.value}`, payload)
    } else {
      await api.post('/api/proxies', payload)
    }
    modalOpen.value = false
    message.success(editingId.value ? '代理已保存' : '代理已添加')
    await load()
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = ''
  }
}

async function saveSystem(v) {
  try {
    const j = await api.put('/api/proxies/system', { proxy_id: v || 0 })
    Object.assign(system, {
      proxy_id: j.proxy_id || 0,
      usable: j.usable !== false,
      masked: j.masked || '直连',
    })
    message.success(v ? `系统代理已设为「${system.masked}」` : '系统代理已改为直连')
  } catch (e) {
    message.error(e.message)
  }
}

function remove(row) {
  const bound = boundNames(row)
  const extra = [
    bound.length ? `绑定它的 ${bound.length} 个账号会改成直连` : '',
    system.proxy_id === row.id ? '它当前是系统代理，会被改回直连' : '',
  ].filter(Boolean).join('；')
  dialog.warning({
    title: '删除代理',
    content: `删除「${labelOf(row)}」？${extra ? extra + '。' : ''}`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api.del(`/api/proxies/${row.id}`)
        message.success('已删除')
        await load()
      } catch (e) {
        message.error(e.message)
      }
    },
  })
}

async function test(row) {
  busy.value = `test-${row.id}`
  try {
    const j = await api.post(`/api/proxies/${row.id}/test`)
    testResults[row.id] = j
    if (j.all_ok) message.success('连通性正常')
    else message.warning('有不通的目标，看下方结果')
  } catch (e) {
    message.error(e.message)
  } finally {
    busy.value = ''
  }
}

const columns = computed(() => {
  const busyValue = busy.value
  const sysId = system.proxy_id
  return [
    {
      title: '名称',
      key: 'name',
      minWidth: 150,
      render: (row) =>
        h('div', { class: 'flex items-center gap-2' }, [
          h('span', { class: 'truncate' }, labelOf(row)),
          sysId === row.id
            ? h(NTag, { size: 'tiny', type: 'success', round: true }, { default: () => '系统' })
            : null,
        ]),
    },
    {
      title: '类型',
      key: 'proxy_type',
      width: 96,
      render: (row) =>
        h(
          NTag,
          { size: 'small', round: true, type: row.proxy_type === 'mtproto' ? 'info' : 'default' },
          { default: () => row.proxy_type.toUpperCase() }
        ),
    },
    {
      title: '地址',
      key: 'addr',
      minWidth: 190,
      render: (row) => h('span', { class: 'mono' }, `${row.addr}:${row.port}`),
    },
    {
      title: '绑定账号',
      key: 'bound',
      minWidth: 120,
      render: (row) => {
        const names = boundNames(row)
        if (!names.length) return h('span', { class: 'muted' }, '—')
        return h('span', { title: names.join('、') }, `${names.length} 个`)
      },
    },
    {
      title: '操作',
      key: 'actions',
      width: 220,
      render: (row) =>
        h('div', { class: 'flex gap-1' }, [
          h(
            NButton,
            {
              size: 'small',
              secondary: true,
              loading: busyValue === `test-${row.id}`,
              onClick: () => test(row),
            },
            { default: () => '测试' }
          ),
          h(NButton, { size: 'small', secondary: true, onClick: () => openEdit(row) }, {
            default: () => '编辑',
          }),
          h(
            NButton,
            { size: 'small', tertiary: true, type: 'error', onClick: () => remove(row) },
            { default: () => '删除' }
          ),
        ]),
    },
  ]
})

onMounted(load)
</script>

<template>
  <!-- ============ 系统代理 ============ -->
  <n-card size="small">
    <template #header>
      <div class="flex items-center gap-2">
        <Globe :size="16" class="text-[var(--app-brand)]" />
        <span class="font-semibold">系统代理</span>
        <n-tag size="small" round :type="system.proxy_id && system.usable ? 'success' : 'default'">
          {{ system.masked }}
        </n-tag>
      </div>
    </template>

    <p class="text-[13.5px] muted mt-0 mb-3">
      <b>不属于任何 TG 账号</b>的请求走这一条：PikPak 链接校验、分享详情接口、秒传、Telegram Bot API。
      账号采集不走它 —— 那走账号自己在下面绑的那条。不选就是直连。
    </p>

    <div class="flex flex-wrap items-center gap-3">
      <n-select
        class="sys-select"
        :value="system.proxy_id"
        :options="systemOptions"
        @update:value="saveSystem"
      />
      <n-button
        v-if="system.proxy_id"
        size="small"
        secondary
        :loading="busy === `test-${system.proxy_id}`"
        @click="test({ id: system.proxy_id })"
      >
        测试这条
      </n-button>
      <span v-if="system.proxy_id && !system.usable" class="text-[13px] text-[var(--app-danger)]">
        这条是 MTProto 代理，代理不了 HTTP 请求，实际按直连走
      </span>
    </div>
  </n-card>

  <!-- ============ 代理池 ============ -->
  <n-card size="small" class="mt-4">
    <template #header>
      <div class="flex items-center gap-2">
        <Server :size="16" class="text-[var(--app-brand)]" />
        <span class="font-semibold">代理池</span>
        <n-tag size="small" round>{{ rows.length }} 条</n-tag>
      </div>
    </template>
    <template #header-extra>
      <div class="flex gap-2">
        <n-button size="small" secondary :loading="loading" @click="load">
          <template #icon><RefreshCw :size="15" /></template>
          刷新
        </n-button>
        <n-button size="small" type="primary" @click="openAdd">
          <template #icon><Plus :size="15" /></template>
          添加代理
        </n-button>
      </div>
    </template>

    <p class="text-[13.5px] muted mt-0 mb-3">
      这里的是<b>给 Telegram 账号绑的</b>出口，每个号可以走不同的一条。
      账号不绑就是直连 —— 能直连 Telegram 的机器（海外 VPS、部分家宽）不绑也照样跑。
    </p>

    <n-data-table
      size="small"
      :columns="columns"
      :data="rows"
      :loading="loading"
      :row-key="(row) => row.id"
      :scroll-x="820"
      :bordered="false"
    />

    <div v-for="row in rows" :key="`r-${row.id}`" class="mt-2">
      <template v-if="testResults[row.id]">
        <div class="text-[13px] muted mb-1">
          {{ labelOf(row) }} ·
          {{ testResults[row.id].all_ok ? '全部连通' : '有不通的目标' }}
        </div>
        <div class="flex flex-wrap gap-2">
          <n-tag
            v-for="r in testResults[row.id].results"
            :key="r.target"
            size="small"
            :type="r.ok ? 'success' : 'error'"
          >
            {{ r.target }} · {{ r.ok ? `${r.ms}ms` : r.error }}
          </n-tag>
        </div>
        <div v-if="testResults[row.id].note" class="text-[12.5px] muted mt-1">
          {{ testResults[row.id].note }}
        </div>
      </template>
    </div>
  </n-card>

  <!-- ============ 添加 / 编辑 ============ -->
  <n-modal
    v-model:show="modalOpen"
    preset="card"
    :title="editingId ? '编辑代理' : '添加代理'"
    style="width: min(600px, 94vw)"
    :mask-closable="false"
  >
    <!-- 粘贴入口：最省事的路径 -->
    <div class="paste">
      <div class="flex items-center gap-2 mb-1">
        <Link2 :size="14" class="text-[var(--app-brand)]" />
        <span class="text-[13px] font-medium">粘贴代理链接</span>
        <span class="text-[12.5px] muted">支持 tg://proxy、tg://socks、socks5://、地址:端口:用户名:密码</span>
      </div>
      <div class="flex gap-2">
        <n-input
          v-model:value="form.raw"
          placeholder="tg://proxy?server=1.2.3.4&port=443&secret=dd..."
          @keyup.enter="parseRaw"
        />
        <n-button secondary :loading="parseBusy" @click="parseRaw">解析</n-button>
      </div>
      <div v-if="parseError" class="text-[12.5px] text-[var(--app-danger)] mt-1">
        {{ parseError }}
      </div>
      <div v-else class="text-[12.5px] muted mt-1">
        解析后下面的字段会自动填好，可以再改。也可以跳过这步直接手填。
      </div>
    </div>

    <div class="grid md:grid-cols-2 gap-3 mt-4">
      <div class="md:col-span-2">
        <div class="text-[13px] muted mb-1">名称</div>
        <n-input v-model:value="form.name" placeholder="例如：香港 SOCKS5" />
      </div>
      <div>
        <div class="text-[13px] muted mb-1">类型</div>
        <n-select v-model:value="form.proxy_type" :options="typeOptions" />
      </div>
      <div>
        <div class="text-[13px] muted mb-1">端口</div>
        <n-input-number v-model:value="form.port" class="w-full" placeholder="1080" />
      </div>
      <div class="md:col-span-2">
        <div class="text-[13px] muted mb-1">地址</div>
        <n-input v-model:value="form.addr" placeholder="127.0.0.1 或 proxy.example.com" />
      </div>

      <template v-if="isMtproto">
        <div class="md:col-span-2">
          <div class="text-[13px] muted mb-1">Secret</div>
          <n-input
            v-model:value="form.secret"
            placeholder="dd0123456789abcdef0123456789abcdef"
          />
          <div class="text-[12.5px] muted mt-1">
            只有 MTProto 代理用得到，就是 tg://proxy 链接里 secret= 后面那串。
          </div>
        </div>
      </template>
      <template v-else>
        <div>
          <div class="text-[13px] muted mb-1">用户名</div>
          <n-input v-model:value="form.username" />
        </div>
        <div>
          <div class="text-[13px] muted mb-1">密码</div>
          <n-input v-model:value="form.password" type="password" show-password-on="click" />
        </div>
      </template>
    </div>

    <n-alert v-if="isMtproto" type="info" :show-icon="true" class="mt-3" :bordered="false">
      MTProto 代理只能给 Telegram 用，代理不了 PikPak 的 HTTP 请求，所以不能当系统代理。
      <template v-if="secretWarning">
        <br />{{ secretWarning }}
      </template>
    </n-alert>

    <template #footer>
      <div class="flex justify-end gap-2">
        <n-button @click="modalOpen = false">取消</n-button>
        <n-button type="primary" :loading="busy === 'save'" @click="save">保存</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.sys-select {
  min-width: 280px;
  flex: 0 1 340px;
}

.paste {
  padding: 12px;
  border: 1px solid var(--app-line);
  border-radius: 12px;
  background: var(--app-panel);
}
</style>
