/**
 * 统一的轮询定时器：标签页切到后台时跳过这一轮。
 *
 * 后台标签页里 setInterval 会被浏览器限流，但限流不等于停 —— 一个开着不管的
 * 仪表盘仍然每 5 秒打一轮接口，而每个接口背后都是全表聚合查询。这里在
 * document.hidden 时直接跳过，切回前台时立刻补一次，用户看不到「回来还是旧数据」。
 */
import { onBeforeUnmount, onMounted } from 'vue'

export function isHidden() {
  return typeof document !== 'undefined' && document.hidden
}

/**
 * @param {Function} fn 每轮要跑的事
 * @param {number} every 间隔（毫秒）
 * @param {{ immediate?: boolean }} [opts] immediate=true 时立即先跑一次
 * @returns {Function} 取消函数
 */
export function startPolling(fn, every, { immediate = false } = {}) {
  let stopped = false

  const tick = () => {
    if (stopped || isHidden()) return
    fn()
  }

  const onVisible = () => {
    if (!isHidden()) tick()
  }

  if (immediate) tick()
  const timer = setInterval(tick, every)
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisible)
  }

  return () => {
    stopped = true
    clearInterval(timer)
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisible)
    }
  }
}

/** 组件内用：挂载时开始轮询，卸载时自动停。 */
export function usePolling(fn, every, opts) {
  let stop = null
  onMounted(() => {
    stop = startPolling(fn, every, opts)
  })
  onBeforeUnmount(() => {
    stop?.()
    stop = null
  })
}
