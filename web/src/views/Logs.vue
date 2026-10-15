<script setup>
/** 统一日志：每个账号的采集日志 + 服务进程日志。 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { Copy, Pause, Play, RefreshCw, Search } from 'lucide-vue-next'

import * as api from '../api'
import { copyText } from '../format'
import { usePolling } from '../polling'

const message = useMessage()

const loading = ref(false)
const active = ref('')
const sources = ref([])
const generatedAt = ref('')
const query = ref('')
const paused = ref(false)
const stickBottom = ref(true)
const scrollEl = ref(null)

const current = computed(
  () => sources.value.find((item) => item.key === active.value) || sources.value[0] || null
)

function shortTime(value) {
  const hit = String(value || '').match(/\d{2}:\d{2}:\d{2}(?:[,.]\d+)?/)
  return hit ? hit[0].replace(',', '.').slice(0, 12) : value || ''
}

function parseLine(raw, index) {
  const line = String(raw || '')
  if (!line.trim()) return { index: index + 1, raw: line, kind: 'blank', body: '' }
  if (line.trim().startsWith('=====')) {
    return { index: index + 1, raw: line, kind: 'section', body: line.trim() }
  }

  let match = line.match(
    /^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?)\s+(INFO|WARNING|WARN|ERROR|DEBUG|CRITICAL)\s+(.*)$/i
  )
  if (match) {
    return {
      index: index + 1,
      raw: line,
      kind: 'log',
      time: shortTime(match[1]),
      level: match[2].toUpperCase(),
      body: match[3],
    }
  }

  match = line.match(/^(\d{2}:\d{2}:\d{2}(?:[,.]\d+)?)\s+(.*)$/)
  if (match) {
    return {
      index: index + 1,
      raw: line,
      kind: 'log',
      time: shortTime(match[1]),
      level: '',
      body: match[2],
    }
  }

  return { index: index + 1, raw: line, kind: 'log', time: '', level: '', body: line }
}

const allLines = computed(() => {
  const text = current.value?.text || ''
  return text ? text.split(/\r?\n/).map(parseLine) : []
})

const lines = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return allLines.value
  return allLines.value.filter((line) => line.raw.toLowerCase().includes(q))
})

async function load(showLoading = false) {
  if (paused.value && !showLoading) return
  if (showLoading || !sources.value.length) loading.value = true
  try {
    const j = await api.get('/api/logs', { lines: 600 })
    sources.value = j.sources || []
    generatedAt.value = j.generated_at || ''
    if (!sources.value.some((source) => source.key === active.value)) {
      active.value = sources.value[0]?.key || ''
    }
    await nextTick()
    if (stickBottom.value) scrollToBottom()
  } catch (e) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

function selectSource(key) {
  active.value = key
  stickBottom.value = true
  nextTick(scrollToBottom)
}

function scrollToBottom() {
  const el = scrollEl.value
  if (el) el.scrollTop = el.scrollHeight
}

function onScroll() {
  const el = scrollEl.value
  if (!el) return
  stickBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 36
}

async function copyCurrent() {
  if (!current.value?.text) {
    message.warning('当前没有可复制的日志')
    return
  }
  try {
    await copyText(current.value.text)
    message.success('日志已复制')
  } catch {
    message.error('复制失败')
  }
}

function timeText(value) {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime())
    ? value
    : d.toLocaleString('zh-CN', { hour12: false })
}

onMounted(() => load(true))
// 标签页在后台时不刷日志，切回来立刻补一次
usePolling(() => load(false), 5000)
</script>

<template>
  <div class="log-page">
    <n-card size="small">
      <div class="log-toolbar">
        <div class="source-strip">
          <button
            v-for="source in sources"
            :key="source.key"
            type="button"
            class="source-pill"
            :class="{ on: source.key === current?.key }"
            @click="selectSource(source.key)"
          >
            <span class="source-dot" />
            {{ source.name }}
          </button>
        </div>

        <div class="log-actions">
          <n-input
            v-model:value="query"
            class="log-search"
            size="small"
            clearable
            placeholder="筛选日志"
          >
            <template #prefix><Search :size="14" /></template>
          </n-input>
          <n-button
            size="small"
            quaternary
            :title="paused ? '继续自动刷新' : '暂停自动刷新'"
            @click="paused = !paused"
          >
            <template #icon>
              <component :is="paused ? Play : Pause" :size="15" />
            </template>
            {{ paused ? '继续' : '暂停' }}
          </n-button>
          <n-button size="small" quaternary @click="copyCurrent">
            <template #icon><Copy :size="15" /></template>
            复制
          </n-button>
          <n-button size="small" secondary :loading="loading" @click="load(true)">
            <template #icon><RefreshCw :size="15" /></template>
            刷新
          </n-button>
        </div>
      </div>

      <div class="log-meta">
        <span class="current-source">{{ current?.name || '暂无日志源' }}</span>
        <span>{{ allLines.length }} 行</span>
        <span v-if="query.trim()">{{ lines.length }} 条匹配</span>
        <span>更新 {{ timeText(generatedAt) }}</span>
        <span class="auto-state" :class="{ paused }">
          {{ paused ? '自动刷新已暂停' : '每 5 秒自动刷新' }}
        </span>
      </div>

      <div class="log-panel">
        <n-spin :show="loading && !allLines.length">
          <div v-if="!sources.length && !loading" class="log-empty">
            <div class="empty-title">还没有日志源</div>
            <div class="empty-desc">账号开始采集或服务产生日志后，会在这里显示。</div>
          </div>
          <div v-else-if="!allLines.length && !loading" class="log-empty">
            <div class="empty-title">当前日志为空</div>
            <div class="empty-desc">等待下一条采集或服务日志。</div>
          </div>
          <div v-else-if="!lines.length" class="log-empty">
            <div class="empty-title">没有匹配内容</div>
            <div class="empty-desc">换一个关键词，或清空筛选条件。</div>
          </div>
          <div v-else ref="scrollEl" class="log-scroll" @scroll="onScroll">
            <div
              v-for="line in lines"
              :key="line.index"
              class="log-line"
              :class="[line.kind, line.level?.toLowerCase()]"
            >
              <template v-if="line.kind === 'section'">
                <span class="section-text">{{ line.body }}</span>
              </template>
              <template v-else>
                <span class="line-no">{{ line.index }}</span>
                <span class="line-time">{{ line.time || '—' }}</span>
                <span v-if="line.level" class="line-level">{{ line.level }}</span>
                <span v-else />
                <span class="line-text">{{ line.body }}</span>
              </template>
            </div>
          </div>
        </n-spin>
      </div>

      <div v-if="current?.path" class="log-path" :title="current.path">
        {{ current.path }}
      </div>
    </n-card>
  </div>
</template>

<style scoped>
.log-page {
  min-width: 0;
}

.log-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.source-strip {
  display: flex;
  min-width: 0;
  flex: 1;
  gap: 7px;
  overflow-x: auto;
  padding-bottom: 2px;
}

.source-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 11px;
  border: 0;
  border-radius: 999px;
  color: var(--app-muted);
  background: var(--app-panel-2);
  font-size: 12.5px;
  white-space: nowrap;
  cursor: pointer;
  transition:
    color 0.16s ease,
    background 0.16s ease,
    transform 0.16s ease;
}

.source-pill:hover {
  color: var(--app-text);
}

.source-pill:active {
  transform: translateY(1px);
}

.source-pill.on {
  color: var(--app-bg);
  background: var(--app-text);
}

.source-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  opacity: 0.72;
}

.log-actions {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-left: auto;
}

.log-search {
  width: 220px;
}

.log-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
  margin-top: 13px;
  color: var(--app-faint);
  font-size: 12.5px;
}

.current-source {
  color: var(--app-text);
  font-weight: 600;
}

.auto-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.auto-state::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--app-ok);
}

.auto-state.paused::before {
  background: var(--app-warn);
}

.log-panel {
  min-height: 230px;
  margin-top: 11px;
  overflow: hidden;
  border: 1px solid var(--app-line);
  border-radius: 14px;
  background: var(--app-panel);
}

.log-scroll {
  max-height: min(62vh, 620px);
  overflow: auto;
  padding: 4px 0;
}

.log-line {
  display: grid;
  grid-template-columns: 48px 78px 58px minmax(0, 1fr);
  align-items: start;
  min-height: 30px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--app-line-soft);
  color: var(--app-text);
  font-family: ui-monospace, SFMono-Regular, Consolas, 'Liberation Mono', monospace;
  font-size: 12.5px;
  line-height: 1.5;
}

.log-line:last-child {
  border-bottom: 0;
}

.log-line:hover {
  background: color-mix(in srgb, var(--app-panel-2) 62%, transparent);
}

.line-no,
.line-time {
  color: var(--app-faint);
  font-variant-numeric: tabular-nums;
}

.line-level {
  width: fit-content;
  min-width: 38px;
  padding: 0 5px;
  border-radius: 5px;
  color: var(--app-muted);
  background: var(--app-panel-2);
  font-size: 10.5px;
  font-weight: 700;
  line-height: 18px;
  text-align: center;
}

.log-line.error .line-level {
  color: var(--app-danger);
  background: color-mix(in srgb, var(--app-danger) 10%, transparent);
}

.log-line.warning .line-level,
.log-line.warn .line-level {
  color: var(--app-warn);
  background: color-mix(in srgb, var(--app-warn) 12%, transparent);
}

.log-line.info .line-level {
  color: #0284c7;
  background: rgba(14, 165, 233, 0.1);
}

.line-text,
.section-text {
  min-width: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.log-line.section {
  display: block;
  padding: 7px 12px;
  color: var(--app-muted);
  background: var(--app-panel-2);
  font-weight: 600;
}

.log-line.blank {
  min-height: 12px;
  padding: 3px 12px;
  border-bottom: 0;
}

.log-empty {
  display: grid;
  min-height: 230px;
  place-content: center;
  padding: 24px;
  text-align: center;
}

.empty-title {
  color: var(--app-text);
  font-size: 14px;
  font-weight: 600;
}

.empty-desc {
  margin-top: 6px;
  color: var(--app-faint);
  font-size: 12.5px;
}

.log-path {
  margin-top: 9px;
  color: var(--app-faint);
  font-family: ui-monospace, SFMono-Regular, Consolas, 'Liberation Mono', monospace;
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

@media (max-width: 760px) {
  .log-actions {
    width: 100%;
    margin-left: 0;
  }

  .log-search {
    min-width: 0;
    flex: 1;
    width: auto;
  }

  .log-line {
    grid-template-columns: 38px 68px 48px minmax(0, 1fr);
    padding-inline: 9px;
  }

  .auto-state {
    width: 100%;
    margin-left: 0;
  }
}
</style>
