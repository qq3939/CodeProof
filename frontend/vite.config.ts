import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时 Vite 把 /api 代理到本机的 FastAPI（8000），和生产环境 Nginx 的路由保持一致，
// 这样前端代码里只写相对路径 /api/...，Cookie 也是同源的。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: false },
    },
  },
  build: {
    chunkSizeWarningLimit: 2000,
  },
})
