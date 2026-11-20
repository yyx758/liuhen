import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/tasks': 'http://127.0.0.1:8000',
      '/chat': 'http://127.0.0.1:8000',
      '/actions': 'http://127.0.0.1:8000',
      '/audit': 'http://127.0.0.1:8000',
    },
  },
})
