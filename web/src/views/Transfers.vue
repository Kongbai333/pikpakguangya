<script setup>
/**
 * 秒传到光鸭：粘贴 PikPak 分享链接 → 光鸭秒传。
 * 页面 2 秒轮询一次，当前任务显示实时进度条和正在秒传的文件。
 */
import { onMounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import {
  ChevronDown,
  CloudUpload,
  Download,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-vue-next'

import * as api from '../api'
import TransferItems from '../components/TransferItems.vue'
import { copyText, fmtBytes, fmtTime } from '../format'
import { usePolling } from '../polling'

const message = useMessage()

const newModal = reactive({ open: false, text: '' })
const creating = ref(false)
const activeJobs = ref([])
const finishedJobs = ref([])
const historyPage = ref(1)
const historySize = ref(20)
const historyTotal = ref(0)
const detail = reactive({ id: 0, job: null, items: [], selection: {} })
const selectBusy = ref(false)
// 用户手动收起记录后，轮询不要再自动展开
const collapsed = ref(false)
const loadingDetail = ref(false)
const refreshing = ref(false)
const guangya = reactive({
  connected: false,
  loaded: false,
  root_folder_id: '',
  root_folder_path: '/',
})
const JOB_STATUS = {
  queued: { label: '排队中', type: 'default' },
  running: { label: '秒传中', type: 'info' },
  paused: { label: '已暂停', type: 'warning' },
  finished: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'error' },
  canceled: { label: '已终止', type: 'default' },
}
const jobStatus = (job) => JOB_STATUS[job?.status] || { label: job?.status || '未知', type: 'default' }
const jobTitle = (job) => job?.title || job?.share_url || `任务 #${job?.id}`
/** 结果标签的颜色：有失败就红、有未命中就黄、全成功才绿 */
const resultTagType = (job) => {
  if (job?.status === 'failed' || job?.failed_files) return 'error'
  if (job?.status === 'canceled' || job?.skipped_files) return 'warning'
  return 'success'
}
const pendingCount = (job) => Math.max(
  0,
  Number(job?.total_files || 0)
    - Number(job?.completed_files || 0)
    - Number(job?.failed_files || 0)
    - Number(job?.skipped_files || 0),
)
const actionBusy = ref('')

async function loadStatus() {
  try {
    // 只查本地登录态，不带容量，避免慢请求期间误显示「未登录」
    const data = await api.get('/api/integrations/status', { storage: false })
    Object.assign(guangya, data.guangya || {}, { loaded: true })
  } catch {
    // 状态拿不到时不影响任务列表，页面其它地方会提示未登录
  }
}

async function loadJobs() {
  const data = await api.get('/api/transfers/jobs', {
    page: historyPage.value,
    size: historySize.value,
  })
  activeJobs.value = data.active_jobs || []
  finishedJobs.value = data.jobs || []
  historyTotal.value = data.total || 0
  const pages = Math.max(1, Number(data.pages) || 1)
  if (historyPage.value > pages) {
    historyPage.value = pages
    await loadJobs()
  }
}

let detailSeq = 0
let detailLoadedAt = 0
let detailSignature = ''

/**
 * 明细的「指纹」：id + 状态 + 勾选。
 * 轮询每 5 秒拉一次，但绝大多数时候内容没变 —— 直接换掉整个数组会让
 * Vue 重渲染几百行，展开明细时看着就是一顿一顿的。
 */
function itemsSignature(items) {
  let out = ''
  for (const item of items) {
    out += `${item.id}:${item.status}:${item.selected === false ? 0 : 1};`
  }
  return out
}

async function loadDetail(jobId, notifyError = false) {
  if (!jobId) {
    detail.id = 0
    detail.job = null
    detail.items = []
    detail.selection = {}
    detailSignature = ''
    return
  }
  const seq = ++detailSeq
  // 轮询刷新不闪加载态，只有切换任务时才显示
  if (notifyError || detail.id !== jobId) loadingDetail.value = true
  try {
    const data = await api.get(`/api/transfers/jobs/${jobId}`, { items_limit: 300 })
    if (seq !== detailSeq) return   // 期间被收起或切走了，这次结果丢掉
    const items = data.items || []
    const signature = itemsSignature(items)
    detail.id = jobId
    detail.job = data.job || null
    detail.selection = data.selection || {}
    // 内容没变就不碰 items，避免整列表重绘
    if (signature !== detailSignature) {
      detail.items = items
      detailSignature = signature
    }
    detailLoadedAt = Date.now()
  } catch (e) {
    if (notifyError) message.error(e.message)
  } finally {
    loadingDetail.value = false
  }
}

/**
 * 勾选 / 取消勾选要秒传的文件。
 *
 * 两种 payload：
 * - {ids: [...], selected} —— 页面上逐条勾选
 * - {selected, max_size / min_size} —— 后端批量，不受「只显示前 300 条」限制
 */
async function selectItems(payload) {
  if (!detail.id) return
  selectBusy.value = true
  try {
    const data = await api.post(`/api/transfers/jobs/${detail.id}/items/select`, payload)
    detail.selection = data.selection || detail.selection
    // 本地同步勾选态：等下一次轮询会有延迟，先把点过的行改掉
    if (payload.ids?.length) {
      const wanted = payload.selected
      for (const item of detail.items) {
        if (payload.ids.includes(item.id)) item.selected = wanted
      }
      // 指纹跟着更新，否则下一轮轮询会以为「内容变了」把整列表重绘一遍
      detailSignature = itemsSignature(detail.items)
    } else {
      await loadDetail(detail.id)
    }
    if (payload.ids?.length) {
      message.success(payload.selected ? `已勾选 ${data.updated} 项` : `已取消 ${data.updated} 项`)
    } else {
      message.success(`已更新 ${data.updated} 项`)
    }
  } catch (e) {
    message.error(e.message)
  } finally {
    selectBusy.value = false
  }
}

async function changeHistoryPage(page) {
  historyPage.value = Number(page) || 1
  if (detail.id && !activeJobs.value.some((job) => job.id === detail.id)) {
    detailSeq += 1
    detail.id = 0
    detail.job = null
    detail.items = []
    detail.selection = {}
    detailSignature = ''
    collapsed.value = true
  }
  await loadJobs()
}

async function refresh(notifyError = false, force = false) {
  try {
    await loadJobs()
  } catch (e) {
    if (notifyError) message.error(e.message)
    return
  }
  const ids = [...activeJobs.value, ...finishedJobs.value].map((job) => job.id)
  if (!detail.id || !ids.includes(detail.id)) {
    detail.id = 0
    detail.job = null
    detail.items = []
    detail.selection = {}
    detailSignature = ''
    collapsed.value = true
    return
  }
  if (collapsed.value) return   // 收起时不刷明细，也不自动展开
  // 正在改勾选时不要拉明细：轮询的结果可能比刚才的改动旧，会把界面闪回去
  if (selectBusy.value) return
  const current = [...activeJobs.value, ...finishedJobs.value]
    .find((job) => job.id === detail.id)
  const terminal = current && !['running', 'queued', 'paused'].includes(current.status)
  // 终态只补刷一次，避免已完成的任务仍每 2 秒重拉上千条明细
  if (terminal && detail.job?.status === current.status) return
  // 运行中明细没摘要那么敏感，5 秒刷新一次即可，别让浏览器反复重绘几百行
  if (!force && !terminal && Date.now() - detailLoadedAt < 5000) return
  await loadDetail(detail.id)
}

async function manualRefresh() {
  refreshing.value = true
  await refresh(true, true)
  refreshing.value = false
}

function openNewModal() {
  newModal.text = ''
  newModal.open = true
}

async function submitNewJobs() {
  const text = newModal.text.trim()
  if (!text) {
    message.warning('先粘贴 PikPak 分享链接，一行一个')
    return
  }
  if (!guangya.loaded) await loadStatus()
  if (!guangya.connected) {
    message.warning('请先在设置里登录光鸭账号')
    return
  }
  creating.value = true
  try {
    const data = await api.post('/api/transfers/jobs', { text })
    newModal.open = false
    newModal.text = ''
    const first = (data.jobs || [])[0]
    if (first?.id) {
      collapsed.value = false
      detail.id = first.id
      await loadDetail(first.id)
    }
    await loadJobs()
    const count = (data.jobs || []).length
    message.success(`已加入秒传队列 ${count} 条`)
    if (data.errors?.length) {
      message.warning(`有 ${data.errors.length} 条没解析出来：${data.errors[0]}`)
    }
  } catch (e) {
    message.error(e.message)
  } finally {
    creating.value = false
  }
}

function selectJob(job) {
  if (detail.id === job.id) {
    collapsed.value = !collapsed.value
    if (collapsed.value) return
  } else {
    detail.id = job.id
    collapsed.value = false
  }
  loadDetail(job.id, true)
}

async function deleteJob(job) {
  try {
    await api.del(`/api/transfers/jobs/${job.id}`)
    if (detail.id === job.id) {
      detailSeq += 1
      detail.id = 0
      detail.job = null
      detail.items = []
      detail.selection = {}
      detailSignature = ''
      collapsed.value = true
    }
    await loadJobs()
    message.success('记录已删除')
  } catch (e) {
    message.error(e.message)
  }
}

function exportJson(job) {
  // 后端把解析结果导成 JSON 附件，浏览器同源直接下；没登录会被 401 挡掉
  window.open(`/api/transfers/jobs/${job.id}/export`, '_blank')
}

async function copyShareUrl(job) {
  try {
    await copyText(job?.share_url || '')
    message.success('分享链接已复制')
  } catch (e) {
    message.error(`复制失败：${e.message}`)
  }
}

async function jobAction(job, action) {
  actionBusy.value = `${job.id}:${action}`
  try {
    const data = await api.post(`/api/transfers/jobs/${job.id}/action`, { action })
    await loadJobs()
    if (data.job?.id && detail.id === data.job.id) await loadDetail(data.job.id)
  } catch (e) {
    message.error(e.message)
  } finally {
    actionBusy.value = ''
  }
}

onMounted(async () => {
  await loadStatus()
  await refresh(true)
})
// 秒传进度变化快，2 秒一轮；标签页在后台时整轮跳过
usePolling(() => refresh(false), 2000)
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- 新建任务 -->
    <n-card size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <CloudUpload :size="16" class="text-[var(--app-brand)]" />
          <span class="font-semibold">新建秒传任务</span>
          <n-tag
            class="ml-2"
            size="small"
            round
            :type="!guangya.loaded ? 'default' : guangya.connected ? 'success' : 'warning'"
          >
            {{ !guangya.loaded ? '检测登录态…' : guangya.connected ? '光鸭已登录' : '光鸭未登录' }}
          </n-tag>
        </div>
      </template>

      <p class="text-[13.5px] muted mt-0 mb-3">
        粘贴 PikPak 分享链接（提取码写在链接里也行），按文件 GCID 秒传到光鸭
        「设置」里选好的目录。命中资源库的文件直接入库，未命中的只记录，不做普通上传。
      </p>

      <n-button type="primary" @click="openNewModal">
        <template #icon><Plus :size="16" /></template>
        新建秒传
      </n-button>
    </n-card>

    <!-- 正在秒传 -->
    <n-card v-if="activeJobs.length" size="small">
      <template #header>
        <div class="flex items-center gap-2">
          <span class="font-semibold">正在秒传</span>
          <n-tag size="small" round type="info">{{ activeJobs.length }} 个任务</n-tag>
        </div>
      </template>

      <div
        v-for="(job, index) in activeJobs"
        :key="job.id"
        class="job-active"
        :class="{ 'mt-4': index }"
      >
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="min-w-0">
            <n-tag size="small" round :type="jobStatus(job).type">
              {{ jobStatus(job).label }}
            </n-tag>
            <span class="ml-2 text-[13px] font-medium">{{ jobTitle(job) }}</span>
            <span class="ml-2 text-[12.5px] muted">→ {{ job.target_folder_path || '/' }}</span>
            <n-button size="tiny" quaternary class="ml-2" @click="selectJob(job)">
              {{ detail.id === job.id && !collapsed ? '收起明细' : '查看明细' }}
            </n-button>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <n-tag size="small" round :bordered="false" type="success">
              成功 {{ job.completed_files }}
            </n-tag>
            <n-tag size="small" round :bordered="false" type="warning">
              未命中 {{ job.skipped_files }}
            </n-tag>
            <n-tag
              size="small"
              round
              :bordered="false"
              :type="job.failed_files ? 'error' : 'default'"
            >
              失败 {{ job.failed_files }}
            </n-tag>
            <n-tag size="small" round :bordered="false">
              共 {{ job.total_files || '…' }}
            </n-tag>
            <n-tag v-if="job.total_bytes" size="small" round :bordered="false">
              {{ fmtBytes(job.completed_bytes) }} / {{ fmtBytes(job.total_bytes) }}
            </n-tag>
            <n-button
              v-if="job.status === 'running' || job.status === 'queued'"
              size="tiny"
              secondary
              :loading="actionBusy === `${job.id}:pause`"
              @click="jobAction(job, 'pause')"
            >
              暂停
            </n-button>
            <n-button
              v-if="job.status === 'paused'"
              size="tiny"
              secondary
              :loading="actionBusy === `${job.id}:resume`"
              @click="jobAction(job, 'resume')"
            >
              继续
            </n-button>
            <n-button
              size="tiny"
              secondary
              type="error"
              :loading="actionBusy === `${job.id}:cancel`"
              @click="jobAction(job, 'cancel')"
            >
              终止
            </n-button>
          </div>
        </div>

        <div class="progress-row mt-3">
          <div class="xbar">
            <div
              class="xbar-fill"
              :class="{ idle: job.status !== 'running' }"
              :style="{ width: `${job.progress || 0}%` }"
            />
          </div>
          <span class="w-[42px] shrink-0 text-right text-[12px] muted">
            {{ job.progress || 0 }}%
          </span>
        </div>

        <div class="mt-2 flex flex-wrap items-center justify-between gap-2 text-[12.5px]">
          <span class="min-w-0 flex-1 truncate" :title="job.current_file">
            {{ job.message || '等待开始…' }}
            <span v-if="job.current_file" class="mono">{{ job.current_file }}</span>
          </span>
          <span class="muted shrink-0">{{ fmtTime(job.created_at) }}</span>
        </div>

        <div v-if="detail.id === job.id && !collapsed" class="mt-3">
          <div v-if="loadingDetail && !detail.items.length" class="detail-hint">
            正在载入明细…
          </div>
          <TransferItems
            :items="detail.items"
            :total="Number(detail.job?.total_files || 0)"
            :job-id="detail.id"
            :selection="detail.selection"
            :busy="selectBusy"
            @select="selectItems"
          />
        </div>
      </div>
    </n-card>

    <!-- 历史记录 -->
    <n-card size="small">
      <template #header>
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-semibold">秒传记录</span>
          <n-tag v-if="historyTotal" class="ml-2" size="small" round>
            {{ historyTotal }}
          </n-tag>
          <!-- 翻页放在头部：展开某个任务的明细后列表会变得很长，
               分页留在底部就得先滚到底才能点下一页。 -->
          <n-pagination
            v-if="historyTotal > historySize"
            class="ml-3"
            size="small"
            :page="historyPage"
            :page-size="historySize"
            :item-count="historyTotal"
            :page-slot="5"
            @update:page="changeHistoryPage"
          />
          <n-button
            class="ml-auto"
            size="tiny"
            quaternary
            :loading="refreshing"
            @click="manualRefresh"
          >
            <template #icon><RefreshCw :size="14" /></template>
            刷新
          </n-button>
        </div>
      </template>

      <div
        v-if="!finishedJobs.length"
        class="grid min-h-[120px] place-items-center text-[13px] muted"
      >
        还没有秒传记录，任务完成后会显示在这里
      </div>

      <div v-else class="flex flex-col gap-2">
        <div
          v-for="job in finishedJobs"
          :key="job.id"
          class="job-row"
          :class="{ open: detail.id === job.id && !collapsed }"
        >
          <button type="button" class="job-head" @click="selectJob(job)">
            <n-tag size="small" round :type="jobStatus(job).type">
              {{ jobStatus(job).label }}
            </n-tag>
            <span class="min-w-0 flex-1 truncate text-[13px]" :title="jobTitle(job)">
              {{ jobTitle(job) }}
            </span>
            <span class="hidden text-[12.5px] muted sm:inline">
              {{ fmtTime(job.finished_at || job.created_at) }}
            </span>
            <n-tag size="small" round :bordered="false" :type="resultTagType(job)">
              {{ job.completed_files }} / {{ job.total_files || '…' }}
            </n-tag>
            <ChevronDown
              :size="15"
              class="chev text-[var(--app-faint)]"
              :class="{ flip: detail.id === job.id && !collapsed }"
            />
          </button>

          <div class="job-bar" aria-hidden="true">
            <span :style="{ width: `${job.progress || 0}%` }" />
          </div>

          <div v-if="detail.id === job.id && !collapsed" class="job-body">
            <!-- 信息区：短信息用小标签，长链接单独一行 -->
            <div class="mb-2 flex flex-wrap items-center gap-2">
              <n-tag size="small" round :bordered="false" type="success">
                成功 {{ job.completed_files }}
              </n-tag>
              <n-tag size="small" round :bordered="false" type="warning">
                未命中 {{ job.skipped_files }}
              </n-tag>
              <n-tag
                size="small"
                round
                :bordered="false"
                :type="job.failed_files ? 'error' : 'default'"
              >
                失败 {{ job.failed_files }}
              </n-tag>
              <n-tag v-if="job.total_bytes" size="small" round :bordered="false">
                总大小 {{ fmtBytes(job.total_bytes) }}
              </n-tag>
              <n-tag v-if="job.total_bytes" size="small" round :bordered="false" type="info">
                已秒传 {{ fmtBytes(job.completed_bytes) }}
              </n-tag>
              <n-tag v-if="job.pass_code" size="small" round :bordered="false" type="warning">
                提取码 {{ job.pass_code }}
              </n-tag>
              <n-tag size="small" round :bordered="false">目录 {{ job.target_folder_path || '/' }}</n-tag>
            </div>

            <div class="mb-2 flex flex-wrap items-center gap-2">
              <n-tag
                size="small"
                round
                :bordered="false"
                class="link-tag max-w-[420px]"
                title="点击复制分享链接"
                @click="copyShareUrl(job)"
              >
                <span class="block max-w-[390px] truncate">{{ job.share_url }}</span>
              </n-tag>
              <n-tag size="small" round :bordered="false" :type="resultTagType(job)">
                {{ job.message }}
              </n-tag>
            </div>

            <!-- 操作按钮 -->
            <div class="mb-3 flex flex-wrap items-center gap-2">
                <n-button
                  v-if="job.failed_files || job.skipped_files || pendingCount(job) || job.status === 'failed' || job.status === 'canceled'"
                  size="small"
                  secondary
                  type="primary"
                  :loading="actionBusy === `${job.id}:retry`"
                  @click="jobAction(job, 'retry')"
                >
                  <template #icon><RefreshCw :size="14" /></template>
                  {{ pendingCount(job) ? '继续未处理' : '重试失败/未命中' }}
                </n-button>
                <n-button size="small" secondary @click="exportJson(job)">
                  <template #icon><Download :size="14" /></template>
                  导出 JSON
                </n-button>
                <n-popconfirm @positive-click="deleteJob(job)">
                  <template #trigger>
                    <n-button size="small" secondary type="error">
                      <template #icon><Trash2 :size="14" /></template>
                      删除记录
                    </n-button>
                  </template>
                  删除这条秒传记录？只删本地记录和明细，光鸭里的文件不动。
                </n-popconfirm>
            </div>

            <!-- 明细：不用 n-spin 包 —— 它会给整个子树加过渡，
                 几百行的情况下切任务时就是肉眼可见的一顿。 -->
            <div v-if="loadingDetail && !detail.items.length" class="detail-hint">
              正在载入明细…
            </div>
            <TransferItems
              :items="detail.items"
              :total="Number(detail.job?.total_files || 0)"
              :job-id="detail.id"
              :selection="detail.selection"
              :busy="selectBusy"
              @select="selectItems"
            />
          </div>
        </div>
      </div>
    </n-card>

    <!-- 新建秒传：一行一个链接 -->
    <n-modal
      v-model:show="newModal.open"
      preset="card"
      title="新建秒传任务"
      style="width: min(680px, 94vw)"
      :mask-closable="!creating"
    >
      <n-input
        v-model:value="newModal.text"
        type="textarea"
        :rows="9"
        placeholder="每行一个 PikPak 分享链接，提取码可以直接写在链接后面：&#10;https://mypikpak.com/s/xxxxx 提取码: ab12&#10;https://mypikpak.com/s/yyyyy"
      />

      <template #footer>
        <div class="flex items-center justify-between gap-3">
          <span class="text-[12.5px] muted">一行一个，最多 50 条；目录用设置里选好的那个</span>
          <div class="flex gap-2">
            <n-button :disabled="creating" @click="newModal.open = false">取消</n-button>
            <n-button type="primary" :loading="creating" @click="submitNewJobs">
              开始秒传
            </n-button>
          </div>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.job-active {
  padding: 12px 14px;
  border: 1px solid var(--app-line);
  border-radius: 12px;
  background: var(--app-panel-2);
}

.progress-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.xbar {
  position: relative;
  flex: 1;
  height: 12px;
  overflow: hidden;
  border-radius: 999px;
  background: color-mix(in srgb, var(--app-line) 55%, transparent);
}

.xbar-fill {
  position: relative;
  height: 100%;
  min-width: 2px;
  overflow: hidden;
  border-radius: inherit;
  background: linear-gradient(90deg, #12a594, #3f8cff 42%, #a855f7 72%, #f59e0b);
  transition: width 0.35s ease;
}

.xbar-fill::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    110deg,
    transparent 25%,
    rgba(255, 255, 255, 0.45) 50%,
    transparent 75%
  );
  background-size: 220% 100%;
  animation: xbar-shine 1.4s linear infinite;
}

