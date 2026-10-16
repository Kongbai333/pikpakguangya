<script setup>
/**
 * 前台浏览页：给访客看的只读链接库。
 *
 * 从表格换成卡片流：这一页的用途是「找到一条链接，把链接和提取码拿走」，
 * 表格的列对齐在这里没有价值，反而把每行压得很窄。卡片能放下标题、
 * 备注，复制按钮也可以做得更大更明确。
 *
 * 这一页**不显示来源**：source 是采集到这条链接的群名，属于库主的隐私。
 * 后端 /api/items 也直接不返回这个字段，不只是前端不渲染。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  ArrowUp,
  Check,
  Copy,
  Inbox,
  Search,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-vue-next'

import { fetchItems } from '../api'
import { copyText, fmtBytes, fmtTime, hasCode, rowStatus } from '../format'
import EmptyState from '../components/EmptyState.vue'
import SkeletonBlock from '../components/SkeletonBlock.vue'

const message = useMessage()

const loading = ref(false)
const rows = ref([])
const total = ref(0)
const offline = ref(false)
const error = ref('')
// 统计跟着列表一起从 /api/items 拿（公开接口），不回后台那份全局状态 ——
// 前台就是前台，只用公开的东西。
const stats = ref(null)
const copiedKey = ref('')
const scrolled = ref(false)

const q = reactive({ text: '', onlyValid: false, withCode: '', page: 1, size: 24 })

const sizeOptions = [
  { value: 24, label: '24 条/页' },
  { value: 48, label: '48 条/页' },
  { value: 96, label: '96 条/页' },
]
const codeOptions = [
  { label: '提取码不限', value: '' },
  { label: '有提取码', value: 'true' },
  { label: '无提取码', value: 'false' },
]

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / q.size)))
const validCount = computed(() => stats.value?.ok ?? null)

/** 复制反馈：按钮短暂变成 ✓，1.2 秒后复原 */
let copyTimer = null
function flash(key) {
  copiedKey.value = key
  clearTimeout(copyTimer)
  copyTimer = setTimeout(() => {
    copiedKey.value = ''
  }, 1200)
}

async function doCopy(text, key, tip = '已复制') {
  try {
    await copyText(text)
    flash(key)
    message.success(tip)
  } catch {
    message.error('复制失败，手动选一下吧')
  }
}

const payloadOf = (row) => {
  const code = hasCode(row) ? `  提取码 ${row.CODE}` : ''
  return `${row.LINK}${code}`
}

/* ---------- 数据 ---------- */
let searchTimer = null
watch(
  () => q.text,
  () => {
    clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
      q.page = 1
      load()
    }, 320)
  }
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const j = await fetchItems({
      q: q.text,
      page: q.page,
      size: q.size,
      sort: 'id',
      dir: 'desc',
      only_valid: q.onlyValid ? 'true' : '',
      with_code: q.withCode,
    })
    rows.value = j.items || []
    total.value = j.total || 0
    stats.value = j.stats || null
    offline.value = !!j.offline
    if (offline.value) {
      message.warning('接口拿不到，显示的是 data.js 里的静态快照（只读、不能搜索分页）')
    }
  } catch (e) {
    error.value = e.message
    rows.value = []
    total.value = 0
    stats.value = null
  } finally {
    loading.value = false
  }
}

function onFilter() {
  q.page = 1
  load()
}

function resetAll() {
  q.text = ''
  q.onlyValid = false
  q.withCode = ''
  q.page = 1
  load()
}

