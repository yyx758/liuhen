import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import { onAuthError } from './api'
import { setUser } from './auth'
import { router } from './router'

// 任意接口返回 401：会话已失效，清空本地用户数据并回到登录页。
// 强制改密不需要在这里处理：管理员重置密码会让旧会话全部失效（先走 401），
// 用临时密码重新登录后 must_change_password 为 true，由路由守卫转到改密页。
onAuthError(() => {
  const current = router.currentRoute.value
  setUser(null)
  if (!current.meta.public) {
    void router.replace({ path: '/login', query: current.fullPath === '/' ? {} : { redirect: current.fullPath } })
  }
})

createApp(App).use(router).mount('#app')
