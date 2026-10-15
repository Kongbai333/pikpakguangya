import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  // 相对路径：产物既能被 FastAPI 托管，也能放在任意子目录
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
  },
  server: {
    port: 5173,
    // npm run dev 时把接口转给本地后台
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