function goPage(p) {
  q.page = p
  load()
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const hasFilter = computed(() => !!q.text || q.onlyValid || !!q.withCode)

/* ---------- 滚动 ---------- */
function onScroll() {
  scrolled.value = window.scrollY > 60
}
function toTop() {
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

onMounted(() => {
  load()
  window.addEventListener('scroll', onScroll, { passive: true })
})
onUnmounted(() => {
  window.removeEventListener('scroll', onScroll)
  clearTimeout(copyTimer)
  clearTimeout(searchTimer)
})
</script>

<template>
  <div class="browse">
    <!-- 背景：一层很淡的光晕，滚动时会淡出 -->
    <div class="halo" :class="{ 'is-scrolled': scrolled }" aria-hidden="true" />

    <!-- ============ 头部 ============ -->
    <header class="hero" :class="{ 'is-compact': scrolled }">
      <div class="hero__inner">
        <div class="hero__brand">
          <span class="brand-mark">PK</span>
          <div>
            <h1>PikPak 链接库</h1>
            <p>收集到的分享链接都在这里，点一下就能拿走链接和提取码</p>
          </div>
        </div>

        <div class="hero__stats">
          <div class="stat">
            <span class="stat__n">{{ total }}</span>
            <span class="stat__k">条链接</span>
          </div>
          <template v-if="validCount !== null">
            <span class="stat__sep" />
            <div class="stat">
              <span class="stat__n stat__n--ok">{{ validCount }}</span>
              <span class="stat__k">条有效</span>
            </div>
          </template>
          <template v-if="stats?.last_message_at">
            <span class="stat__sep" />
            <div class="stat">
              <span class="stat__n stat__n--sm">{{ fmtTime(stats.last_message_at) }}</span>
              <span class="stat__k">最后更新</span>
            </div>
          </template>
        </div>
      </div>

      <!-- 搜索栏：滚动后吸顶 -->
      <div class="searchbar" :class="{ 'is-stuck': scrolled }">
        <div class="searchbar__inner ctl-row">
          <n-input
            v-model:value="q.text"
            size="large"
            clearable
            placeholder="搜链接、提取码、文件名、备注…"
          >
            <template #prefix><Search :size="16" /></template>
          </n-input>

          <n-select
            class="searchbar__select"
            size="large"
            :value="q.withCode"
            :options="codeOptions"
            @update:value="(v) => { q.withCode = v; onFilter() }"
          />

          <button
            class="toggle"
            type="button"
            :class="{ on: q.onlyValid }"
            @click="q.onlyValid = !q.onlyValid; onFilter()"
          >
            <Sparkles :size="14" />
            只看有效
          </button>

          <n-select
            class="searchbar__select searchbar__select--size"
            size="large"
            :value="q.size"
            :options="sizeOptions"
            @update:value="(v) => { q.size = v; onFilter() }"
          />
        </div>

        <div v-if="hasFilter" class="searchbar__filtered">
          <span>已筛选出 <b>{{ total }}</b> 条</span>
          <button type="button" class="linkbtn" @click="resetAll">
            <X :size="13" /> 清除筛选
          </button>
        </div>
      </div>
    </header>

    <!-- ============ 列表 ============ -->
    <main class="list">
      <n-alert v-if="error" type="error" :show-icon="true" class="mb-4">{{ error }}</n-alert>

      <!-- 首屏骨架 -->
      <div v-if="loading && !rows.length" class="cards">
        <div v-for="i in 6" :key="i" class="card card--ghost">
          <SkeletonBlock width="62%" height="16px" />
          <SkeletonBlock width="38%" height="12px" />
          <SkeletonBlock width="100%" height="34px" radius="10px" />
        </div>
      </div>

      <div v-else-if="rows.length" class="cards" :class="{ 'is-busy': loading }">
        <article
          v-for="(row, index) in rows"
          :key="row.ID"
          class="card"
          :style="{ animationDelay: `${Math.min(index, 12) * 26}ms` }"
        >
          <div class="card__head">
            <span class="card__title" :title="row.TITLE || row.MSG">
              {{ row.TITLE || row.MSG || '未命名分享' }}
            </span>
            <n-tag size="tiny" round :type="rowStatus(row).type" class="shrink-0">
              {{ rowStatus(row).label }}
            </n-tag>
          </div>

          <div class="card__meta">
            <span v-if="row.SIZE_BYTES" class="card__size">{{ fmtBytes(row.SIZE_BYTES) }}</span>
            <span class="card__time">{{ fmtTime(row.FIRST_SEEN) }}</span>
          </div>

          <!-- 链接 + 提取码：整行可点，点哪儿复制哪儿 -->
          <div class="payload">
            <button
              type="button"
              class="payload__link"
              :title="row.LINK"
              @click="doCopy(row.LINK, `l${row.ID}`, '链接已复制')"
            >
              <span class="payload__url">{{ row.LINK }}</span>
              <Check v-if="copiedKey === `l${row.ID}`" :size="14" class="payload__ok" />
            </button>

            <button
              v-if="hasCode(row)"
              type="button"
              class="payload__code"
              :class="{ 'is-copied': copiedKey === `c${row.ID}` }"
              :title="`复制提取码 ${row.CODE}`"
              @click="doCopy(row.CODE, `c${row.ID}`, '提取码已复制')"
            >
              <template v-if="copiedKey === `c${row.ID}`">
                <Check :size="12" /> 已复制
              </template>
              <template v-else>
                提取码 <b>{{ row.CODE }}</b>
              </template>
            </button>
            <span v-else class="payload__nocode">无需提取码</span>
          </div>

          <button
            type="button"
            class="card__copy"
            :class="{ 'is-done': copiedKey === `p${row.ID}` }"
            @click="doCopy(payloadOf(row), `p${row.ID}`, '链接和提取码已复制')"
          >
            <component :is="copiedKey === `p${row.ID}` ? Check : Copy" :size="15" />
            {{ copiedKey === `p${row.ID}` ? '已复制' : '复制全部' }}
          </button>
        </article>
      </div>

      <EmptyState
        v-else
        :icon="hasFilter ? Search : Inbox"
        :title="hasFilter ? '没有匹配的链接' : '库里还没有链接'"
        :hint="
          hasFilter
            ? '换个关键词，或者把筛选条件清掉再看看。'
            : '后台开始采集后，群里的 PikPak 分享会自动出现在这里。'
        "
      >
        <n-button v-if="hasFilter" size="small" secondary class="mt-2" @click="resetAll">
          清除筛选
        </n-button>
      </EmptyState>

      <!-- 分页 -->
      <div v-if="pageCount > 1" class="pager">
        <button
          type="button"
          class="pager__btn"
          :disabled="q.page <= 1"
          @click="goPage(q.page - 1)"
        >
          上一页
        </button>
        <span class="pager__now">{{ q.page }} / {{ pageCount }}</span>
        <button
          type="button"
          class="pager__btn"
          :disabled="q.page >= pageCount"
          @click="goPage(q.page + 1)"
        >
          下一页
        </button>
      </div>

      <footer class="foot">
        <ShieldCheck :size="13" />
        数据全部存在本机，不上传任何地方
      </footer>
    </main>

    <Transition name="fade-up">
      <button v-if="scrolled" class="to-top" title="回到顶部" @click="toTop">
        <ArrowUp :size="17" />
      </button>
    </Transition>
  </div>
</template>

<style scoped>
.browse {
  position: relative;
  min-height: 100%;
}

/* ==========================================================================
   背景光晕
   ========================================================================== */
.halo {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(
      70% 42% at 50% -8%,
      color-mix(in srgb, var(--app-brand) 13%, transparent),
      transparent 70%
    ),
    radial-gradient(
      40% 30% at 88% 4%,
      color-mix(in srgb, var(--app-ok) 8%, transparent),
      transparent 70%
    );
  opacity: 1;
  transition: opacity 0.5s ease;
  animation: halo-breathe 9s ease-in-out infinite;
}

.halo.is-scrolled {
  opacity: 0.35;
}

@keyframes halo-breathe {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.04); }
}

