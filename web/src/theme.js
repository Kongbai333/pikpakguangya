/** 主题开关：默认跟随系统，用户手动切换后记住选择。 */

import { ref, watch } from 'vue'

const KEY = 'pk.theme'
const saved = localStorage.getItem(KEY)
const prefersDark = window.matchMedia?.('(prefers-color-scheme: dark)').matches

export const dark = ref(saved ? saved === 'dark' : !!prefersDark)

function apply(value) {
  document.documentElement.dataset.theme = value ? 'dark' : 'light'
  document.documentElement.style.colorScheme = value ? 'dark' : 'light'
}

apply(dark.value)
watch(dark, (value) => {
  localStorage.setItem(KEY, value ? 'dark' : 'light')
  apply(value)
})

export function toggleTheme() {
  dark.value = !dark.value
}
