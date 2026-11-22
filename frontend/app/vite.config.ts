import vue from '@vitejs/plugin-vue'
import { defineConfig, type ProxyOptions } from 'vite'

// 后端接口前缀与前端路由（/tasks、/chat）重名：浏览器地址栏直接打开或刷新页面时
// 请求的是 HTML，交给前端处理；fetch 请求才转发到后端。
const backend: ProxyOptions = {
  target: 'http://127.0.0.1:8000',
  bypass: req => (req.headers.accept?.includes('text/html') ? '/index.html' : undefined),
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  base: process.env.NODE_ENV === 'production' ? '/app/' : '/',
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/auth': backend,
      '/tasks': backend,
      '/chat': backend,
      '/actions': backend,
      '/audit': backend,
      '/admin': backend,
      '/notifications': backend,
    },
  },
})