/* ==========================================================================
   头部
   ========================================================================== */
.hero {
  position: relative;
  z-index: 1;
  padding: 40px 20px 0;
  transition: padding var(--dur) var(--ease-out);
}

.hero.is-compact {
  padding-top: 18px;
}

.hero__inner {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  width: 100%;
  max-width: 1280px;
  margin: 0 auto 22px;
}

.hero__brand {
  display: flex;
  align-items: center;
  gap: 13px;
  min-width: 0;
}

.hero__brand h1 {
  margin: 0;
  color: var(--app-text);
  font-size: 26px;
  font-weight: 700;
  line-height: 1.15;
  letter-spacing: -0.5px;
  transition: font-size var(--dur) var(--ease-out);
}

.hero.is-compact .hero__brand h1 {
  font-size: 20px;
}

.hero__brand p {
  margin: 5px 0 0;
  color: var(--app-muted);
  font-size: 13px;
}

.hero__stats {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 9px 16px;
  border: 1px solid var(--app-line);
  border-radius: 14px;
  background: color-mix(in srgb, var(--app-bg) 72%, transparent);
  backdrop-filter: blur(10px);
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.stat__n {
  color: var(--app-text);
  font-size: 19px;
  font-weight: 700;
  line-height: 1.1;
  font-variant-numeric: tabular-nums;
}

.stat__n--sm {
  font-size: 13px;
  font-weight: 600;
}

.stat__n--ok {
  color: var(--app-ok);
}

.stat__k {
  color: var(--app-faint);
  font-size: 11.5px;
}

.stat__sep {
  width: 1px;
  height: 26px;
  background: var(--app-line);
}

/* ---------- 搜索栏 ---------- */
.searchbar {
  position: sticky;
  top: 0;
  z-index: 20;
  padding: 12px 20px;
  transition: background var(--dur) var(--ease-out), box-shadow var(--dur) var(--ease-out);
}

.searchbar.is-stuck {
  background: color-mix(in srgb, var(--app-bg) 88%, transparent);
  backdrop-filter: blur(16px) saturate(160%);
  box-shadow: 0 1px 0 var(--app-line);
}

.searchbar__inner {
  /* 前台的大搜索框：这一行整体比后台工具栏高一档。
     行内所有控件都按这个值出高度（.ctl-row 的规则）。
     40px 正好是 Naive large 的设计高度，字号和内边距也就跟着对上了。 */
  --control-h: 40px;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 150px auto 130px;
  gap: 10px;
  align-items: center;
  width: 100%;
  max-width: 1280px;
  margin: 0 auto;
}

.searchbar__select--size {
  min-width: 0;
}

.toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: var(--control-h);
  padding: 0 15px;
  border: 1px solid var(--app-line);
  border-radius: 10px;
  background: var(--app-bg);
  color: var(--app-muted);
  font-family: inherit;
  font-size: 13.5px;
  white-space: nowrap;
  cursor: pointer;
  transition:
    background var(--dur-fast) ease,
    color var(--dur-fast) ease,
    border-color var(--dur-fast) ease;
}

