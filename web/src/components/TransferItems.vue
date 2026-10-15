<script setup>
/**
 * 秒传任务的文件明细列表：正在秒传和历史记录共用。
 *
 * 支持选择性秒传：勾掉的文件不会被传（留在 pending 状态，随时可以改主意）。
 * 两万条明细不可能一次性渲染，所以详情只显示前 N 条，而「全选 / 按大小筛」
 * 这类批量操作由后端直接做 —— 只把改动的那几条 ID 传回去。
 */
import { computed, ref } from 'vue'
// 注意 lucide 0.468 的命名：没有 CheckSquare / Loader2 / Wand2，
// 它们分别叫 SquareCheck / LoaderCircle / WandSparkles。
import { LoaderCircle, Square, SquareCheck, WandSparkles } from 'lucide-vue-next'

import { fmtBytes } from '../format'

const props = defineProps({
  items: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  jobId: { type: Number, default: 0 },
  selection: { type: Object, default: () => ({}) },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['select'])

const ITEM_STATUS = {
  pending: { label: '等待中', type: 'default' },
  completed: { label: '已秒传', type: 'success' },
  skipped: { label: '未命中', type: 'warning' },
  failed: { label: '失败', type: 'error' },
}
const itemStatus = (item) =>
  ITEM_STATUS[item?.status] || { label: item?.status || '未知', type: 'default' }

/** 只有还没处理的条目能改勾选 —— 传完的勾不勾都没意义 */
const editable = (item) => item?.status === 'pending'

const pendingShown = computed(() => props.items.filter(editable))
const shownSelected = computed(() => pendingShown.value.filter((i) => i.selected !== false))

const allShownSelected = computed(
  () => pendingShown.value.length > 0 && shownSelected.value.length === pendingShown.value.length
)

// 负值表示「大于」：幂运算前的一元负号必须加括号，否则是语法错误
const SIZE_PRESETS = [
  { label: '只选小于 100 MB', value: 100 * 1024 * 1024 },
  { label: '只选小于 500 MB', value: 500 * 1024 * 1024 },
  { label: '只选小于 1 GB', value: 1024 ** 3 },
  { label: '只选小于 2 GB', value: 2 * 1024 ** 3 },
  { label: '只选大于 1 GB', value: -(1024 ** 3) },
]
const sizeOptions = SIZE_PRESETS.map((p, i) => ({ label: p.label, value: i }))
const sizePick = ref(null)

function toggleShown() {
  const ids = pendingShown.value.map((i) => i.id)
  if (!ids.length) return
  emit('select', { ids, selected: !allShownSelected.value })
}

function toggleOne(item) {
  emit('select', { ids: [item.id], selected: item.selected === false })
}

/** 按大小批量勾选：由后端跑，不受「只显示前 300 条」的限制 */
function applySize(index) {
  if (index === null || index === undefined) return
  const preset = SIZE_PRESETS[index]
  if (!preset) return
  emit('select', {
    selected: true,
    max_size: preset.value > 0 ? preset.value : null,
    min_size: preset.value < 0 ? -preset.value : null,
  })
  sizePick.value = null
}

const pct = computed(() => {
  const total = Number(props.selection?.total || 0)
  if (!total) return 0
  return Math.round((Number(props.selection?.selected || 0) / total) * 100)
})
</script>

<template>
  <div class="items">
    <!-- 勾选概况 + 批量操作 -->
    <div v-if="pendingShown.length || selection?.total" class="items__bar">
      <div class="items__summary">
        <span class="items__pct">{{ pct }}%</span>
        <span class="items__text">
          已选 <b>{{ selection?.selected ?? 0 }}</b> / {{ selection?.total ?? 0 }} 项
          <template v-if="selection?.selected_bytes">
            · {{ fmtBytes(selection.selected_bytes) }}
          </template>
        </span>
      </div>

      <div class="items__ops">
        <button
          type="button"
          class="op"
          :disabled="busy || !pendingShown.length"
          @click="toggleShown"
        >
          <component :is="allShownSelected ? Square : SquareCheck" :size="14" />
          {{ allShownSelected ? '取消本页' : '全选本页' }}
        </button>
        <button
          type="button"
          class="op"
          :disabled="busy"
          @click="emit('select', { selected: false })"
        >
          全部取消
        </button>
        <button
          type="button"
          class="op"
          :disabled="busy"
          @click="emit('select', { selected: true })"
        >
          全部选中
        </button>
        <n-select
          v-model:value="sizePick"
          class="op-select"
          size="small"
          placeholder="按大小筛选"
          :options="sizeOptions"
          :disabled="busy"
          @update:value="applySize"
        />
        <LoaderCircle v-if="busy" :size="14" class="op-spin" />
        <WandSparkles v-else :size="13" class="op-hint" />
      </div>
    </div>

    <!-- 明细 -->
    <div class="items__list">
      <div
        v-for="item in items"
        :key="item.id"
        class="row"
        :class="{ 'row--off': editable(item) && item.selected === false }"
      >
        <button
          v-if="editable(item)"
          type="button"
          class="row__check"
          :class="{ on: item.selected !== false }"
          :disabled="busy"
          :title="item.selected === false ? '勾上才会秒传' : '取消勾选则不传这个文件'"
          @click="toggleOne(item)"
        >
          <component :is="item.selected === false ? Square : SquareCheck" :size="15" />
        </button>
        <span v-else class="row__check row__check--locked" aria-hidden="true" />

        <n-tag size="tiny" round :type="itemStatus(item).type">
          {{ itemStatus(item).label }}
        </n-tag>
        <span class="row__path" :title="item.path">{{ item.path }}</span>
        <span class="row__size">{{ fmtBytes(item.size_bytes) }}</span>
        <span v-if="item.error" class="row__err" :title="item.error">{{ item.error }}</span>
      </div>

      <div v-if="!items.length" class="row-empty">这个任务没有文件明细</div>
    </div>

    <div v-if="total > items.length" class="items__more muted">
      仅显示前 {{ items.length }} 条，完整明细请导出 JSON（批量勾选不受这个限制）
    </div>
  </div>
</template>

<style scoped>
.items {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* ---------- 概况条 ---------- */
.items__bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border: 1px solid var(--app-line);
  border-radius: 12px;
  background: var(--app-panel);
}

.items__summary {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.items__pct {
  color: var(--app-brand);
  font-size: 15px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.items__text {
  color: var(--app-muted);
  font-size: 12.5px;
}

.items__text b {
  color: var(--app-text);
}

.items__ops {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.op {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--app-line);
  border-radius: 8px;
  background: var(--app-bg);
  color: var(--app-muted);
  font-family: inherit;
  font-size: 12.5px;
  white-space: nowrap;
  cursor: pointer;
  transition: background var(--dur-fast) ease, color var(--dur-fast) ease;
}

.op:hover:not(:disabled) {
  background: var(--app-panel-2);
  color: var(--app-text);
}

.op:disabled {
  opacity: 0.45;
  cursor: default;
}

.op-select {
  width: 148px;
}

.op-spin {
  color: var(--app-brand);
  animation: spin 0.8s linear infinite;
}

.op-hint {
  color: var(--app-faint);
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---------- 明细 ---------- */
.items__list {
  max-height: 380px;
  overflow: auto;
  border: 1px solid var(--app-line);
  border-radius: 12px;
  background: var(--app-bg);
  /* 滚动时用合成层，减少整块重绘 */
  overscroll-behavior: contain;
}

.row {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 11px;
  font-size: 13px;
  border-bottom: 1px solid var(--app-line-soft);
  transition: background var(--dur-fast) ease;
  /* 一次要渲染 300 行，屏幕外的行没必要参与布局和绘制。
     contain-intrinsic-size 给个预估高度，滚动条才不会跳。 */
  content-visibility: auto;
  contain-intrinsic-size: auto 37px;
}

.row:hover {
  background: var(--app-panel);
}

.row:last-child {
  border-bottom: 0;
}

/* 没勾选的行整体压暗，一眼看出哪些不会被传 */
.row--off {
  opacity: 0.5;
}

.row--off .row__path {
  text-decoration: line-through;
  text-decoration-color: var(--app-faint);
}

.row__check {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--app-faint);
  cursor: pointer;
  transition: color var(--dur-fast) ease, background var(--dur-fast) ease;
}

.row__check:hover:not(:disabled) {
  background: var(--app-panel-2);
}

.row__check.on {
  color: var(--app-brand);
}

.row__check:disabled {
  cursor: default;
}

.row__check--locked {
  cursor: default;
}

.row__path {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row__size {
  flex: 0 0 auto;
  color: var(--app-faint);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.row__err {
  flex: 0 0 auto;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--app-danger);
  font-size: 12px;
}

.row-empty {
  padding: 26px 12px;
  text-align: center;
  color: var(--app-faint);
  font-size: 13px;
}

.items__more {
  font-size: 12px;
  text-align: center;
}

@media (max-width: 760px) {
  .items__ops {
    width: 100%;
    margin-left: 0;
  }

  .op-select {
    flex: 1;
    width: auto;
    min-width: 130px;
  }

  .row__err {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .op-spin {
    animation: none;
  }
}
</style>
