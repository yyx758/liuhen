import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import { onAuthError } from './api'
import { auth, setUser } from './auth'
import { PASSWORD_PATH, router } from './router'

// 任意接口返回 401：会话已失效，清空本地用户数据并回到登录页；需要改密时转到改密页。
onAuthError(error => {
  const current = router.currentRoute.value
  if (error.code === 'password_change_required') {
    if (auth.user) auth.user.must_change_password = true
    void router.replace(PASSWORD_PATH)
    return
  }
  setUser(null)
  if (!current.meta.public) {
    void router.replace({ path: '/login', query: current.fullPath === '/' ? {} : { redirect: current.fullPath } })
  }
})

createApp(App).use(router).mount('#app')