.toggle:hover {
  color: var(--app-text);
  background: var(--app-panel);
}

.toggle.on {
  border-color: color-mix(in srgb, var(--app-brand) 45%, var(--app-line));
  background: var(--app-brand-soft);
  color: var(--app-brand);
  font-weight: 600;
}

.searchbar__filtered {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  max-width: 1280px;
  margin: 10px auto 0;
  color: var(--app-muted);
  font-size: 12.5px;
}

.searchbar__filtered b {
  color: var(--app-text);
}

.linkbtn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--app-brand);
  font-family: inherit;
  font-size: 12.5px;
  cursor: pointer;
}

.linkbtn:hover {
  text-decoration: underline;
}

/* ==========================================================================
   卡片列表
   ========================================================================== */
.list {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 1280px;
  margin: 0 auto;
  padding: 8px 20px 60px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
  transition: opacity var(--dur) ease;
}

.cards.is-busy {
  opacity: 0.55;
}

.card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 15px 16px 14px;
  border: 1px solid var(--app-line);
  border-radius: 16px;
  background: var(--app-bg);
  animation: card-in 0.4s var(--ease-out) both;
  transition:
    transform var(--dur) var(--ease-spring),
    box-shadow var(--dur) var(--ease-out),
    border-color var(--dur-fast) ease;
}

.card:hover {
  transform: translateY(-3px);
  border-color: color-mix(in srgb, var(--app-brand) 26%, var(--app-line));
  box-shadow: var(--shadow-2);
}

.card--ghost {
  gap: 12px;
  animation: none;
}

@keyframes card-in {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
}

