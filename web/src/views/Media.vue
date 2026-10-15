<script setup>
/**
 * Telegram 媒体瀑布流：只展示缩略图，原媒体跳转 Telegram。
 *
 * 缩略图本身很小（Telegram 自带的那种），所以点开看大图意义有限 ——
 * 灯箱主要是为了「看清是什么，再决定去不去 Telegram 看原图」。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Image as ImageIcon,
  RefreshCw,
  X,
} from 'lucide-vue-next'

import * as api from '../api'
import { fmtBytes, fmtTime } from '../format'
import EmptyState from '../components/EmptyState.vue'
import SkeletonBlock from '../components/SkeletonBlock.vue'

const message = useMessage()

const loading = ref(false)
const items = ref([])
const total = ref(0)
const page = ref(1)
const size = 60
const accountOptions = ref([])
const chatOptions = ref([])
const filters = reactive({ media_type: '', account_id: 0, chat_id: '' })

const typeOptions = [
  { label: '全部类型', value: '' },
  { label: '图片', value: 'photo' },
  { label: '视频', value: 'video' },
]
const accountSelectOptions = computed(() => [
  { label: '全部账号', value: 0 },
  ...accountOptions.value.map((a) => ({
    label: a.name || a.phone || `#${a.id}`,
    value: a.id,
  })),
])

/* ---------- 灯箱 ---------- */
const lightbox = ref(-1)
const current = computed(() =>
  lightbox.value >= 0 ? items.value[lightbox.value] || null : null
)

function openLightbox(index) {
  if (!items.value[index]?.thumb_url) return
  lightbox.value = index
}
function closeLightbox() {
  lightbox.value = -1
}
function step(delta) {
  const next = lightbox.value + delta
  if (next < 0 || next >= items.value.length) return
  lightbox.value = next
}
function onKey(event) {
  if (lightbox.value < 0) return
  if (event.key === 'Escape') closeLightbox()
  else if (event.key === 'ArrowLeft') step(-1)
  else if (event.key === 'ArrowRight') step(1)
}

/* ---------- 数据 ---------- */
async function loadOptions() {
  try {
    const j = await api.get('/api/monitor')
    accountOptions.value = j.accounts || []
    chatOptions.value = []
    for (const account of accountOptions.value) {
      for (const chat of account.watched || []) {
        chatOptions.value.push({
          value: chat.chat_id,
          label: `${account.name || account.phone} · ${chat.title || chat.chat_id}`,
        })
      }
    }
  } catch {
    accountOptions.value = []
    chatOptions.value = []
  }
}

