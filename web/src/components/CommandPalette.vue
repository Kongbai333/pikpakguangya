<script setup>
/**
 * 命令面板（⌘/Ctrl + K）。
 *
 * 后台有 8 个页面 + 几个全局动作，全靠底部的浮动导航点来点去太慢。
 * 这里做键盘优先的跳转：输入即筛选，↑↓ 选，Enter 走。
 * 只做「跳转」和「全局动作」两件事，不复制页面内的业务操作 ——
 * 那些有上下文（选中了哪些行、当前筛选是什么），放进来反而容易误操作。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { CornerDownLeft, LogOut, Moon, RefreshCw, Search, Sun } from 'lucide-vue-next'

const props = defineProps({
  open: { type: Boolean, default: false },
  pages: { type: Array, default: () => [] },
  dark: { type: Boolean, default: false },
})

const emit = defineEmits(['update:open', 'refresh', 'toggle-theme', 'logout'])

const router = useRouter()
const route = useRoute()

const keyword = ref('')
const cursor = ref(0)
const inputEl = ref(null)
const listEl = ref(null)

/* ---------- 候选 ---------- */
const ACTIONS = [
  { key: 'refresh', label: '刷新当前数据', hint: '重新拉一遍接口', icon: RefreshCw },
  { key: 'theme', label: '切换深浅色', hint: '浅色 / 深色', icon: Moon },
  { key: 'browse', label: '打开前台浏览页', hint: '新标签页', icon: Search },
  { key: 'logout', label: '退出管理后台', hint: '', icon: LogOut },
]

const pageItems = computed(() =>
  props.pages.map((p) => ({
    key: `page:${p.to}`,
    group: '跳转',
    label: p.label,
    hint: p.group || '',
    icon: p.icon,
    run: () => router.push(p.to),
  }))
)

const actionItems = computed(() =>
  ACTIONS.map((a) => ({
    key: `action:${a.key}`,
    group: '动作',
    label: a.label,
    hint: a.hint,
    icon: a.key === 'theme' && props.dark ? Sun : a.icon,
    run: () => {
      if (a.key === 'refresh') emit('refresh')
      else if (a.key === 'theme') emit('toggle-theme')
      else if (a.key === 'browse') window.open('#/', '_blank', 'noopener')
      else if (a.key === 'logout') emit('logout')
    },
  }))
)

/** 子序列匹配：'lgl' 能命中 '链接管理'（按字符顺序，不必连续） */
function matches(text, query) {
  if (!query) return true
  const t = text.toLowerCase()
  let i = 0
  for (const ch of query.toLowerCase()) {
    i = t.indexOf(ch, i)
    if (i === -1) return false
    i += 1
  }
  return true
}

const results = computed(() => {
  const q = keyword.value.trim()
  const all = [...pageItems.value, ...actionItems.value]
  return all.filter((item) => matches(`${item.label} ${item.hint}`, q))
})

const groups = computed(() => {
  const out = []
  for (const item of results.value) {
    const last = out[out.length - 1]
    if (last && last.name === item.group) last.items.push(item)
    else out.push({ name: item.group, items: [item] })
  }
  return out
})

/** 扁平序号 → 列表项，键盘上下移动要用 */
const flat = computed(() => groups.value.flatMap((g) => g.items))
const activeKey = computed(() => flat.value[cursor.value]?.key || '')

/* ---------- 交互 ---------- */
watch(keyword, () => {
  cursor.value = 0
})

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    keyword.value = ''
    cursor.value = 0
    await nextTick()
    inputEl.value?.focus()
  }
)

function close() {
  emit('update:open', false)
}

function move(step) {
  const n = flat.value.length
  if (!n) return
  cursor.value = (cursor.value + step + n) % n
  nextTick(() => {
    listEl.value?.querySelector('.cmd-item.on')?.scrollIntoView({ block: 'nearest' })
  })
}

function run(item) {
  if (!item) return
  close()
  // 关掉面板再执行：跳转后焦点留在输入框里会很怪
  nextTick(() => item.run())
}

function onKeydown(event) {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    move(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    move(-1)
  } else if (event.key === 'Enter') {
    event.preventDefault()
    run(flat.value[cursor.value])
  } else if (event.key === 'Escape') {
    event.preventDefault()
    close()
  }
}

