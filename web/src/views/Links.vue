<script setup>
/** 分享链接管理：搜索、筛选、检测、显示开关、批量删除。 */
import { computed, h, onMounted, reactive, ref } from 'vue'
import { NButton, NSwitch, NTag, useDialog, useMessage } from 'naive-ui'
import { RefreshCw, ScanSearch, Search, Trash2 } from 'lucide-vue-next'

import * as api from '../api'
import { checkStatus, copyPayload, copyText, fmtBytes, fmtTime, rowStatus } from '../format'
import { useTableHeight } from '../table'

const message = useMessage()
const dialog = useDialog()

const loading = ref(false)
const checking = ref(false)
const cleaning = ref(false)
const checkingId = ref(0)
const transferId = ref(0)
const rows = ref([])
const total = ref(0)
const sources = ref([])
const statusCounts = ref({})
const statusLabels = ref({})
const selectedKeys = ref([])
const error = ref('')

// 表格铺满当前屏幕。扣掉的是：顶栏 64 + 内容区上留白 24 + 工具栏 ~105
// + 间距 12 + 分页条 ~57 + 下留白 48 ≈ 310。
// 勾选后批量条会插进来（约 56px），那时再让表格矮一截，避免整页多出一条滚动条。
// （必须放在 selectedKeys 之后：useTableHeight 里的 watchEffect 会立即执行，
//   声明顺序反了就是 TDZ 报错。）
const tableHeight = useTableHeight(() => (selectedKeys.value.length ? 366 : 310))

const sourceModal = reactive({ open: false, row: null, value: '' })

const q = reactive({
  text: '',
  source: '',
  hidden: '',
  status: '',
  withCode: '',
  page: 1,
  size: 20,
  sort: 'id',
  dir: 'desc',
})

/**
 * 筛选条件记在 sessionStorage 里：这一页的筛选项有五六个，去「链接详情」或
 * 切到别的页再回来就全没了，得重设一遍。只存本次会话，关掉标签页就清空。
 */
const FILTER_KEY = 'pk.links.filters'

function loadFilters() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(FILTER_KEY) || '{}')
    Object.assign(q, {
      text: typeof saved.text === 'string' ? saved.text : '',
      source: saved.source || '',
      hidden: saved.hidden || '',
      status: saved.status || '',
      withCode: saved.withCode || '',
      size: Number(saved.size) || 20,
      page: 1,
    })
  } catch {
    /* 存的 JSON 坏了就按默认值来，不值得报错打断页面 */
  }
}

function saveFilters() {
  try {
    sessionStorage.setItem(
      FILTER_KEY,
      JSON.stringify({
        text: q.text,
        source: q.source,
        hidden: q.hidden,
        status: q.status,
        withCode: q.withCode,
        size: q.size,
      })
    )
  } catch {
    /* 隐私模式下 sessionStorage 可能不可写 */
  }
}

const hiddenCount = computed(() => rows.value.filter((r) => r.HIDDEN).length)
const removableCount = computed(() =>
  Object.entries(statusCounts.value || {}).reduce((sum, [status, count]) => {
    if (['unchecked', '', 'OK', 'PASS_CODE_EMPTY'].includes(status)) return sum
    return sum + Number(count || 0)
  }, 0)
)
const selectedRows = computed(() =>
  rows.value.filter((r) => selectedKeys.value.includes(r.ID))
)
const statusOptions = computed(() => {
  const c = statusCounts.value || {}
  const labels = statusLabels.value || {}
  const sum = Object.values(c).reduce((a, b) => a + b, 0)
  const out = [{ value: '', label: `状态不限（${sum}）` }]
  if (c.unchecked) out.push({ value: 'unchecked', label: `未检测（${c.unchecked}）` })
  Object.keys(c)
    .filter((k) => k !== 'unchecked')
    .sort((a, b) => c[b] - c[a])
    .forEach((k) => out.push({ value: k, label: `${labels[k] || k}（${c[k]}）` }))
  return out
})