async function load(reset = false) {
  if (reset) {
    page.value = 1
    items.value = []
    closeLightbox()
  }
  loading.value = true
  try {
    const j = await api.get('/api/media', {
      page: page.value,
      size,
      media_type: filters.media_type,
      account_id: filters.account_id || '',
      chat_id: filters.chat_id || '',
    })
    items.value = reset ? j.items || [] : items.value.concat(j.items || [])
    total.value = j.total || 0
  } catch (e) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

function more() {
  page.value += 1
  load(false)
}

function onFilter() {
  load(true)
}

function hasFilter() {
  return !!(filters.media_type || filters.account_id || filters.chat_id)
}

function resetFilter() {
  filters.media_type = ''
  filters.account_id = 0
  filters.chat_id = ''
  load(true)
}

onMounted(async () => {
  window.addEventListener('keydown', onKey)
  await Promise.all([loadOptions(), load(true)])
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- ============ 工具栏 ============ -->
    <div class="toolbar">
      <div class="toolbar__row ctl-row">
        <n-select
          class="toolbar__sel"
          :value="filters.media_type"
          :options="typeOptions"
          @update:value="(v) => { filters.media_type = v; onFilter() }"
        />
        <n-select
          class="toolbar__sel"
          :value="filters.account_id"
          :options="accountSelectOptions"
          @update:value="(v) => { filters.account_id = v; onFilter() }"
        />
        <n-select
          class="toolbar__sel toolbar__sel--wide"
          :value="filters.chat_id"
          clearable
          filterable
          :options="chatOptions"
          placeholder="全部群组"
          @update:value="(v) => { filters.chat_id = v; onFilter() }"
        />
        <n-tag round>共 {{ total }} 条</n-tag>
        <n-button v-if="hasFilter()" quaternary @click="resetFilter">
          清除筛选
        </n-button>
        <n-button class="ml-auto" secondary :loading="loading" @click="load(true)">
          <template #icon><RefreshCw :size="15" /></template>
          刷新
        </n-button>
      </div>
      <p class="toolbar__note">
        只缓存 Telegram 缩略图，点卡片看大图，点右下角跳原消息
      </p>
    </div>

    <!-- ============ 瀑布流 ============ -->
    <div v-if="loading && !items.length" class="media-grid">
      <div v-for="i in 10" :key="i" class="media-item media-item--ghost">
        <SkeletonBlock :height="`${120 + (i % 4) * 40}px`" radius="0" />
        <div class="meta">
          <SkeletonBlock width="70%" height="11px" />
          <SkeletonBlock width="45%" height="11px" />
        </div>
      </div>
    </div>

    <EmptyState
      v-else-if="!items.length"
      :icon="ImageIcon"
      title="没有媒体"
      hint="到「账号监控」给群组打开媒体开关，并打开该账号的采集。只有打开开关之后收到的消息才会采缩略图。"
    />

    <div v-else class="media-grid">
      <article
        v-for="(item, index) in items"
        :key="item.id"
        class="media-item"
        :class="{ 'is-clickable': !!item.thumb_url }"
        @click="openLightbox(index)"
      >
        <div class="thumb">
          <img
            v-if="item.thumb_url"
            :src="item.thumb_url"
            :alt="item.file_name || `媒体 ${item.id}`"
            loading="lazy"
          />
          <div v-else class="placeholder">
            <ImageIcon :size="20" />
            <span>{{ item.thumb_status === 'error' ? '缩略图失败' : '处理中' }}</span>
          </div>

          <span v-if="item.media_type === 'video'" class="badge badge--video">视频</span>

          <!-- 悬停时盖一层信息 + 跳转入口 -->
          <div v-if="item.thumb_url" class="overlay">
            <span v-if="item.size_bytes" class="overlay__size">
              {{ fmtBytes(item.size_bytes) }}
            </span>
            <a
              v-if="item.telegram_url"
              class="overlay__go"
              :href="item.telegram_url"
              target="_blank"
              rel="noopener noreferrer"
              title="在 Telegram 里打开原消息"
              @click.stop
            >
              <ExternalLink :size="15" />
            </a>
          </div>
        </div>

        <div class="meta">
          <div class="truncate font-medium" :title="item.file_name">
            {{ item.file_name || (item.media_type === 'video' ? '视频' : '图片') }}
          </div>
          <div class="truncate muted" :title="item.chat_title">
            {{ item.chat_title || item.chat_id }}
          </div>
          <div class="flex justify-between muted">
            <span>{{ fmtBytes(item.size_bytes) }}</span>
            <span>{{ fmtTime(item.posted_at || item.created_at) }}</span>
          </div>
        </div>
      </article>
    </div>

    <div v-if="items.length && items.length < total" class="flex justify-center">
      <n-button :loading="loading" secondary @click="more">加载更多</n-button>
    </div>

    <!-- ============ 灯箱 ============ -->
    <Teleport to="body">
      <Transition name="lb">
        <div
          v-if="current"
          class="lb"
          role="dialog"
          aria-modal="true"
          aria-label="媒体预览"
          @click.self="closeLightbox"
        >
          <button class="lb__close" type="button" title="关闭（Esc）" @click="closeLightbox">
            <X :size="20" />
          </button>

          <button
            class="lb__nav lb__nav--prev"
            type="button"
            title="上一个（←）"
            :disabled="lightbox <= 0"
            @click.stop="step(-1)"
          >
            <ChevronLeft :size="22" />
          </button>

          <figure class="lb__stage" @click.stop>
            <img :src="current.thumb_url" :alt="current.file_name || '媒体预览'" />
            <figcaption class="lb__info">
              <div class="lb__title truncate" :title="current.file_name">
                {{ current.file_name || (current.media_type === 'video' ? '视频' : '图片') }}
              </div>
              <div class="lb__sub">
                <span>{{ current.chat_title || current.chat_id }}</span>
                <span v-if="current.size_bytes">· {{ fmtBytes(current.size_bytes) }}</span>
                <span>· {{ fmtTime(current.posted_at || current.created_at) }}</span>
              </div>
              <a
                v-if="current.telegram_url"
                class="lb__go"
                :href="current.telegram_url"
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLink :size="14" />
                在 Telegram 里看原消息
              </a>
            </figcaption>
          </figure>

          <button
            class="lb__nav lb__nav--next"
            type="button"
            title="下一个（→）"
            :disabled="lightbox >= items.length - 1"
            @click.stop="step(1)"
          >
            <ChevronRight :size="22" />
          </button>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
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

.toolbar__sel {
  flex: 0 1 160px;
  min-width: 128px;
}

.toolbar__sel--wide {
  flex: 1 1 240px;
  min-width: 180px;
}

.toolbar__note {
  margin: 0;
  color: var(--app-faint);
  font-size: 12px;
}

/* ==========================================================================
   瀑布流
   ========================================================================== */
/* 多列瀑布流：列宽自适应，图片按原始比例排，短卡片自动补到上一列的缝隙里。
   （上一轮清理死代码时把这条规则从 style.css 删掉了，但模板还在用
   .media-grid —— 结果就是块级布局，一张图占满一整行。） */
.media-grid {
  columns: 6 165px;
  column-gap: 12px;
}

.media-item {
  position: relative;
  break-inside: avoid;
  margin-bottom: 12px;
  border: 1px solid var(--app-line);
  border-radius: 14px;
  overflow: hidden;
  background: var(--app-bg);
  box-shadow: var(--shadow-card);
  transition:
    transform var(--dur) var(--ease-spring),
    box-shadow var(--dur) var(--ease-out),
    border-color var(--dur-fast) ease;
}

.media-item.is-clickable {
  cursor: zoom-in;
}

.media-item:hover {
  transform: translateY(-3px);
  border-color: color-mix(in srgb, var(--app-brand) 26%, var(--app-line));
  box-shadow: var(--shadow-2);
}

.media-item--ghost {
  animation: none;
}

.thumb {
  position: relative;
  display: flex;
  width: 100%;
  min-height: 120px;
  align-items: center;
  justify-content: center;
  background: var(--app-panel-2);
  overflow: hidden;
}

.thumb img {
  display: block;
  width: 100%;
  height: auto;
  transition: transform 0.45s var(--ease-out);
}

.media-item:hover .thumb img {
  transform: scale(1.045);
}

.placeholder {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  padding: 36px 12px;
  color: var(--app-muted);
  font-size: 12.5px;
}

.badge {
  position: absolute;
  top: 8px;
  left: 8px;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  backdrop-filter: blur(6px);
}

.badge--video {
  background: color-mix(in srgb, #0b0b0d 58%, transparent);
  color: #fff;
}

/* 悬停浮层：只放最有用的两个信息，别把图盖住 */
.overlay {
  position: absolute;
  inset: auto 0 0 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 20px 9px 8px;
  background: linear-gradient(transparent, color-mix(in srgb, #0b0b0d 62%, transparent));
  opacity: 0;
  transition: opacity var(--dur) ease;
}

.media-item:hover .overlay,
.media-item:focus-within .overlay {
  opacity: 1;
}

.overlay__size {
  color: #fff;
  font-size: 12px;
  font-weight: 600;
}

.overlay__go {
  margin-left: auto;
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 999px;
  background: color-mix(in srgb, #fff 88%, transparent);
  color: #18181b;
  transition: transform var(--dur-fast) var(--ease-spring);
}

.overlay__go:hover {
  transform: scale(1.12);
}

.meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px 9px;
  font-size: 12.5px;
  color: var(--app-muted);
}

/* ==========================================================================
   灯箱
   ========================================================================== */
.lb {
  position: fixed;
  inset: 0;
  z-index: 300;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 4vh 16px;
  background: color-mix(in srgb, #08080a 78%, transparent);
  backdrop-filter: blur(6px);
}

.lb__stage {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: min(880px, 100%);
  max-height: 100%;
  margin: 0;
  animation: lb-in 0.28s var(--ease-out) both;
}

@keyframes lb-in {
  from {
    opacity: 0;
    transform: scale(0.97);
  }
}

.lb__stage img {
  display: block;
  max-width: 100%;
  max-height: 74vh;
  object-fit: contain;
  border-radius: 12px;
  background: #0b0b0d;
}

.lb__info {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 12px 14px;
  border-radius: 12px;
  background: color-mix(in srgb, #16161a 88%, transparent);
  color: #e7e7ea;
}

.lb__title {
  font-size: 14px;
  font-weight: 600;
}

.lb__sub {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  color: #9b9ba4;
  font-size: 12.5px;
}

.lb__go {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 3px;
  color: #93c5fd;
  font-size: 12.5px;
  text-decoration: none;
}

.lb__go:hover {
  text-decoration: underline;
}

.lb__close {
  position: absolute;
  top: 18px;
  right: 20px;
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border: 0;
  border-radius: 999px;
  background: color-mix(in srgb, #fff 14%, transparent);
  color: #fff;
  cursor: pointer;
  transition: background var(--dur-fast) ease;
}

.lb__close:hover {
  background: color-mix(in srgb, #fff 24%, transparent);
}

.lb__nav {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  border: 0;
  border-radius: 999px;
  background: color-mix(in srgb, #fff 12%, transparent);
  color: #fff;
  cursor: pointer;
  transition: background var(--dur-fast) ease, opacity var(--dur-fast) ease;
}

.lb__nav:hover:not(:disabled) {
  background: color-mix(in srgb, #fff 22%, transparent);
}

.lb__nav:disabled {
  opacity: 0.25;
  cursor: default;
}

.lb-enter-active,
.lb-leave-active {
  transition: opacity var(--dur) ease;
}

.lb-enter-from,
.lb-leave-to {
  opacity: 0;
}

@media (max-width: 1400px) {
  .media-grid {
    columns: 5 160px;
  }
}

@media (max-width: 1000px) {
  .media-grid {
    columns: 4 150px;
  }
}

@media (max-width: 760px) {
  .media-grid {
    columns: 2 140px;
    column-gap: 9px;
  }

  .media-item {
    margin-bottom: 9px;
    border-radius: 12px;
  }

  .lb__nav {
    display: none;
  }

  .lb__stage img {
    max-height: 62vh;
  }

  .toolbar__sel,
  .toolbar__sel--wide {
    flex: 1 1 140px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .lb__stage,
  .thumb img,
  .media-item {
    animation: none;
    transition: none;
  }
  .media-item:hover .thumb img {
    transform: none;
  }
}
</style>