function indexOf(item) {
  return flat.value.indexOf(item)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="cmd">
      <div
        v-if="open"
        class="cmd-mask"
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        @click.self="close"
      >
        <div class="cmd-panel">
          <div class="cmd-search">
            <Search :size="16" class="cmd-search__icon" />
            <input
              ref="inputEl"
              v-model="keyword"
              class="cmd-search__input"
              type="text"
              placeholder="搜索页面或动作…"
              autocomplete="off"
              spellcheck="false"
              @keydown="onKeydown"
            />
            <kbd class="cmd-esc">ESC</kbd>
          </div>

          <div ref="listEl" class="cmd-list">
            <template v-for="group in groups" :key="group.name">
              <div class="cmd-group">{{ group.name }}</div>
              <button
                v-for="item in group.items"
                :key="item.key"
                type="button"
                class="cmd-item"
                :class="{ on: item.key === activeKey }"
                @click="run(item)"
                @mousemove="cursor = indexOf(item)"
              >
                <component :is="item.icon" :size="15" class="cmd-item__icon" />
                <span class="cmd-item__label">{{ item.label }}</span>
                <span v-if="item.hint" class="cmd-item__hint">{{ item.hint }}</span>
                <CornerDownLeft v-if="item.key === activeKey" :size="13" class="cmd-item__enter" />
              </button>
            </template>

            <div v-if="!flat.length" class="cmd-empty">
              没有匹配「{{ keyword }}」的页面或动作
            </div>
          </div>

          <div class="cmd-foot">
            <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
            <span><kbd>↵</kbd> 打开</span>
            <span><kbd>esc</kbd> 关闭</span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.cmd-mask {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 14vh 16px 16px;
  background: color-mix(in srgb, #0b0b0d 42%, transparent);
  backdrop-filter: blur(3px);
}

.cmd-panel {
  width: min(560px, 100%);
  max-height: 62vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--app-line);
  border-radius: 18px;
  background: var(--app-bg);
  box-shadow: var(--shadow-3);
}

.cmd-search {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--app-line-soft);
}

.cmd-search__icon {
  flex: 0 0 auto;
  color: var(--app-faint);
}

.cmd-search__input {
  flex: 1;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--app-text);
  font-family: inherit;
  font-size: 15px;
  outline: none;
}

.cmd-search__input::placeholder {
  color: var(--app-faint);
}

.cmd-esc {
  flex: 0 0 auto;
  padding: 2px 6px;
  border: 1px solid var(--app-line);
  border-radius: 6px;
  color: var(--app-faint);
  font-family: inherit;
  font-size: 10.5px;
}

.cmd-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 6px;
}

.cmd-group {
  padding: 8px 10px 4px;
  color: var(--app-faint);
  font-size: 11.5px;
  letter-spacing: 0.6px;
  text-transform: uppercase;
}

.cmd-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 10px;
  border: 0;
  border-radius: 10px;
  background: transparent;
  color: var(--app-text);
  font-family: inherit;
  font-size: 13.5px;
  text-align: left;
  cursor: pointer;
  transition: background var(--dur-fast) ease;
}

.cmd-item.on {
  background: var(--app-panel);
}

.cmd-item__icon {
  flex: 0 0 auto;
  color: var(--app-muted);
}

.cmd-item.on .cmd-item__icon {
  color: var(--app-brand);
}

.cmd-item__label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cmd-item__hint {
  flex: 0 0 auto;
  color: var(--app-faint);
  font-size: 12px;
}

.cmd-item__enter {
  flex: 0 0 auto;
  color: var(--app-faint);
}

.cmd-empty {
  padding: 34px 16px;
  text-align: center;
  color: var(--app-faint);
  font-size: 13.5px;
}

.cmd-foot {
  display: flex;
  gap: 16px;
  padding: 9px 16px;
  border-top: 1px solid var(--app-line-soft);
  color: var(--app-faint);
  font-size: 11.5px;
}

.cmd-foot kbd {
  margin-right: 4px;
  padding: 1px 5px;
  border: 1px solid var(--app-line);
  border-radius: 5px;
  background: var(--app-panel);
  font-family: inherit;
  font-size: 10.5px;
}

/* ---------- 进出场 ---------- */
.cmd-enter-active,
.cmd-leave-active {
  transition: opacity var(--dur-fast) ease;
}

.cmd-enter-active .cmd-panel,
.cmd-leave-active .cmd-panel {
  transition: transform var(--dur) var(--ease-out), opacity var(--dur-fast) ease;
}

.cmd-enter-from,
.cmd-leave-to {
  opacity: 0;
}

.cmd-enter-from .cmd-panel,
.cmd-leave-to .cmd-panel {
  opacity: 0;
  transform: translateY(-10px) scale(0.98);
}
</style>
