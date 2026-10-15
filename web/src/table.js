/**
 * 表格高度：按视口算，让表格铺满当前屏幕而不是固定 560px。
 *
 * 固定高度的问题是双重的：屏幕大的时候底下空一大截，屏幕小的时候
 * 又要在一个小窗口里滚。这里算「视口高度 - 表格上方的实际占用」，
 * 窗口一变化就重算。
 */
import { onBeforeUnmount, onMounted, ref, watchEffect } from 'vue'

/**
 * @param {number|Function} offset 表格上方要扣掉多少像素。
 *   传函数时会在 watchEffect 里执行 —— 函数里读到的响应式数据
 *   （比如「批量操作条是否显示」）一变，高度就跟着重算。
 * @param {{ min?: number, max?: number }} [opts]
 */
export function useTableHeight(offset = 300, { min = 320, max = 2000 } = {}) {
  const height = ref(min)

  function measure() {
    const used = typeof offset === 'function' ? Number(offset()) || 0 : Number(offset) || 0
    const available = window.innerHeight - used
    height.value = Math.max(min, Math.min(max, Math.round(available)))
  }

  onMounted(() => window.addEventListener('resize', measure))
  onBeforeUnmount(() => window.removeEventListener('resize', measure))
  // 立即跑一次，并订阅 offset 里用到的响应式数据
  watchEffect(measure)

  return height
}