const sourceOptions = computed(() => [
  { value: '', label: '全部来源' },
  ...sources.value.map((s) => ({
    value: s.source,
    label: `${s.source}（${s.total}${s.hidden ? '，隐藏 ' + s.hidden : ''}）`,
  })),
])

const hiddenOptions = [
  { value: '', label: '显示不限' },
  { value: 'false', label: '显示中' },
  { value: 'true', label: '已隐藏' },
]
const codeOptions = [
  { value: '', label: '提取码不限' },
  { value: 'true', label: '有提取码' },
  { value: 'false', label: '真没提取码' },
]
const sizeOptions = [
  { value: 20, label: '20 条/页' },
  { value: 50, label: '50 条/页' },
  { value: 100, label: '100 条/页' },
]

async function load(page) {
  if (page) q.page = page
  loading.value = true
  error.value = ''
  try {
    const j = await api.get('/api/admin/links', {
      q: q.text,
      source: q.source,
      hidden: q.hidden,
      status: q.status,
      with_code: q.withCode,
      page: q.page,
      size: q.size,
      sort: q.sort,
      dir: q.dir,
    })
    rows.value = j.items || []
    total.value = j.total || 0
    sources.value = j.sources || []
    statusCounts.value = j.status_counts || {}
    statusLabels.value = j.status_labels || {}
    selectedKeys.value = []
    saveFilters()
  } catch (e) {
    error.value = e.message
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function needSelection() {
  if (!selectedRows.value.length) {
    message.warning('先勾选几条')
    return false
  }
  return true
}

function onSelectionChange(keys) {
  selectedKeys.value = keys
}

/* ---------- 检测 ---------- */
function summarize(j) {
  const counts = {}
  ;(j.results || []).forEach((r) => {
    counts[r.status] = (counts[r.status] || 0) + 1
  })
  const parts = Object.entries(counts).map(([st, n]) => `${checkStatus(st).label} ${n}`)
  return parts.join(' / ') || '没有结果'
}

async function runCheck(ids, tip) {
  checking.value = true
  try {
    const j = await api.postLong('/api/admin/links/check', { ids })
    statusCounts.value = j.status_counts || statusCounts.value
    message.success(`检测完成（${tip}）：${summarize(j)}`)
    await load(0)
  } catch (e) {
    message.error(e.message)
  } finally {
    checking.value = false
  }
}

function checkOne(row) {
  checkingId.value = row.ID
  return runCheck([row.ID], `#${row.ID}`).finally(() => {
    checkingId.value = 0
  })
}

function checkSelected() {
  if (!needSelection()) return
  return runCheck(selectedRows.value.map((r) => r.ID), `选中 ${selectedRows.value.length} 条`)
}

function checkPage() {
  if (!rows.value.length) return
  return runCheck(rows.value.map((r) => r.ID), `本页 ${rows.value.length} 条`)
}

async function checkPending() {
  try {
    const j = await api.post('/api/check?limit=200')
    message.success(`已经在后台排队检测 ${j.queued} 条，过一会儿点「查询」刷新看结果`)
  } catch (e) {
    message.error(e.message)
  }
}

/* ---------- 显示 / 删除 / 来源 ---------- */
async function setHidden(hidden, one) {
  const target = one ? [one.ID] : selectedRows.value.map((r) => r.ID)
  if (!one && !needSelection()) return
  try {
    const j = await api.post('/api/admin/links/hide', { ids: target, hidden })
    message.success(`已${hidden ? '隐藏' : '显示'} ${j.updated} 条`)
    await load(0)
  } catch (e) {
    message.error(e.message)
  }
}

function remove(one) {
  const target = one ? [one.ID] : selectedRows.value.map((r) => r.ID)
  if (!one && !needSelection()) return
  dialog.warning({
    title: '删除链接',
    content: one
      ? `彻底删除这条链接？前台和列表里都不会再有，且不可恢复。`
      : `彻底删除选中的 ${target.length} 条链接？不可恢复。`,
    positiveText: '删除',
    negativeText: '算了',
    onPositiveClick: async () => {
      try {
        const j = await api.post('/api/admin/links/delete', { ids: target })
        message.success(`已删除 ${j.deleted} 条`)
        await load(1)
      } catch (e) {
        message.error(e.message)
      }
    },
  })
}

function cleanupUnwanted() {
  if (!removableCount.value) return
  dialog.warning({
    title: '一键清理',
    content:
      `将永久清理 ${removableCount.value} 条链接，只保留未检测、有效、需提取码的数据。` +
      '原始消息仍会留档，此操作不可恢复。',
    positiveText: '确认清理',
    negativeText: '取消',
    onPositiveClick: async () => {
      cleaning.value = true
      try {
        const removeStatuses = Object.keys(statusCounts.value || {}).filter(
          (status) => !['unchecked', '', 'OK', 'PASS_CODE_EMPTY'].includes(status)
        )
        const ids = []
        for (const status of removeStatuses) {
          let page = 1
          while (true) {
            const j = await api.get('/api/admin/links', {
              status,
              page,
              size: 500,
              sort: 'id',
              dir: 'asc',
            })
            const items = j.items || []
            ids.push(...items.map((row) => row.ID))
            if (!items.length || page * 500 >= Number(j.total || 0)) break
            page += 1
          }
        }

        let deleted = 0
        for (let i = 0; i < ids.length; i += 500) {
          const j = await api.post('/api/admin/links/delete', {
            ids: ids.slice(i, i + 500),
          })
          deleted += Number(j.deleted || 0)
        }
        message.success(`已清理 ${deleted} 条`)
        await load(1)
      } catch (e) {
        message.error(e.message)
      } finally {
        cleaning.value = false
      }
    },
  })
}

async function doCopy(row) {
  try {
    await copyText(copyPayload(row))
    message.success('已复制')
  } catch {
    message.error('复制失败，手动选一下吧')
  }
}

/** 把这条链接直接推进秒传队列（提取码一起带上） */
async function sendToTransfer(row) {
  const url = String(row.LINK || '').trim()
  if (!url) {
    message.warning('这条链接没有地址')
    return
  }
  const code = row.CODE && row.CODE !== '无' ? String(row.CODE) : ''
  transferId.value = row.ID
  try {
    await api.post('/api/transfers/jobs', { share_url: url, pass_code: code })
    message.success('已加入秒传队列，去「秒传」页看进度')
  } catch (e) {
    message.error(e.message)
  } finally {
    transferId.value = 0
  }
}

function openSource(row) {
  sourceModal.row = row
  sourceModal.value = row.SOURCE || ''
  sourceModal.open = true
}

async function saveSource() {
  try {
    const j = await api.post('/api/admin/links/source', {
      ids: [sourceModal.row.ID],
      source: sourceModal.value || '',
    })
    message.success(`已更新 ${j.updated} 条`)
    sourceModal.open = false
    await load(0)
  } catch (e) {
    message.error(e.message)
  }
}

function onSearch() {
  load(1)
}

/** 表格列 key → 后端 SORT_COLUMNS 的字段名 */
const SORT_MAP = {
  ID: 'id',
  CODE: 'pass_code',
  TITLE: 'check_title',
  SOURCE: 'source',
  STATUS: 'check_status',
  FIRST_SEEN: 'first_seen_at',
  LAST_SEEN: 'last_seen_at',
  HIDDEN: 'hidden',
}

/**
 * 表头排序。后端 SORT_COLUMNS 支持 8 个字段，之前前端把 sort 写死成 id，
 * 其余能排的列一直是死的。
 */
function onSort(sorter) {
  const field = SORT_MAP[sorter?.columnKey]
  if (!sorter || !sorter.order || !field) {
    q.sort = 'id'
    q.dir = 'desc'
  } else {
    q.sort = field
    q.dir = sorter.order === 'ascend' ? 'asc' : 'desc'
  }
  load(1)
}

/** 当前排序列的箭头状态，交给 n-data-table 显示 */
function sortOrder(key) {
  return q.sort === SORT_MAP[key] ? (q.dir === 'asc' ? 'ascend' : 'descend') : false
}

function resetFilters() {
  q.text = ''
  q.status = ''
  q.source = ''
  q.hidden = ''
  q.withCode = ''
  q.size = 20
  q.sort = 'id'
  q.dir = 'desc'
  try {
    sessionStorage.removeItem(FILTER_KEY)
  } catch {
    /* 同 saveFilters */
  }
  onSearch()
}

/* ---------- 表格列 ---------- */
const columns = computed(() => {
  const checkingRow = checkingId.value
  const transferringRow = transferId.value
  return [
  { type: 'selection', fixed: 'left' },
  {
    title: 'ID',
    key: 'ID',
    width: 74,
    fixed: 'left',
    sorter: true,
    sortOrder: sortOrder('ID'),
  },
  {
    title: '链接',
    key: 'LINK',
    minWidth: 240,
    fixed: 'left',
    render: (row) =>
      h(
        'a',
        {
          href: row.LINK,
          target: '_blank',
          rel: 'noopener noreferrer',
          class: 'text-[var(--app-brand)] break-all hover:underline',
        },
        row.LINK
      ),
  },
  {
    title: '提取码',
    key: 'CODE',
    width: 92,
    sorter: true,
    sortOrder: sortOrder('CODE'),
    render: (row) => h('span', { class: 'mono' }, row.CODE),
  },
  {
    title: '文件名',
    key: 'TITLE',
    minWidth: 200,
    ellipsis: { tooltip: true },
    sorter: true,
    sortOrder: sortOrder('TITLE'),
    render: (row) => row.TITLE || '—',
  },
  {
    title: '文件大小',
    key: 'SIZE_BYTES',
    width: 104,
    align: 'right',
    render: (row) => h('span', { class: 'mono' }, fmtBytes(row.SIZE_BYTES)),
  },
  {
    title: '来源',
    key: 'SOURCE',
    width: 160,
    ellipsis: { tooltip: true },
    sorter: true,
    sortOrder: sortOrder('SOURCE'),
    render: (row) =>
      h(
        NButton,
        { text: true, type: 'primary', onClick: () => openSource(row) },
        { default: () => row.SOURCE || '（未知，点击补）' }
      ),
  },
  {
    title: '备注',
    key: 'MSG',
    minWidth: 150,
    ellipsis: { tooltip: true },
    render: (row) =>
      h('div', [
        h('div', row.MSG),
        row.CHAT ? h('div', { class: 'mono muted text-[12.5px]' }, row.CHAT) : null,
      ]),
  },
  {
    title: '状态',
    key: 'STATUS',
    width: 104,
    sorter: true,
    sortOrder: sortOrder('STATUS'),
    render: (row) =>
      h(NTag, { size: 'small', round: true, type: rowStatus(row).type }, {
        default: () => rowStatus(row).label,
      }),
  },
  {
    title: '检测时间',
    key: 'CHECKED_AT',
    width: 140,
    render: (row) => h('span', { class: 'muted text-[13px]' }, fmtTime(row.CHECKED_AT)),
  },
  {
    title: '入库时间',
    key: 'FIRST_SEEN',
    width: 140,
    sorter: true,
    sortOrder: sortOrder('FIRST_SEEN'),
    render: (row) => h('span', { class: 'muted text-[13px]' }, fmtTime(row.FIRST_SEEN)),
  },
  {
    title: '前台显示',
    key: 'HIDDEN',
    width: 96,
    align: 'center',
    fixed: 'right',
    sorter: true,
    sortOrder: sortOrder('HIDDEN'),
    render: (row) =>
      h(NSwitch, {
        value: !row.HIDDEN,
        size: 'small',
        onUpdateValue: (v) => setHidden(!v, row),
      }),
  },
  {
    title: '操作',
    key: 'actions',
    width: 285,
    fixed: 'right',
    render: (row) =>
      h('div', { class: 'flex gap-1' }, [
        h(
          NButton,
          {
            size: 'small',
            secondary: true,
            type: 'primary',
            loading: transferringRow === row.ID,
            onClick: () => sendToTransfer(row),
          },
          { default: () => '秒传' }
        ),
        h(
          NButton,
          {
            size: 'small',
            secondary: true,
            loading: checkingRow === row.ID,
            onClick: () => checkOne(row),
          },
          { default: () => '检测' }
        ),
        h(NButton, { size: 'small', secondary: true, onClick: () => doCopy(row) }, {
          default: () => '复制',
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

onMounted(() => {
  loadFilters()
  load(1)
})
</script>

<template>
  <div class="flex flex-col gap-3">
    <!-- ============ 工具栏 ============ -->
    <div class="toolbar">
      <div class="toolbar__row ctl-row">
        <n-input
          v-model:value="q.text"
          class="toolbar__search"
          clearable
          placeholder="搜索 链接 / 提取码 / 备注 / 文件名 / 来源"
          @keyup.enter="onSearch"
        >
          <template #prefix><Search :size="14" /></template>
        </n-input>
        <n-select
          class="toolbar__sel"
          :value="q.status"
          :options="statusOptions"
          @update:value="(v) => { q.status = v; onSearch() }"
        />
        <n-select
          class="toolbar__sel"
          :value="q.source"
          :options="sourceOptions"
          @update:value="(v) => { q.source = v; onSearch() }"
        />
        <n-select
          class="toolbar__sel"
          :value="q.hidden"
          :options="hiddenOptions"
          @update:value="(v) => { q.hidden = v; onSearch() }"
        />
        <n-select
          class="toolbar__sel"
          :value="q.withCode"
          :options="codeOptions"
          @update:value="(v) => { q.withCode = v; onSearch() }"
        />
        <n-select
          class="toolbar__sel toolbar__sel--sm"
          :value="q.size"
          :options="sizeOptions"
          @update:value="(v) => { q.size = v; onSearch() }"
        />
        <n-button type="primary" @click="onSearch">
          <template #icon><Search :size="15" /></template>
          查询
        </n-button>
        <n-button secondary @click="resetFilters">重置</n-button>
      </div>

      <div class="toolbar__row toolbar__row--sub ctl-row">
        <n-tag size="small" round>共 {{ total }} 条</n-tag>
        <n-tag v-if="hiddenCount" size="small" round type="warning">
          本页隐藏 {{ hiddenCount }}
        </n-tag>
        <n-button
          size="small"
          secondary
          :loading="checking"
          :disabled="!rows.length"
          @click="checkPage"
        >
          <template #icon><ScanSearch :size="15" /></template>
          检测本页
        </n-button>
        <n-button size="small" secondary @click="checkPending">
          <template #icon><RefreshCw :size="15" /></template>
          检测待检测（后台）
        </n-button>
        <n-button
          size="small"
          tertiary
          type="error"
          :loading="cleaning"
          :disabled="!removableCount"
          @click="cleanupUnwanted"
        >
          <template #icon><Trash2 :size="15" /></template>
          一键清理
        </n-button>
        <span class="ml-auto text-[12.5px] faint hidden lg:inline">
          「来源」= 这条链接第一次被采到的群；点表头可以按该列排序
        </span>
      </div>
    </div>

    <!-- ============ 批量操作条：勾选后才出现 ============ -->
    <Transition name="bulk">
      <div v-if="selectedRows.length" class="bulk">
        <span class="bulk__count">已选 <b>{{ selectedRows.length }}</b> 条</span>
        <span class="bulk__sep" aria-hidden="true" />
        <n-button size="small" secondary :loading="checking" @click="checkSelected">
          <template #icon><ScanSearch :size="15" /></template>
          检测
        </n-button>
        <n-button size="small" secondary @click="setHidden(true)">隐藏</n-button>
        <n-button size="small" secondary @click="setHidden(false)">取消隐藏</n-button>
        <n-button size="small" tertiary type="error" @click="remove()">删除</n-button>
        <n-button size="small" quaternary class="ml-auto" @click="selectedKeys = []">
          取消选择
        </n-button>
      </div>
    </Transition>

    <n-card size="small" :content-style="{ padding: '0' }">
      <n-alert v-if="error" type="error" :show-icon="true" class="m-3">{{ error }}</n-alert>
      <n-data-table
        size="small"
        :columns="columns"
        :data="rows"
        :loading="loading"
        :row-key="(row) => row.ID"
        :checked-row-keys="selectedKeys"
        :max-height="tableHeight"
        :scroll-x="1760"
        :bordered="false"
        :striped="true"
        @update:checked-row-keys="onSelectionChange"
        @update:sorter="onSort"
      />
      <div class="flex justify-center py-3 border-t border-[var(--app-line)]">
        <n-pagination
          :page="q.page"
          :page-size="q.size"
          :item-count="total"
          :page-slot="7"
          @update:page="load"
        />
      </div>
    </n-card>

    <!-- 修改来源 -->
    <n-modal
      v-model:show="sourceModal.open"
      preset="card"
      title="修改来源"
      style="width: min(480px, 94vw)"
      :mask-closable="false"
    >
      <div class="text-[13px] muted mb-2">
        给这条链接填一个来源（现在：{{ sourceModal.row?.SOURCE || '空' }}）
      </div>
      <n-input v-model:value="sourceModal.value" placeholder="群名或来源备注" />
      <template #footer>
        <div class="flex justify-end gap-2">
          <n-button @click="sourceModal.open = false">取消</n-button>
          <n-button type="primary" @click="saveSource">保存</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
/* ==========================================================================
   工具栏：一行放完所有筛选，比原来的两行网格省一半纵向空间
   ========================================================================== */
.toolbar {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 12px 14px;
  border: 1px solid var(--app-line);
  border-radius: 16px;
  background: var(--app-panel);
}

.toolbar__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.toolbar__row--sub {
  /* 次级工具栏整行小一档：Naive 的 small 是 28px，
     而 Tag 默认只有 22px，不加这个的话标签和按钮差 6px。 */
  --control-h: 28px;
  padding-top: 9px;
  border-top: 1px solid var(--app-line);
}

.toolbar__search {
  flex: 1 1 260px;
  min-width: 200px;
}

.toolbar__sel {
  flex: 0 1 168px;
  min-width: 130px;
}

.toolbar__sel--sm {
  flex: 0 0 118px;
  min-width: 108px;
}

/* ==========================================================================
   批量操作条：勾选后才出现，滑下来而不是把下面的内容顶走
   ========================================================================== */
.bulk {
  position: sticky;
  top: calc(var(--top-h) + 8px);
  z-index: 12;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  border: 1px solid color-mix(in srgb, var(--app-brand) 34%, var(--app-line));
  border-radius: 14px;
  background: color-mix(in srgb, var(--app-brand) 7%, var(--app-bg));
  backdrop-filter: blur(12px);
  box-shadow: var(--shadow-2);
}

.bulk__count {
  color: var(--app-muted);
  font-size: 13px;
}

.bulk__count b {
  color: var(--app-text);
  font-size: 15px;
  font-variant-numeric: tabular-nums;
}

.bulk__sep {
  width: 1px;
  height: 20px;
  background: color-mix(in srgb, var(--app-brand) 26%, var(--app-line));
}

.bulk-enter-active,
.bulk-leave-active {
  transition: opacity var(--dur-fast) ease, transform var(--dur) var(--ease-out);
}

.bulk-enter-from,
.bulk-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}

@media (max-width: 760px) {
  .toolbar__sel,
  .toolbar__sel--sm {
    flex: 1 1 130px;
  }

  .bulk {
    position: static;
  }
}

@media (prefers-reduced-motion: reduce) {
  .bulk-enter-active,
  .bulk-leave-active {
    transition: none;
  }
}
</style>