.xbar-fill.idle::after {
  opacity: 0;
  animation: none;
}

@keyframes xbar-shine {
  from {
    background-position: 180% 0;
  }

  to {
    background-position: -80% 0;
  }
}

.job-row {
  border: 1px solid var(--app-line);
  border-radius: 12px;
  overflow: hidden;
}

.job-row.open {
  background: color-mix(in srgb, var(--app-panel-2) 70%, transparent);
}

.job-head {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: transparent;
  border: 0;
  color: var(--app-text);
  cursor: pointer;
  text-align: left;
}

.job-head:hover {
  background: var(--app-panel-2);
}

.job-bar {
  height: 4px;
  background: color-mix(in srgb, var(--app-line) 60%, transparent);
}

.job-bar span {
  display: block;
  height: 100%;
  border-radius: 0 999px 999px 0;
  background: linear-gradient(90deg, #12a594, #3f8cff 42%, #a855f7 72%, #f59e0b);
  transition: width 0.35s ease;
}

.chev {
  transition: transform 0.18s ease;
}

.chev.flip {
  transform: rotate(180deg);
}

.job-body {
  /* 上面就是那条彩色进度线，留一点呼吸空间 */
  padding: 12px 12px 12px;
}

.link-tag {
  cursor: pointer;
  transition: filter 0.15s ease;
}

.link-tag:hover {
  filter: brightness(0.95);
}

.detail-hint {
  padding: 22px 12px;
  text-align: center;
  color: var(--app-faint);
  font-size: 13px;
}

@media (prefers-reduced-motion: reduce) {
  .chev,
  .xbar-fill,
  .job-bar span {
    transition: none;
  }

  .link-tag {
    transition: none;
  }

  .xbar-fill::after {
    animation: none;
  }
}
</style>