.card__head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.card__title {
  flex: 1;
  min-width: 0;
  color: var(--app-text);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.card__meta {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  color: var(--app-faint);
  font-size: 12px;
}

.card__size,
.card__time {
  white-space: nowrap;
}

.card__time {
  margin-left: auto;
}

/* ---------- 链接 + 提取码 ---------- */
.payload {
  display: flex;
  align-items: stretch;
  gap: 8px;
  min-width: 0;
}

.payload__link {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 11px;
  border: 1px solid var(--app-line-soft);
  border-radius: 11px;
  background: var(--app-panel);
  color: var(--app-muted);
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 11.5px;
  text-align: left;
  cursor: pointer;
  transition: background var(--dur-fast) ease, color var(--dur-fast) ease;
}

.payload__link:hover {
  background: var(--app-panel-2);
  color: var(--app-text);
}

.payload__url {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.payload__ok {
  flex: 0 0 auto;
  color: var(--app-ok);
}

.payload__code {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 9px 12px;
  border: 1px solid color-mix(in srgb, var(--app-brand) 26%, var(--app-line));
  border-radius: 11px;
  background: var(--app-brand-soft);
  color: var(--app-brand);
  font-family: inherit;
  font-size: 12.5px;
  white-space: nowrap;
  cursor: pointer;
  transition:
    transform var(--dur-fast) var(--ease-spring),
    background var(--dur-fast) ease;
}

.payload__code:hover {
  transform: scale(1.04);
}

.payload__code b {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 13.5px;
  letter-spacing: 0.5px;
}

.payload__code.is-copied {
  border-color: color-mix(in srgb, var(--app-ok) 40%, var(--app-line));
  background: color-mix(in srgb, var(--app-ok) 13%, transparent);
  color: var(--app-ok);
}

.payload__nocode {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  padding: 9px 12px;
  border-radius: 11px;
  background: var(--app-panel);
  color: var(--app-faint);
  font-size: 12px;
  white-space: nowrap;
}

/* ---------- 复制全部 ---------- */
.card__copy {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  height: 36px;
  border: 0;
  border-radius: 11px;
  background: var(--app-panel);
  color: var(--app-muted);
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition:
    background var(--dur-fast) ease,
    color var(--dur-fast) ease,
    transform var(--dur-fast) ease;
}

.card__copy:hover {
  background: var(--app-brand);
  color: #fff;
}

.card__copy:active {
  transform: scale(0.985);
}

.card__copy.is-done {
  background: color-mix(in srgb, var(--app-ok) 16%, transparent);
  color: var(--app-ok);
}

/* ==========================================================================
   分页 / 页脚 / 回顶
   ========================================================================== */
.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  margin-top: 28px;
}

.pager__btn {
  height: 36px;
  padding: 0 18px;
  border: 1px solid var(--app-line);
  border-radius: 10px;
  background: var(--app-bg);
  color: var(--app-muted);
  font-family: inherit;
  font-size: 13.5px;
  cursor: pointer;
  transition: background var(--dur-fast) ease, color var(--dur-fast) ease;
}

.pager__btn:hover:not(:disabled) {
  background: var(--app-panel);
  color: var(--app-text);
}

.pager__btn:disabled {
  opacity: 0.42;
  cursor: default;
}

.pager__now {
  color: var(--app-muted);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.foot {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: 34px;
  color: var(--app-faint);
  font-size: 12px;
}

.to-top {
  position: fixed;
  right: 22px;
  bottom: 24px;
  z-index: 40;
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border: 1px solid var(--app-line);
  border-radius: 999px;
  background: color-mix(in srgb, var(--app-bg) 86%, transparent);
  backdrop-filter: blur(12px);
  color: var(--app-muted);
  cursor: pointer;
  box-shadow: var(--shadow-2);
  transition: color var(--dur-fast) ease, transform var(--dur-fast) ease;
}

.to-top:hover {
  color: var(--app-text);
  transform: translateY(-2px);
}

.fade-up-enter-active,
.fade-up-leave-active {
  transition: opacity var(--dur-fast) ease, transform var(--dur) var(--ease-out);
}

.fade-up-enter-from,
.fade-up-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

/* ==========================================================================
   窄屏
   ========================================================================== */
@media (max-width: 900px) {
  .searchbar__inner {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .searchbar__select,
  .searchbar__select--size {
    display: none;
  }

  .hero__stats {
    width: 100%;
    justify-content: space-between;
  }

  .stat__sep {
    display: none;
  }
}

@media (max-width: 640px) {
  .hero {
    padding: 24px 14px 0;
  }

  .hero__brand h1 {
    font-size: 21px;
  }

  .hero__brand p {
    display: none;
  }

  .searchbar {
    padding: 10px 14px;
  }

  .list {
    padding: 6px 14px 48px;
  }

  .cards {
    grid-template-columns: minmax(0, 1fr);
  }

  .payload {
    flex-direction: column;
  }

  .payload__code,
  .payload__nocode {
    justify-content: center;
  }
}

@media (prefers-reduced-motion: reduce) {
  .halo,
  .card {
    animation: none;
  }
}
</style>
